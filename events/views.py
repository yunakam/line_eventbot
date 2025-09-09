# events/views.py
import os
import re
import unicodedata
import json
import requests  
from datetime import date, time, timedelta, datetime

from django.apps import apps
from django.http import HttpResponse, JsonResponse, HttpResponseBadRequest
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.core.exceptions import ImproperlyConfigured
from django.conf import settings
from django.utils.dateparse import parse_datetime
from django.utils import timezone

from linebot import LineBotApi, WebhookParser, WebhookHandler
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    PostbackEvent, TemplateSendMessage, PostbackAction,
    QuickReply, QuickReplyButton, URIAction
    )
from linebot.exceptions import InvalidSignatureError

from . import ui
from .models import Event, EventDraft, EventEditDraft
from .utils import build_liff_url_for_source
from .handlers import create_wizard as cw, edit_wizard as ew, commands as cmd

import logging
logger = logging.getLogger(__name__)



def _get(name: str) -> str:
    """共通キー優先で .env から取得。無ければ APP_ENV を見て環境別キーを参照。"""
    val = os.getenv(name, "")
    if val:
        return val
    env = os.getenv("APP_ENV", "dev").lower()
    return os.getenv(f"{name}_{env.upper()}", "")

# .env 参照（LINE_* が無ければ MESSAGING_* もフォールバックで見る）
_access_token = _get("LINE_CHANNEL_ACCESS_TOKEN") or _get("MESSAGING_CHANNEL_ACCESS_TOKEN")
_channel_secret = _get("LINE_CHANNEL_SECRET") or _get("MESSAGING_CHANNEL_SECRET")

if not _access_token or not _channel_secret:
    # 起動時に気づけるよう、明示的に例外を投げる
    raise RuntimeError("LINE channel credentials are not set. Check .env")

line_bot_api = LineBotApi(_access_token)
handler = WebhookHandler(_channel_secret)


@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    text = (event.message.text or "").strip()
    source = event.source

    # 「イベント」での分岐
    if text in ("イベント", "event", "ｲﾍﾞﾝﾄ"):
        # グループでは「1:1で作成してね」と最小ガイダンスだけ返す
        if source.type == "group":
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="イベントはボットとの1:1チャットで作れるよ")
            )
            return
        # 1:1 では LIFF を開くボタンを返す
        liff_url = build_liff_url_for_source(
            source_type="user",
            user_id=getattr(source, "user_id", None),
        )
        msg = ui.msg_open_liff("イベント管理を開くよ。『開く』をタップしてね。", liff_url)
        line_bot_api.reply_message(event.reply_token, msg)
        return

    # ここまで来たら動作確認用のエコー（まずは確実に返信が出る状態を作る）
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=f"受け付けたよ: {text}")
    )
    
    
# ===== 以下、Chatbot用 ===== #

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
# --- 既存の import 群の後に line_bot_api / handler の初期化があること（前回答の通り） ---

@csrf_exempt
def callback(request):
    # get X-Line-Signature header value
    signature = request.META.get('HTTP_X_LINE_SIGNATURE', '')
    # get request body as text
    body = request.body.decode('utf-8')
    logger.debug("Request body: %s", body)

    # ★ここは「手動で events を回さない」★
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.error("Invalid signature. Check your channel access token/channel secret.")
        return HttpResponse(status=400)
    except Exception as e:
        logger.error("Error: %s", str(e))
        return HttpResponseBadRequest()

    return HttpResponse('OK')



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

