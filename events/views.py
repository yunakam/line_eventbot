# events/views.py
import os, json, unicodedata, requests
from datetime import date, time, datetime

from django.apps import apps
from django.http import HttpResponse, JsonResponse, HttpResponseBadRequest
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.core.exceptions import ImproperlyConfigured
from django.conf import settings
from django.utils import timezone
from django.urls import reverse
from django.db.models import Q

from linebot import LineBotApi, WebhookParser, WebhookHandler
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    QuickReply, QuickReplyButton, URIAction, FlexSendMessage,
    JoinEvent, LeaveEvent
)
from linebot.exceptions import InvalidSignatureError

from . import ui, utils, policies
from .models import KnownGroup, Event, Participant
from .utils import build_liff_url_for_source, build_liff_deeplink_for_source  # 既存依存を維持
# handlers.* は現状未使用だが、プロジェクト内参照があるため残置
from .handlers import create_wizard as cw, edit_wizard as ew, commands as cmd

import logging
logger = logging.getLogger(__name__)


# =========================
# 設定・クライアント初期化
# =========================

def _get(name: str) -> str:
    """.envから取得。無ければ APP_ENV に応じたキーへフォールバック。"""
    val = os.getenv(name, "")
    if val:
        return val
    env = os.getenv("APP_ENV", "dev").lower()
    return os.getenv(f"{name}_{env.upper()}", "")

_ACCESS_TOKEN = _get("LINE_CHANNEL_ACCESS_TOKEN") or _get("MESSAGING_CHANNEL_ACCESS_TOKEN")
_CHANNEL_SECRET = _get("LINE_CHANNEL_SECRET") or _get("MESSAGING_CHANNEL_SECRET")
if not _ACCESS_TOKEN or not _CHANNEL_SECRET:
    raise RuntimeError("LINE channel credentials are not set. Check .env")

line_bot_api = LineBotApi(_ACCESS_TOKEN)
handler = WebhookHandler(_CHANNEL_SECRET)


# =========================
# ヘルパ
# =========================

def _touch_known_group(group_id: str, *, refresh_summary: bool = False) -> None:
    """KnownGroupをupsertし、既存行でもjoined=Trueに戻す。必要に応じて名前/アイコンも更新。"""
    if not group_id:
        return
    obj, _ = KnownGroup.objects.get_or_create(group_id=group_id, defaults={"joined": True})
    if obj.joined is False:
        obj.joined = True
    obj.last_seen_at = timezone.now()
    if refresh_summary:
        try:
            s = line_bot_api.get_group_summary(group_id)
            obj.name = getattr(s, "group_name", None) or getattr(s, "groupName", "") or obj.name
            obj.picture_url = getattr(s, "picture_url", None) or getattr(s, "pictureUrl", "") or obj.picture_url
            obj.last_summary_at = timezone.now()
        except Exception:
            pass
    obj.save()

def _is_home_menu_trigger(text: str) -> bool:
    """'ボット'系や'🤖'でホームトリガー判定。"""
    if not text:
        return False
    if "🤖" in text:
        return True
    norm = unicodedata.normalize("NFKC", text).strip().lower()
    return norm in ("ボット", "ぼっと", "bot")

def get_line_clients():
    """Messaging APIクライアント（通知用途）を返す。"""
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

def _resolve_scope_id(obj) -> str:
    """会話スコープID（group/room/user）を抽出。"""
    source = getattr(obj, "source", obj)
    return getattr(source, "group_id", None) \
        or getattr(source, "room_id", None) \
        or getattr(source, "user_id", "")

def _to_str(v):
    """date/datetime/timeはisoformat、それ以外は安全なstr化。"""
    if isinstance(v, (datetime, date, time)):
        try:
            return v.isoformat()
        except Exception:
            return str(v)
    return v if isinstance(v, (int, float, bool)) else (str(v) if v is not None else None)

