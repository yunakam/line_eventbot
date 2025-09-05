# events/views.py
import os
import re
import unicodedata
from datetime import date, time, timedelta, datetime

from django.apps import apps
from django.http import HttpResponse, JsonResponse, HttpResponseBadRequest
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.core.exceptions import ImproperlyConfigured
from django.conf import settings
from django.utils.dateparse import parse_datetime
from django.utils import timezone

from linebot import LineBotApi, WebhookParser
import json
import requests  
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    PostbackEvent, TemplateSendMessage, PostbackAction
)
from linebot.exceptions import InvalidSignatureError

from .models import Event, EventDraft, EventEditDraft
from . import ui, utils
from .handlers import create_wizard as cw, edit_wizard as ew, commands as cmd

import logging
logger = logging.getLogger(__name__)


def _is_home_menu_trigger(text: str) -> bool:
    """
    'ボット' / 'ぼっと' / 'BOT'(全半角・大小文字許容) / 🤖 のときだけ True
    """
    if not text:
        return False
    # 絵文字は正規化せずダイレクトに判定（バリエーションセレクタも吸収）
    if "🤖" in text:
        return True

    # 全角半角の差やケース差を吸収して 'bot' と一致させる
    norm = unicodedata.normalize("NFKC", text).strip().lower()
    if norm in ("ボット", "ぼっと", "bot"):
        return True
    return False


def get_line_clients():
    """
    Messaging API（Bot通知）用のクライアントを返す。
    """
    token = (
        getattr(settings, "MESSAGING_CHANNEL_ACCESS_TOKEN", None)
        or getattr(settings, "LINE_CHANNEL_ACCESS_TOKEN", None)
        or os.getenv("MESSAGING_CHANNEL_ACCESS_TOKEN")
        or os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    )
    secret = (
        getattr(settings, "MESSAGING_CHANNEL_SECRET", None)
        or getattr(settings, "LINE_CHANNEL_SECRET", None)
        or os.getenv("MESSAGING_CHANNEL_SECRET")
        or os.getenv("LINE_CHANNEL_SECRET")
    )

    if not token or not secret:
        raise ImproperlyConfigured("LINEのトークン/シークレットが未設定だよ")

    return LineBotApi(token), WebhookParser(secret)


def _resolve_scope_id(obj) -> str:  # ev も ev.source も受け取れる
    """
    LINEの会話スコープIDを決める。
    - グループ: ev.source.group_id
    - ルーム  : ev.source.room_id
    - 1:1     : ev.source.user_id
    
    ※これを設定しないと、複数のグループでボットを使う場合に
    他グループのイベントまで閲覧可能となる（情報漏洩リスク）
    """
    source = getattr(obj, "source", obj)
    return getattr(source, "group_id", None) \
        or getattr(source, "room_id", None) \
        or getattr(source, "user_id", "")


