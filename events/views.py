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
from django.urls import reverse

from linebot import LineBotApi, WebhookParser, WebhookHandler
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    PostbackEvent, TemplateSendMessage, PostbackAction,
    QuickReply, QuickReplyButton, URIAction
    )
from linebot.exceptions import InvalidSignatureError

from . import ui, utils
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
    """
    host = request.get_host()
    # ngrok/Proxy 配下でも必ず https で返す（LINEは http を拒否する）
    abs_redirect = f"https://{host}{reverse('liff_entry')}"

    return render(request, 'events/liff_app.html', {
        'LIFF_ID': getattr(settings, 'LIFF_ID', ''),
        'LIFF_REDIRECT_ABS': abs_redirect,
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
    GET: イベント一覧（既存仕様）
    POST: イベント作成
      受理JSON:
        {
          "id_token": "<LIFFで取得したIDトークン>",     # 必須（サーバで検証）
          "name": "タイトル",                           # 必須
          "date": "YYYY-MM-DD",                         # 必須（開始日）
          "start_time": "HH:MM" | "",                   # 任意（未指定=日付のみ）
          "endmode": "time" | "duration" | "",          # 任意（自動判定も可）
          "end_time": "HH:MM" | "",                     # 任意（endmode=time 時）
          "duration": "1:30" | "90m" | "2h" | "120",    # 任意（endmode=duration 時）
          "capacity": 12,                                # 任意（1以上）
          "scope_id": "<groupId or userId>"              # 任意（URLクエリから渡す想定）
        }
    """
    # ====== GET: 一覧（既存のロジックを維持）======
    if request.method == 'GET':
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

        prefer = ['id', 'name', 'title', 'date', 'event_date',
                  'start_time', 'start_time_has_clock', 'end_time', 'capacity']
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

    # ====== POST: 作成 ======
    if request.method != 'POST':
        return HttpResponseBadRequest('invalid method')

    try:
        body = json.loads(request.body.decode('utf-8'))
    except Exception:
        return HttpResponseBadRequest('invalid json')

    # 1) IDトークン検証（サーバ側で必ず実施）
    id_token = (body.get('id_token') or "").strip()
    if not id_token:
        return JsonResponse({'ok': False, 'reason': 'id_token required'}, status=401)

    try:
        res = requests.post(
            'https://api.line.me/oauth2/v2.1/verify',
            data={'id_token': id_token, 'client_id': getattr(settings, 'MINIAPP_CHANNEL_ID', '')},
            timeout=10
        )
        vr = res.json()
        if res.status_code != 200:
            return JsonResponse({'ok': False, 'reason': vr}, status=401)
        user_id = vr.get('sub') or ''
        if not user_id:
            return JsonResponse({'ok': False, 'reason': 'verify ok but no sub'}, status=401)
    except Exception as e:
        return JsonResponse({'ok': False, 'reason': str(e)}, status=500)

    # 2) 入力バリデーション＆日時合成
    name = (body.get('name') or '').strip()
    date_str = (body.get('date') or '').strip()
    if not name or not date_str:
        return JsonResponse({'ok': False, 'reason': 'name and date are required'}, status=400)

    # 開始日の00:00(現地TZ) → aware → UTC
    base_dt = utils.extract_dt_from_params_date_only({'date': date_str})
    if base_dt is None:
        return JsonResponse({'ok': False, 'reason': 'invalid date'}, status=400)

    start_hhmm = (body.get('start_time') or '').strip()
    if start_hhmm:
        start_dt = utils.hhmm_to_utc_on_same_day(base_dt, start_hhmm)
        if start_dt is None:
            return JsonResponse({'ok': False, 'reason': 'invalid start_time'}, status=400)
        start_has_clock = True
    else:
        start_dt = base_dt
        start_has_clock = False

    endmode = (body.get('endmode') or '').strip()
    end_hhmm = (body.get('end_time') or '').strip()
    duration = (body.get('duration') or '').strip()

    end_dt = None
    # 自動判定（両方空なら未設定）
    if endmode == 'time' or (end_hhmm and not duration):
        if end_hhmm:
            end_dt = utils.hhmm_to_utc_on_same_day(start_dt, end_hhmm)
            if end_dt is None:
                return JsonResponse({'ok': False, 'reason': 'invalid end_time'}, status=400)
            if start_dt and end_dt <= start_dt:
                return JsonResponse({'ok': False, 'reason': 'end_time must be after start_time'}, status=400)
    elif endmode == 'duration' or (duration and not end_hhmm):
        delta = utils.parse_duration_to_delta(duration)
        if not delta or delta.total_seconds() <= 0:
            return JsonResponse({'ok': False, 'reason': 'invalid duration'}, status=400)
        end_dt = start_dt + delta
    # else: end_dt は None（終了未設定）

    cap = body.get('capacity', None)
    if cap in (None, ''):
        capacity = None
    else:
        try:
            cap_int = int(cap)
        except Exception:
            return JsonResponse({'ok': False, 'reason': 'capacity must be integer'}, status=400)
        if cap_int <= 0:
            return JsonResponse({'ok': False, 'reason': 'capacity must be >=1'}, status=400)
        capacity = cap_int

    scope_id = (body.get('scope_id') or '').strip() or None

    # 3) 登録
    e = Event.objects.create(
        name=name,
        start_time=start_dt,
        end_time=end_dt,
        capacity=capacity,
        start_time_has_clock=start_has_clock,
        created_by=user_id,
        scope_id=scope_id,
    )

    # 4) レスポンス（一覧APIと近い形で返す）
    return JsonResponse({
        'ok': True,
        'item': {
            'id': e.id,
            'name': e.name,
            'start_time': _to_str(e.start_time),
            'start_time_has_clock': e.start_time_has_clock,
            'end_time': _to_str(e.end_time),
            'capacity': e.capacity,
        }
    }, status=201)