def _verify_id_token_internal(id_token: str) -> dict:
    """LIFFのIDトークンを MINIAPP_CHANNEL_ID で検証。OKでsub等を返す。NGで例外。"""
    channel_id = getattr(settings, 'MINIAPP_CHANNEL_ID', '')
    resp = requests.post(
        'https://api.line.me/oauth2/v2.1/verify',
        data={'id_token': id_token, 'client_id': channel_id},
        timeout=10
    )
    data = resp.json()
    if resp.status_code != 200 or not data.get('sub'):
        logger.warning("verify status=%s body=%s", resp.status_code, data)
        raise ValueError('verify failed')
    return data


# =========================
# Webhook（LINEプラットフォーム）
# =========================

@csrf_exempt
def callback(request):
    """LINEからのWebhook受信。ハンドラに委譲。"""
    signature = request.META.get('HTTP_X_LINE_SIGNATURE', '')
    body = request.body.decode('utf-8')
    logger.debug("Request body: %s", body)
    utils.set_request_host(request.get_host())
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.error("Invalid signature. Check your channel access token/channel secret.")
        return HttpResponse(status=400)
    except Exception as e:
        logger.error("Error: %s", str(e))
        return HttpResponseBadRequest()
    finally:
        utils.set_request_host(None)
    return HttpResponse('OK')

@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    """テキスト受信ハンドラ。簡易コマンドとLIFF起動誘導。"""
    text = (event.message.text or "").strip()
    source = event.source

    if getattr(source, "type", "") == "group":
        _touch_known_group(getattr(source, "group_id", ""), refresh_summary=False)

    if text in ("グループID", "group id", "groupid", "gid"):
        if getattr(source, "type", "") == "group":
            gid = getattr(source, "group_id", "")
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"このグループIDは {gid} だよ"))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="グループのトークで呼び出してね"))
        return

    if text in ("イベント", "event", "ｲﾍﾞﾝﾄ"):
        if source.type == "group":
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="イベントはボットとの1:1チャットで作れるよ"))
            return
        liff_url = build_liff_url_for_source(source_type="user", user_id=getattr(source, "user_id", None))
        msg = ui.msg_open_liff("イベント管理を開くよ。『開く』をタップしてね。", liff_url)
        line_bot_api.reply_message(event.reply_token, msg)
        return

@handler.add(JoinEvent)
def handle_join(event):
    """グループに追加されたらKnownGroupを更新。"""
    st = getattr(event.source, "type", "")
    gid = getattr(event.source, "group_id", "") or getattr(event.source, "room_id", "")
    logger.info("JoinEvent received: type=%s id=%s", st, gid)
    if st == "group" and gid:
        _touch_known_group(gid, refresh_summary=True)

@handler.add(LeaveEvent)
def handle_leave(event):
    """グループ退出時にjoined=Falseへ。"""
    try:
        gid = getattr(event.source, "group_id", "") or getattr(event.source, "room_id", "")
        if gid:
            KnownGroup.objects.filter(group_id=gid).update(joined=False, last_seen_at=timezone.now())
    except Exception:
        pass


# =========================
# LIFF（HTML/検証）
# =========================

def liff_entry(request):
    """LIFFのエントリHTMLを返す。必要に応じてKnownGroupを更新。"""
    host = request.get_host()
    abs_redirect = f"https://{host}{reverse('liff_entry')}"
    group_id = request.GET.get('groupId') or ""
    if group_id:
        try:
            obj, _ = KnownGroup.objects.get_or_create(group_id=group_id, defaults={"joined": True})
            obj.last_seen_at = timezone.now()
            try:
                s = line_bot_api.get_group_summary(group_id)
                obj.name = getattr(s, "group_name", None) or getattr(s, "groupName", "") or obj.name
                obj.picture_url = getattr(s, "picture_url", None) or getattr(s, "pictureUrl", "") or obj.picture_url
                obj.last_summary_at = timezone.now()
            except Exception:
                pass
            obj.save()
        except Exception:
            pass
    return render(request, 'events/liff_app.html', {
        'LIFF_ID': getattr(settings, 'LIFF_ID', ''),
        'LIFF_REDIRECT_ABS': abs_redirect,
    })