# ==== LINEプラットフォームからのWebhookエンドポイント ==== #
@csrf_exempt
def callback(request):
    if request.method != "POST":
        return HttpResponse("Method not allowed", status=405)

    line_bot_api, parser = get_line_clients()

    signature = request.headers.get("X-Line-Signature", "")
    body = request.body.decode("utf-8")
    try:
        events = parser.parse(body, signature)
    except InvalidSignatureError:
        return HttpResponse(status=400)

    for ev in events:
        scope_id = _resolve_scope_id(ev)

        # --- Text ---
        if isinstance(ev, MessageEvent) and isinstance(ev.message, TextMessage):
            user_id = ev.source.user_id
            text = ev.message.text.strip()
            
            # ボット起動語彙は最優先で処理。イベントドラフトを破棄してホームメニュー
            if _is_home_menu_trigger(text):
                # ドラフトがある場合は破棄してリセット
                EventDraft.objects.filter(user_id=user_id).delete()
                EventEditDraft.objects.filter(user_id=user_id).delete()
                line_bot_api.reply_message(ev.reply_token, ui.ask_home_menu("home=launch"))
                continue

            # 編集ドラフトがあるなら編集のテキスト優先
            if EventEditDraft.objects.filter(user_id=user_id).exists():
                reply = ew.handle_edit_text(user_id, text)
                if reply:
                    line_bot_api.reply_message(ev.reply_token, reply)
                    continue

            # 作成ウィザード中のテキスト
            if EventDraft.objects.filter(user_id=user_id).exists():
                reply = cw.handle_wizard_text(user_id, text)
                if reply:
                    line_bot_api.reply_message(ev.reply_token, reply)
                    continue

            # それ以外のテキストは全てメニューへ誘導
            # line_bot_api.reply_message(ev.reply_token, ui.ask_home_menu("home=help"))
            # continue


        # --- Postback ---        
        elif isinstance(ev, PostbackEvent):
            user_id = ev.source.user_id
            data = ev.postback.data or ""
            params = ev.postback.params or {}

            if data == "back_home":
                # イベントドラフトを破棄
                EventDraft.objects.filter(user_id=user_id).delete()
                EventEditDraft.objects.filter(user_id=user_id).delete()
                
                line_bot_api.reply_message(ev.reply_token, ui.ask_home_menu())
                continue

            # 1) まずイベント削除（確認→実行）を処理
            if data.startswith("evt=delete&id="):
                m = re.search(r"evt=delete&id=(\d)", data)
                if not m:
                    line_bot_api.reply_message(ev.reply_token, TextSendMessage(text="不正なイベントIDだよ"))
                    continue
                eid = int(m.group(1))
                e = Event.objects.filter(id=eid, scope_id=scope_id).first()
                if not e:
                    line_bot_api.reply_message(ev.reply_token, TextSendMessage(text="イベントが見つからないよ"))
                    continue
                line_bot_api.reply_message(ev.reply_token, ui.ask_delete_confirm(e))
                continue

            if data.startswith("evt=delete_confirm"):
                m = re.search(r"id=(\d)", data)
                ok = "ok=1" in data
                if not m:
                    line_bot_api.reply_message(ev.reply_token, TextSendMessage(text="不正なイベントIDだよ"))
                    continue
                eid = int(m.group(1))
                e = Event.objects.filter(id=eid, scope_id=scope_id).first()
                if not e:
                    line_bot_api.reply_message(ev.reply_token, TextSendMessage(text="イベントが見つからないよ"))
                    continue

                if ok:
                    # 作成者のみ削除可（policies で拡張可）
                    from . import policies  # 既存 import 方針に合わせる
                    if not policies.can_edit_event(user_id, e):
                        line_bot_api.reply_message(ev.reply_token, TextSendMessage(text="イベントの作成者だけが削除できるよ"))
                        continue
                    Event.objects.filter(id=eid, scope_id=scope_id).delete()
                    line_bot_api.reply_message(ev.reply_token, [
                        TextSendMessage(text="イベントを削除したよ"),
                        ui.ask_home_menu()
                    ])
                else:
                    # キャンセル → 詳細へ戻す（元のButtonsTemplate）
                    line_bot_api.reply_message(ev.reply_token, ui.build_event_summary(e))
                continue
            
            
            # 2) 次に一覧/詳細のショートカットを処理
            shortcut = cmd.handle_evt_shortcut(user_id, scope_id, data)
            if shortcut:
                if isinstance(shortcut, (TextSendMessage, TemplateSendMessage)):
                    if getattr(shortcut, "quick_reply", None) is None:
                        shortcut.quick_reply = ui.make_quick_reply(show_home=True, show_exit=True)
                line_bot_api.reply_message(ev.reply_token, shortcut)
                continue

            # 3) ホームメニューの「イベント一覧」
            if data == "home=list":
                qs = Event.objects.filter(scope_id=scope_id).order_by("-id")[:10]
                reply = ui.render_event_list(qs)
                line_bot_api.reply_message(ev.reply_token, reply)
                continue

            # 4) 編集ドラフトがあるなら編集ポストバック優先
            if EventEditDraft.objects.filter(user_id=user_id).exists():
                reply = ew.handle_edit_postback(user_id, scope_id, data, params)
                if reply:
                    line_bot_api.reply_message(ev.reply_token, reply)
                    continue

            # 5) 作成ウィザードのポストバック（home=create / endmode等 含む）
            reply = cw.handle_wizard_postback(user_id, data, params, scope_id)
            if reply:
                line_bot_api.reply_message(ev.reply_token, reply)


    return HttpResponse(status=200)