@csrf_exempt
def events_list(request):
    """
    イベント一覧（GET）／イベント作成（POST）。
    GET: ?scope_id= でスコープ絞り込み
    POST JSON: {
      name, date("YYYY-MM-DD"), start_hhmm("HH:MM"|null),
      end_hhmm("HH:MM"|null), duration("1:30"/"90m"/"2h"/"120"|null),
      capacity(int|null), scope_id(str|null)
    }
    """
    # ---- POST: 作成 ----
    if request.method == 'POST':
        try:
            body = json.loads(request.body.decode('utf-8'))
        except Exception:
            return HttpResponseBadRequest('invalid json')

        name = (body.get('name') or '').strip()
        date_str = (body.get('date') or '').strip()
        start_hhmm = (body.get('start_hhmm') or None)
        end_hhmm = (body.get('end_hhmm') or None)
        duration = (body.get('duration') or None)
        capacity = body.get('capacity', None)
        scope_id = (body.get('scope_id') or None)

        if not name:
            return HttpResponseBadRequest('name is required')
        if not date_str:
            return HttpResponseBadRequest('date is required')

        # 日付をその日の 00:00（現在TZのaware）に
        from . import utils
        base_dt = utils.extract_dt_from_params_date_only({'date': date_str})
        if not base_dt:
            return HttpResponseBadRequest('invalid date')

        # 開始
        start_time_has_clock = False
        if start_hhmm:
            sdt = utils.hhmm_to_utc_on_same_day(base_dt, start_hhmm)
            if sdt is None:
                return HttpResponseBadRequest('invalid start_hhmm')
            start_dt = sdt
            start_time_has_clock = True
        else:
            # 時刻なし→ 00:00（UTCに合わせる）
            start_dt = base_dt.astimezone(timezone.utc)

        # 終了（終了時刻 or 所要時間）
        end_dt = None
        end_time_has_clock = None
        if end_hhmm:
            edt = utils.hhmm_to_utc_on_same_day(base_dt, end_hhmm)
            if edt is None:
                return HttpResponseBadRequest('invalid end_hhmm')
            if edt <= start_dt:
                return HttpResponseBadRequest('end must be after start')
            end_dt = edt
            end_time_has_clock = True
        elif duration:
            delta = utils.parse_duration_to_delta(duration)
            if not delta or delta.total_seconds() <= 0:
                return HttpResponseBadRequest('invalid duration')
            end_dt = start_dt + delta
            end_time_has_clock = False

        # capacity
        if capacity is not None:
            try:
                capacity = int(capacity)
                if capacity <= 0:
                    capacity = None
            except Exception:
                capacity = None

        e = Event.objects.create(
            name=name,
            start_time=start_dt,
            end_time=end_dt,
            capacity=capacity,
            start_time_has_clock=start_time_has_clock,
            scope_id=scope_id or None,
        )

        # レスポンス（直列化は既存GETに合わせ安全）
        obj = {
            'id': e.id,
            'name': e.name,
            'start_time': e.start_time.isoformat() if e.start_time else None,
            'end_time': e.end_time.isoformat() if e.end_time else None,
            'capacity': e.capacity,
        }
        return JsonResponse({'ok': True, 'item': obj}, status=201)

    # ---- GET: 一覧（既存の実装そのまま） ----
    if request.method != 'GET':
        return HttpResponseBadRequest('invalid method')

    scope_id = request.GET.get('scope_id') or None

    try:
        EventModel = apps.get_model('events', 'Event')
    except LookupError:
        return JsonResponse({'ok': True, 'items': []}, status=200)

    fields = {f.name for f in EventModel._meta.get_fields() if hasattr(f, 'attname')}
    order_candidates = ['date', 'event_date', 'start_time', 'id']
    order_keys = [k for k in order_candidates if k in fields]
    try:
        qs = EventModel.objects.all()
        if scope_id and 'scope_id' in fields:
            qs = qs.filter(scope_id=scope_id)
        if order_keys:
            qs = qs.order_by(*order_keys)
        qs = qs[:100]
    except Exception:
        return JsonResponse({'ok': True, 'items': []}, status=200)

    prefer = ['id', 'name', 'title', 'date', 'event_date', 'start_time', 'end_time', 'capacity']
    items = []
    for e in qs:
        obj = {}
        for key in prefer:
            if key in fields:
                obj[key] = _to_str(getattr(e, key, None))
        if 'name' not in obj and 'title' in obj:
            obj['name'] = obj.get('title')
        items.append(obj)

    return JsonResponse({'ok': True, 'items': items}, status=200)