@csrf_exempt
def verify_idtoken(request):
    """LIFFのIDトークンをサーバ側で検証してpayloadを返す（デバッグ/初期化用）。"""
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
            return JsonResponse({'ok': False, 'reason': data}, status=400)
        return JsonResponse({'ok': True, 'payload': data})
    except Exception as e:
        return JsonResponse({'ok': False, 'reason': str(e)}, status=500)


# =========================
# REST API（イベント/グループ）
# =========================

@csrf_exempt
def groups_suggest(request):
    """Bot参加グループを候補返却。only_my=True時はid_token検証＋在籍確認を行う。"""
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'reason': 'method_not_allowed'}, status=405)
    try:
        body = json.loads(request.body.decode('utf-8'))
    except Exception:
        return JsonResponse({'ok': False, 'reason': 'bad_json'}, status=400)

    id_token = (body.get('id_token') or '').strip()
    q = (body.get('q') or '').strip()
    lim = body.get('limit', 20)
    only_my = bool(body.get('only_my', False))

    user_id = None
    if only_my:
        try:
            payload = _verify_id_token_internal(id_token)
            user_id = payload.get('sub') or None
        except Exception:
            return JsonResponse({'ok': False, 'reason': 'invalid_id_token'}, status=401)

    qs = KnownGroup.objects.filter(joined=True)
    if q:
        qs = qs.filter(name__icontains=q)
    qs = qs.order_by('-last_seen_at')[:100]

    items = []
    for g in qs:
        if len(items) >= lim:
            break
        gid = g.group_id
        if only_my:
            try:
                line_bot_api.get_group_member_profile(gid, user_id)
            except Exception:
                continue
        name = g.name or ""
        pic = g.picture_url or ""
        if not name or not pic:
            try:
                s = line_bot_api.get_group_summary(gid)
                name2 = getattr(s, "group_name", None) or getattr(s, "groupName", "") or ""
                pic2 = getattr(s, "picture_url", None) or getattr(s, "pictureUrl", "") or ""
                if name2 and name != name2:
                    g.name = name = name2
                if pic2 and pic != pic2:
                    g.picture_url = pic = pic2
                g.last_summary_at = timezone.now()
                g.save(update_fields=["name", "picture_url", "last_summary_at"])
            except Exception:
                pass
        items.append({'id': gid, 'name': name or gid, 'pictureUrl': pic or ''})

    return JsonResponse({'ok': True, 'items': items, 'total': len(items)}, status=200)

@csrf_exempt
def events_mine(request):
    """自分が作成したイベント一覧（LIFFの1:1ページ用）。"""
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'reason': 'method_not_allowed'}, status=405)
    try:
        body = json.loads(request.body.decode('utf-8'))
    except Exception:
        return JsonResponse({'ok': False, 'reason': 'bad_json'}, status=400)

    id_token = (body.get('id_token') or '').strip()
    if not id_token:
        return JsonResponse({'ok': False, 'reason': 'missing_id_token'}, status=400)

    try:
        payload = _verify_id_token_internal(id_token)
        user_id = payload.get('sub') or None
        if not user_id:
            return JsonResponse({'ok': False, 'reason': 'invalid_id_token'}, status=401)
    except Exception:
        return JsonResponse({'ok': False, 'reason': 'invalid_id_token'}, status=401)

    qs = (Event.objects
          .filter(Q(created_by=user_id) |
                  Q(created_by__isnull=True, scope_id=user_id) |
                  Q(created_by="", scope_id=user_id))
          .order_by('-start_time')[:200])

    items = [{
        'id': e.id,
        'name': e.name,
        'start_time': _to_str(e.start_time),
        'start_time_has_clock': e.start_time_has_clock,
        'end_time': _to_str(e.end_time),
        'capacity': e.capacity,
        'scope_id': e.scope_id,
        'created_by': user_id,
    } for e in qs]
    return JsonResponse({'ok': True, 'items': items}, status=200)