# =========================
# LIFF 用の最小ビュー
# =========================

def liff_entry(request):
    """
    LIFFエントリのHTMLを返す。
    テンプレート 'events/liff_app.html' に settings.LIFF_ID を埋め込み、
    フロントで liff.init({ liffId }) を行う。
    """
    return render(request, 'events/liff_app.html', {
        'LIFF_ID': getattr(settings, 'LIFF_ID', ''),
    })

@csrf_exempt  # まず通すためCSRF免除。同一オリジンでCSRFトークン送出できるなら外してよい。
def verify_idtoken(request):
    """
    クライアント（LIFF）のIDトークンをサーバで検証する。
    - POST JSON: { "id_token": "<string>" }
    - 検証エンドポイント: https://api.line.me/oauth2/v2.1/verify
      client_id は settings.MINIAPP_CHANNEL_ID を使用する。
    """
    if request.method != 'POST':
        return HttpResponseBadRequest('invalid method')

    try:
        body = json.loads(request.body.decode('utf-8'))
        id_token = body.get('id_token')
        if not id_token:
            return HttpResponseBadRequest('id_token is required')

        res = requests.post(
            'https://api.line.me/oauth2/v2.1/verify',
            data={'id_token': id_token, 'client_id': getattr(settings, 'MINIAPP_CHANNEL_ID', '')},
            timeout=10
        )
        data = res.json()
        if res.status_code != 200:
            # 例: {"error":"invalid_token","error_description":"The token is invalid"}
            return JsonResponse({'ok': False, 'reason': data}, status=400)

        # sub（userId）, name, picture 等が含まれる
        return JsonResponse({'ok': True, 'payload': data})
    except Exception as e:
        return JsonResponse({'ok': False, 'reason': str(e)}, status=500)

def _to_str(v):
    """date/datetime/time は ISO っぽく、それ以外は安全に str 化"""
    if isinstance(v, (datetime, date, time)):
        try:
            return v.isoformat()
        except Exception:
            return str(v)
    return v if isinstance(v, (int, float, bool)) else (str(v) if v is not None else None)

def events_list(request):
    """
    イベント一覧。実在フィールドのみで order_by し、直列化も安全に行う。
    モデル未整備やフィールド差異があっても 500 にしない。
    """
    if request.method != 'GET':
        return HttpResponseBadRequest('invalid method')

    # LIFF から受け取るスコープ（groupId/roomId/userId）。未指定なら全体（後方互換）
    scope_id = request.GET.get('scope_id') or None

    # 1) Event モデル取得（存在しないなら空配列で返す）
    try:
        EventModel = apps.get_model('events', 'Event')
    except LookupError:
        return JsonResponse({'ok': True, 'items': []}, status=200)

    # 2) 利用可能なフィールド名の集合
    fields = {f.name for f in EventModel._meta.get_fields() if hasattr(f, 'attname')}

    # 3) 安全な並び順（存在するものだけ適用）
    order_candidates = ['date', 'event_date', 'start_time', 'id']
    order_keys = [k for k in order_candidates if k in fields]
    try:
        qs = EventModel.objects.all()
        if scope_id and 'scope_id' in fields:
            qs = qs.filter(scope_id=scope_id)
        if order_keys:
            qs = qs.order_by(*order_keys)
        qs = qs[:100]
    except Exception as ex:
        # 並び替え時にエラーが出ても空で返す（500にしない）
        return JsonResponse({'ok': True, 'items': []}, status=200)

    # 4) 直列化（あるものだけ詰める）
    prefer = ['id', 'name', 'title', 'date', 'event_date', 'start_time', 'end_time', 'capacity']
    items = []
    for e in qs:
        obj = {}
        for key in prefer:
            if key in fields:
                obj[key] = _to_str(getattr(e, key, None))
        # name/title の補完
        if 'name' not in obj and 'title' in obj:
            obj['name'] = obj.get('title')
        items.append(obj)

    return JsonResponse({'ok': True, 'items': items}, status=200)