@csrf_exempt
def events_list(request):
    """
    GET : 汎用イベント一覧（scope_id絞り込み対応）
    POST: イベント作成（id_token検証・日時合成・任意通知）
    """
    # GET
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
            if 'created_by' in fields:
                obj['created_by'] = getattr(e, 'created_by', None)
            if 'scope_id' in fields:
                obj['scope_id'] = getattr(e, 'scope_id', None)
            items.append(obj)
        return JsonResponse({'ok': True, 'items': items}, status=200)

    # POST（作成）
    if request.method != 'POST':
        return HttpResponseBadRequest('invalid method')

    try:
        body = json.loads(request.body.decode('utf-8'))
    except Exception:
        return HttpResponseBadRequest('invalid json')

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

    name = (body.get('name') or '').strip()
    date_str = (body.get('date') or '').strip()
    if not name or not date_str:
        return JsonResponse({'ok': False, 'reason': 'name and date are required'}, status=400)

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

    e = Event.objects.create(
        name=name,
        start_time=start_dt,
        end_time=end_dt,
        capacity=capacity,
        start_time_has_clock=start_has_clock,
        created_by=user_id,
        scope_id=scope_id,
    )

    notify = bool(body.get('notify', False))
    if notify and scope_id:
        try:
            utils.set_request_host(request.get_host())
            try:
                liff_url = utils.build_liff_url_for_source(source_type="group", group_id=scope_id)
            finally:
                utils.set_request_host(None)
            flex_contents = {
                "type": "bubble",
                "body": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {"type": "text", "text": f"「{e.name}」が作成されました！", "wrap": True, "weight": "bold", "size": "md"},
                        {"type": "text", "text": "グループのイベントはここから見れるよ", "size": "sm", "color": "#0000FF",
                         "action": {"type": "uri", "label": "ここ", "uri": liff_url}}
                    ]
                }
            }
            msg = FlexSendMessage(
                alt_text=f"「{e.name}」が作成されました！グループのイベントは {liff_url} から見れるよ",
                contents=flex_contents
            )
            line_bot_api.push_message(scope_id, msg)
        except Exception as ex:
            logger.warning("notify push failed: %s", ex)

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

@csrf_exempt
def event_detail(request, event_id: int):
    """単一イベントのGET/PATCH/DELETE。更新はid_token検証＋権限チェック。"""
    try:
        e = Event.objects.get(id=event_id)
    except Event.DoesNotExist:
        return JsonResponse({'ok': False, 'reason': 'not found'}, status=404)

    if request.method == 'GET':
        return JsonResponse({
            'ok': True,
            'item': {
                'id': e.id,
                'name': e.name,
                'start_time': _to_str(e.start_time),
                'start_time_has_clock': getattr(e, 'start_time_has_clock', True),
                'end_time': _to_str(e.end_time),
                'capacity': e.capacity,
                'created_by': getattr(e, 'created_by', None),
            }
        }, status=200)

    if request.method not in ('PATCH', 'DELETE'):
        return HttpResponseBadRequest('invalid method')

    try:
        body = json.loads(request.body.decode('utf-8')) if request.body else {}
    except Exception:
        return HttpResponseBadRequest('invalid json')

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
    except Exception as ex:
        return JsonResponse({'ok': False, 'reason': str(ex)}, status=500)

    if not policies.can_edit_event(user_id, e):
        return JsonResponse({'ok': False, 'reason': 'forbidden'}, status=403)

    if request.method == 'DELETE':
        e.delete()
        return JsonResponse({'ok': True}, status=200)

    # PATCH
    name = (body.get('name') or '').strip() or e.name
    date_str = (body.get('date') or '').strip()
    base_dt = utils.extract_dt_from_params_date_only({'date': date_str}) if date_str else None
    start_base = base_dt or e.start_time
    start_hhmm = (body.get('start_time') or None)
    if start_hhmm is None:
        new_start = e.start_time
        start_has_clock = getattr(e, 'start_time_has_clock', True)
    elif start_hhmm == "":
        if not base_dt:
            base_dt = utils.extract_dt_from_params_date_only({'date': e.start_time.astimezone(timezone.get_current_timezone()).strftime('%Y-%m-%d')})
        new_start = base_dt
        start_has_clock = False
    else:
        hb = base_dt or e.start_time
        new_start = utils.hhmm_to_utc_on_same_day(hb, start_hhmm)
        if new_start is None:
            return JsonResponse({'ok': False, 'reason': 'invalid start_time'}, status=400)
        start_has_clock = True

    endmode = (body.get('endmode') or '').strip()
    end_hhmm = (body.get('end_time') or '').strip()
    duration = (body.get('duration') or '').strip()
    new_end = e.end_time
    if endmode == 'time' or (end_hhmm and not duration):
        if end_hhmm:
            new_end = utils.hhmm_to_utc_on_same_day(new_start, end_hhmm)
            if new_end is None:
                return JsonResponse({'ok': False, 'reason': 'invalid end_time'}, status=400)
            if new_start and new_end <= new_start:
                return JsonResponse({'ok': False, 'reason': 'end_time must be after start_time'}, status=400)
    elif endmode == 'duration' or (duration and not end_hhmm):
        delta = utils.parse_duration_to_delta(duration)
        if not delta or delta.total_seconds() <= 0:
            return JsonResponse({'ok': False, 'reason': 'invalid duration'}, status=400)
        new_end = new_start + delta

    cap = body.get('capacity', '__KEEP__')
    if cap == '__KEEP__':  # 未指定
        new_cap = e.capacity
    elif cap in (None, ''):
        new_cap = None
    else:
        try:
            cap_int = int(cap)
        except Exception:
            return JsonResponse({'ok': False, 'reason': 'capacity must be integer'}, status=400)
        if cap_int <= 0:
            return JsonResponse({'ok': False, 'reason': 'capacity must be >=1'}, status=400)
        new_cap = cap_int

    e.name = name
    e.start_time = new_start
    e.start_time_has_clock = start_has_clock
    e.end_time = new_end
    e.capacity = new_cap
    e.save()

    return JsonResponse({
        'ok': True,
        'item': {
            'id': e.id,
            'name': e.name,
            'start_time': _to_str(e.start_time),
            'start_time_has_clock': e.start_time_has_clock,
            'end_time': _to_str(e.end_time),
            'capacity': e.capacity,
            'created_by': getattr(e, 'created_by', None),
        }
    }, status=200)

@csrf_exempt
def event_participants(request, event_id: int):
    """作成者向けの参加者/ウェイトリスト一覧を返す。プロフィール付与（グループ/ルーム）。"""
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'reason': 'method_not_allowed'}, status=405)
    try:
        body = json.loads(request.body.decode('utf-8'))
    except Exception:
        return JsonResponse({'ok': False, 'reason': 'bad_json'}, status=400)

    id_token = (body.get('id_token') or '').strip()
    if not id_token:
        return JsonResponse({'ok': False, 'reason': 'missing_id_token'}, status=400)

    try:
        payload = _verify_id_token_internal(id_token)
        user_id = payload.get('sub') or None
        if not user_id:
            return JsonResponse({'ok': False, 'reason': 'invalid_id_token'}, status=401)
    except Exception:
        return JsonResponse({'ok': False, 'reason': 'invalid_id_token'}, status=401)

    try:
        e = Event.objects.get(pk=event_id)
    except Event.DoesNotExist:
        return JsonResponse({'ok': False, 'reason': 'not_found'}, status=404)

    is_creator = (
        (getattr(e, 'created_by', None) == user_id) or
        (not getattr(e, 'created_by', None) and getattr(e, 'scope_id', None) == user_id)
    )
    if not is_creator:
        return JsonResponse({'ok': False, 'reason': 'forbidden'}, status=403)

    qs = e.participants.all().order_by('joined_at', 'id')
    base_participants = [{'user_id': p.user_id, 'joined_at': p.joined_at.isoformat()} for p in qs if not p.is_waiting]
    base_waitlist = [{'user_id': p.user_id, 'joined_at': p.joined_at.isoformat()} for p in qs if p.is_waiting]

    profiles = {}
    scope_id = getattr(e, 'scope_id', '') or ''
    if scope_id and (scope_id.startswith('C') or scope_id.startswith('R')):
        try:
            lb, _ = get_line_clients()
        except Exception:
            lb = None
        if lb:
            uids = {r['user_id'] for r in (base_participants + base_waitlist)}
            for uid in uids:
                try:
                    prof = lb.get_group_member_profile(scope_id, uid) if scope_id.startswith('C') \
                        else lb.get_room_member_profile(scope_id, uid)
                    name = getattr(prof, 'display_name', None) or getattr(prof, 'displayName', None) or ''
                    pic = getattr(prof, 'picture_url', None) or getattr(prof, 'pictureUrl', None) or ''
                    profiles[uid] = {'name': name, 'pictureUrl': pic}
                except Exception:
                    pass

    def enrich(rows):
        out = []
        for r in rows:
            prof = profiles.get(r['user_id'], {})
            out.append({**r, 'name': prof.get('name', ''), 'pictureUrl': prof.get('pictureUrl', '')})
        return out

    participants = enrich(base_participants)
    waitlist = enrich(base_waitlist)

    return JsonResponse({
        'ok': True,
        'event': {'id': e.id, 'name': e.name, 'capacity': e.capacity},
        'participants': participants,
        'waitlist': waitlist,
        'counts': {'participants': len(participants), 'waitlist': len(waitlist), 'capacity': e.capacity},
    }, status=200)

@csrf_exempt
def event_rsvp(request, event_id: int):
    """参加/キャンセルAPI。満員時はウェイトリスト登録・繰り上げ昇格に対応。"""
    try:
        e = Event.objects.get(id=event_id)
    except Event.DoesNotExist:
        return JsonResponse({'ok': False, 'reason': 'not found'}, status=404)

    try:
        body = json.loads(request.body.decode('utf-8')) if request.body else {}
    except Exception:
        return HttpResponseBadRequest('invalid json')

    id_token = (body.get('id_token') or "").strip()
    if not id_token:
        return JsonResponse({'ok': False, 'reason': 'id_token required'}, status=401)

    try:
        payload = _verify_id_token_internal(id_token)
        user_id = payload.get('sub') or None
        if not user_id:
            return JsonResponse({'ok': False, 'reason': 'invalid_id_token'}, status=401)
    except Exception:
        return JsonResponse({'ok': False, 'reason': 'invalid_id_token'}, status=401)

    if request.method == 'POST':
        existed = Participant.objects.filter(user_id=user_id, event=e).first()
        if existed:
            return JsonResponse({
                'ok': True, 'status': 'already',
                'is_waiting': existed.is_waiting,
                'confirmed_count': e.participants.filter(is_waiting=False).count(),
                'capacity': e.capacity,
            }, status=200)

        confirmed_count = e.participants.filter(is_waiting=False).count()
        waiting = (e.capacity is not None) and (confirmed_count >= e.capacity)
        Participant.objects.create(user_id=user_id, event=e, is_waiting=waiting)
        return JsonResponse({
            'ok': True,
            'status': 'waiting' if waiting else 'joined',
            'is_waiting': waiting,
            'confirmed_count': e.participants.filter(is_waiting=False).count(),
            'capacity': e.capacity,
        }, status=200)

    if request.method == 'DELETE':
        p = Participant.objects.filter(user_id=user_id, event=e).first()
        if not p:
            return JsonResponse({'ok': True, 'status': 'not_joined'}, status=200)
        p.delete()
        promoted_user_id = None
        if e.capacity is not None:
            w = Participant.objects.filter(event=e, is_waiting=True).order_by('joined_at').first()
            if w:
                w.is_waiting = False
                w.save(update_fields=['is_waiting'])
                promoted_user_id = w.user_id
        return JsonResponse({'ok': True, 'status': 'canceled', 'promoted_user_id': promoted_user_id}, status=200)

    return JsonResponse({'ok': False, 'reason': 'method_not_allowed'}, status=405)

@csrf_exempt
def rsvp_status(request):
    """指定イベントID群に対する自分の参加状況をまとめて返す。"""
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'reason': 'method_not_allowed'}, status=405)
    try:
        body = json.loads(request.body.decode('utf-8'))
    except Exception:
        return JsonResponse({'ok': False, 'reason': 'bad_json'}, status=400)

    id_token = (body.get('id_token') or "").strip()
    ids = body.get('ids') or []
    if not id_token or not isinstance(ids, list) or not ids:
        return JsonResponse({'ok': False, 'reason': 'missing_params'}, status=400)

    try:
        payload = _verify_id_token_internal(id_token)
        user_id = payload.get('sub') or None
        if not user_id:
            return JsonResponse({'ok': False, 'reason': 'invalid_id_token'}, status=401)
    except Exception:
        return JsonResponse({'ok': False, 'reason': 'invalid_id_token'}, status=401)

    rows = Participant.objects.filter(user_id=user_id, event_id__in=ids)
    mp = {str(r.event_id): {'joined': True, 'is_waiting': r.is_waiting} for r in rows}
    for i in ids:
        mp.setdefault(str(i), {'joined': False, 'is_waiting': False})
    return JsonResponse({'ok': True, 'statuses': mp}, status=200)

@csrf_exempt
def group_validate(request):
    """グループIDの有効性・Bot参加・ユーザー在籍（任意）を検証。"""
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'reason': 'method_not_allowed'}, status=405)
    try:
        body = json.loads(request.body.decode('utf-8'))
    except Exception:
        return JsonResponse({'ok': False, 'reason': 'bad_json'}, status=400)

    id_token = (body.get('id_token') or '').strip()
    group_id = (body.get('group_id') or '').strip()
    if not id_token or not group_id:
        return JsonResponse({'ok': False, 'reason': 'missing_params'}, status=400)

    try:
        payload = _verify_id_token_internal(id_token)
        user_id = payload.get('sub') or None
    except Exception as ex:
        logger.warning("verify failed: %s", ex)
        return JsonResponse({'ok': False, 'reason': 'invalid_id_token'}, status=401)

    try:
        summary = line_bot_api.get_group_summary(group_id)
        group_name = getattr(summary, 'group_name', None) or getattr(summary, 'groupName', None) or ''
        picture_url = getattr(summary, 'picture_url', None) or getattr(summary, 'pictureUrl', None) or ''
    except Exception as ex:
        logger.info("get_group_summary failed: %s", ex)
        return JsonResponse({'ok': False, 'reason': 'not_joined_or_invalid'}, status=400)

    user_in_group = None
    if user_id:
        try:
            _ = line_bot_api.get_group_member_profile(group_id, user_id)
            user_in_group = True
        except Exception:
            user_in_group = False

    return JsonResponse({
        'ok': True,
        'group': {'id': group_id, 'name': group_name, 'pictureUrl': picture_url},
        'user_in_group': user_in_group,
    }, status=200)
