# events/views.py
import os, re, unicodedata, json, requests
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
from django.db.models import Q

from linebot import LineBotApi, WebhookParser, WebhookHandler
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    PostbackEvent, TemplateSendMessage, PostbackAction,
    QuickReply, QuickReplyButton, URIAction, FlexSendMessage,
    JoinEvent, LeaveEvent
    )
from linebot.exceptions import InvalidSignatureError

from . import ui, utils
from . import policies
from .models import KnownGroup, Event, Participant, EventDraft, EventEditDraft
from .utils import build_liff_url_for_source, build_liff_deeplink_for_source
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


def _touch_known_group(group_id: str, *, refresh_summary: bool = False):
    """
    KnownGroup を upsert し、**既存行でも joined を True に戻す**。
    refresh_summary=True のときは get_group_summary() で名前/アイコンも更新。
    """
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

    

@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    text = (event.message.text or "").strip()
    source = event.source

    if getattr(source, "type", "") == "group":
        _touch_known_group(getattr(source, "group_id", ""), refresh_summary=False)

    # まず「グループID」要求に応答
    if text in ("グループID", "group id", "groupid", "gid"):
        if getattr(source, "type", "") == "group":
            gid = getattr(source, "group_id", "")
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"このグループIDは {gid} だよ")
            )
        else:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="グループのトークで呼び出してね")
            )
        return
    
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


# グループにボットが追加された瞬間に KnownGroup を更新
@handler.add(JoinEvent)
def handle_join(event):
    st = getattr(event.source, "type", "")
    gid = getattr(event.source, "group_id", "") or getattr(event.source, "room_id", "")
    logger.info("JoinEvent received: type=%s id=%s", st, gid)
    if st == "group" and gid:
        _touch_known_group(gid, refresh_summary=True)


# ボットがグループから外れたらフラグを落とす
@handler.add(LeaveEvent)
def handle_leave(event):
    try:
        gid = getattr(event.source, "group_id", "") or getattr(event.source, "room_id", "")
        if gid:
            KnownGroup.objects.filter(group_id=gid).update(joined=False, last_seen_at=timezone.now())
    except Exception:
        pass


@csrf_exempt
def groups_suggest(request):
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'reason': 'method_not_allowed'}, status=405)
    try:
        body = json.loads(request.body.decode('utf-8'))
    except Exception:
        return JsonResponse({'ok': False, 'reason': 'bad_json'}, status=400)

    id_token = (body.get('id_token') or '').strip()
    q   = (body.get('q') or '').strip()
    lim = body.get('limit', 20)
    only_my  = bool(body.get('only_my', False))

    # === ここが重要: only_my=True のときだけ検証 ===
    user_id = None
    if only_my:
        try:
            payload = _verify_id_token_internal(id_token)
            user_id = payload.get('sub') or None
        except Exception:
            return JsonResponse({'ok': False, 'reason': 'invalid_id_token'}, status=401)

    # Bot参加グループ
    qs = KnownGroup.objects.filter(joined=True)
    if q:
        qs = qs.filter(name__icontains=q)
    qs = qs.order_by('-last_seen_at')[:100]

    items = []
    for g in qs:
        if len(items) >= lim:
            break
        gid = g.group_id

        # === 在籍チェックも only_my=True のときだけ ===
        if only_my:
            try:
                line_bot_api.get_group_member_profile(gid, user_id)
            except Exception:
                continue

        # 名前・アイコンの不足を補完（任意）
        name = g.name or ""
        pic  = g.picture_url or ""
        if not name or not pic:
            try:
                s = line_bot_api.get_group_summary(gid)
                name2 = getattr(s, "group_name", None) or getattr(s, "groupName", "") or ""
                pic2  = getattr(s, "picture_url", None) or getattr(s, "pictureUrl", "") or ""
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
    """
    LIFFのIDトークンを検証し、作成者=自分のイベントを返す。
    1:1の「イベント管理」ページ用。
    """
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'reason': 'method_not_allowed'}, status=405)

    try:
        body = json.loads(request.body.decode('utf-8'))
    except Exception:
        return JsonResponse({'ok': False, 'reason': 'bad_json'}, status=400)

    id_token = (body.get('id_token') or '').strip()
    if not id_token:
        return JsonResponse({'ok': False, 'reason': 'missing_id_token'}, status=400)

    # 1) IDトークン検証 → user_id
    try:
        payload = _verify_id_token_internal(id_token)
        user_id = payload.get('sub') or None
        if not user_id:
            return JsonResponse({'ok': False, 'reason': 'invalid_id_token'}, status=401)
    except Exception:
        return JsonResponse({'ok': False, 'reason': 'invalid_id_token'}, status=401)

    # 2) 自分が作成したイベント
    #    ※旧データ救済: created_by が空/NULL & scope_id が自分の userId のものも含める
    qs = (Event.objects
        .filter(Q(created_by=user_id) |
                Q(created_by__isnull=True, scope_id=user_id) |
                Q(created_by="", scope_id=user_id))
        .order_by('-start_time')[:200])


    def _to_str(dt):
        return dt.isoformat() if dt else None

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
def event_participants(request, event_id: int):
    """
    作成者だけが見られる参加者/ウェイトリスト一覧 API。
    POST JSON: { "id_token": "<LIFFのIDトークン>" }
    戻り: {
      ok,
      event:{id,name,capacity},
      participants:[{user_id, joined_at, name?, pictureUrl?}],
      waitlist:[{...}],
      counts:{participants, waitlist, capacity}
    }
    """
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'reason': 'method_not_allowed'}, status=405)

    # 受信JSON
    try:
        body = json.loads(request.body.decode('utf-8'))
    except Exception:
        return JsonResponse({'ok': False, 'reason': 'bad_json'}, status=400)

    id_token = (body.get('id_token') or '').strip()
    if not id_token:
        return JsonResponse({'ok': False, 'reason': 'missing_id_token'}, status=400)

    # トークン検証 → user_id
    try:
        payload = _verify_id_token_internal(id_token)
        user_id = payload.get('sub') or None
        if not user_id:
            return JsonResponse({'ok': False, 'reason': 'invalid_id_token'}, status=401)
    except Exception:
        return JsonResponse({'ok': False, 'reason': 'invalid_id_token'}, status=401)

    # 対象イベント取得
    try:
        e = Event.objects.get(pk=event_id)
    except Event.DoesNotExist:
        return JsonResponse({'ok': False, 'reason': 'not_found'}, status=404)

    # 作成者だけ許可（旧データ救済あり）
    is_creator = (
        (getattr(e, 'created_by', None) == user_id) or
        (not getattr(e, 'created_by', None) and getattr(e, 'scope_id', None) == user_id)
    )
    if not is_creator:
        return JsonResponse({'ok': False, 'reason': 'forbidden'}, status=403)

    # 参加者・ウェイトリスト（参加日時昇順）
    qs = e.participants.all().order_by('joined_at', 'id')
    base_participants = [{'user_id': p.user_id, 'joined_at': p.joined_at.isoformat()} for p in qs if not p.is_waiting]
    base_waitlist    = [{'user_id': p.user_id, 'joined_at': p.joined_at.isoformat()} for p in qs if p.is_waiting]

    # --- プロフィール付与（グループ/ルームのみ） ---
    profiles = {}
    scope_id = getattr(e, 'scope_id', '') or ''
    if scope_id and (scope_id.startswith('C') or scope_id.startswith('R')):  # C=Group, R=Room
        try:
            line_bot_api, _ = get_line_clients()  # 既存ヘルパで LineBotApi を得る :contentReference[oaicite:1]{index=1}
        except Exception:
            line_bot_api = None

        if line_bot_api:
            uids = {r['user_id'] for r in (base_participants + base_waitlist)}
            for uid in uids:
                try:
                    if scope_id.startswith('C'):
                        prof = line_bot_api.get_group_member_profile(scope_id, uid)
                    else:
                        prof = line_bot_api.get_room_member_profile(scope_id, uid)
                    name = getattr(prof, 'display_name', None) or getattr(prof, 'displayName', None) or ''
                    pic  = getattr(prof, 'picture_url', None)  or getattr(prof, 'pictureUrl', None)  or ''
                    profiles[uid] = {'name': name, 'pictureUrl': pic}
                except Exception:
                    # 取得失敗時は黙って素通し（IDのみの表示にフォールバック）
                    pass

    def enrich(rows):
        out = []
        for r in rows:
            prof = profiles.get(r['user_id'], {})
            out.append({
                **r,
                'name': prof.get('name', ''),
                'pictureUrl': prof.get('pictureUrl', ''),
            })
        return out

    participants = enrich(base_participants)
    waitlist     = enrich(base_waitlist)

    return JsonResponse({
        'ok': True,
        'event': {'id': e.id, 'name': e.name, 'capacity': e.capacity},
        'participants': participants,
        'waitlist': waitlist,
        'counts': {'participants': len(participants), 'waitlist': len(waitlist), 'capacity': e.capacity},
    }, status=200)


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
    signature = request.META.get('HTTP_X_LINE_SIGNATURE', '')
    body = request.body.decode('utf-8')
    logger.debug("Request body: %s", body)

    # 追加: 受信したリクエストの Host を保持（ngrok でもOK）
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
        # 念のためクリア（同一プロセス内の他リクエストへの漏れ防止）
        utils.set_request_host(None)

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

    # グループから開かれた場合、サジェスト用レジストリに登録する
    group_id = request.GET.get('groupId') or ""
    if group_id:
        try:
            from .models import KnownGroup
            obj, _ = KnownGroup.objects.get_or_create(group_id=group_id, defaults={"joined": True})
            obj.last_seen_at = timezone.now()
            # 可能なら名前・アイコンを更新（Botがグループに参加している必要あり）
            try:
                s = line_bot_api.get_group_summary(group_id)
                obj.name = getattr(s, "group_name", None) or getattr(s, "groupName", "") or obj.name
                obj.picture_url = getattr(s, "picture_url", None) or getattr(s, "pictureUrl", "") or obj.picture_url
                obj.last_summary_at = timezone.now()
            except Exception:
                pass
            obj.save()
        except Exception:
            # レジストリ更新に失敗しても画面表示は継続
            pass
        
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
            # 作成者IDを返す（一覧で「自分のイベント」UI等に使う）
            if 'created_by' in fields:
                obj['created_by'] = getattr(e, 'created_by', None)
            # 共有先ID(scope_id)も返す（編集モーダル復元やUI安定化のため）
            if 'scope_id' in fields:
                obj['scope_id'] = getattr(e, 'scope_id', None)
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

    # 3-1) 登録
    e = Event.objects.create(
        name=name,
        start_time=start_dt,
        end_time=end_dt,
        capacity=capacity,
        start_time_has_clock=start_has_clock,
        created_by=user_id,
        scope_id=scope_id,
    )


    # 3-2) イベント作成をグループに通知
    notify = bool(body.get('notify', False))
    if notify and scope_id:
        try:
            # 現在ホストからのHTTPS絶対URLを生成
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
                        { "type": "text",
                        "text": f"「{e.name}」が作成されました！",
                        "wrap": True, "weight": "bold", "size": "md" },
                        { "type": "text",
                        "text": "グループのイベントはここから見れるよ",
                        "size": "sm", "color": "#0000FF",
                        "action": { "type": "uri", "label": "ここ", "uri": liff_url } }
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


@csrf_exempt
def event_detail(request, event_id: int):
    """
    GET   : 単一イベント取得
    PATCH : イベント更新（作成と同じ入力仕様）
    DELETE: イベント削除（作成者のみ）
    すべての更新系は id_token 検証＋権限チェックを行う。
    """
    # 取得
    try:
        e = Event.objects.get(id=event_id)
    except Event.DoesNotExist:
        return JsonResponse({'ok': False, 'reason': 'not found'}, status=404)

    # GET はだれでも（同スコープ内で使う前提）
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

    # それ以外（PATCH/DELETE）は認証必須
    if request.method not in ('PATCH', 'DELETE'):
        return HttpResponseBadRequest('invalid method')

    # JSON を読む（DELETE でも body を受ける）
    try:
        body = json.loads(request.body.decode('utf-8')) if request.body else {}
    except Exception:
        return HttpResponseBadRequest('invalid json')

    id_token = (body.get('id_token') or "").strip()
    if not id_token:
        return JsonResponse({'ok': False, 'reason': 'id_token required'}, status=401)

    # id_token 検証（作成時と同じ）
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

    # 権限チェック（作成者のみ許可。将来はポリシーで拡張）
    if not policies.can_edit_event(user_id, e):
        return JsonResponse({'ok': False, 'reason': 'forbidden'}, status=403)

    # DELETE
    if request.method == 'DELETE':
        e.delete()
        return JsonResponse({'ok': True}, status=200)

    # PATCH：作成と同じ入力を解釈して上書き
    name = (body.get('name') or '').strip() or e.name

    date_str = (body.get('date') or '').strip()
    # date が渡ってきたらその日の 00:00 を新しい基準日にする
    base_dt = utils.extract_dt_from_params_date_only({'date': date_str}) if date_str else None
    start_base = base_dt or e.start_time  # start_time 未指定なら既存値ベース
    # start_time（HH:MM）が来たら合成、空文字なら「日付のみ」にする
    start_hhmm = (body.get('start_time') or None)
    if start_hhmm is None:
        # 未指定：既存の start_time を維持
        new_start = e.start_time
        start_has_clock = getattr(e, 'start_time_has_clock', True)
    elif start_hhmm == "":
        # 空文字：日付のみ（00:00のまま、has_clock=False）
        # 注意: start_base は現地TZ 00:00の aware になっている必要がある
        if not base_dt:
            # date が無いケースで「時刻だけ消す」は、既存日の 00:00 に寄せる
            base_dt = utils.extract_dt_from_params_date_only({'date': e.start_time.astimezone(timezone.get_current_timezone()).strftime('%Y-%m-%d')})
        new_start = base_dt
        start_has_clock = False
    else:
        # HH:MM を合成
        hb = base_dt or e.start_time
        new_start = utils.hhmm_to_utc_on_same_day(hb, start_hhmm)
        if new_start is None:
            return JsonResponse({'ok': False, 'reason': 'invalid start_time'}, status=400)
        start_has_clock = True

    endmode  = (body.get('endmode')  or '').strip()
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
    # else: 上書きなし（そのまま）

    cap = body.get('capacity', '__KEEP__')
    if cap == '__KEEP__':
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

    # 反映
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
def event_rsvp(request, event_id: int):
    """
    POST   : 参加する（満員ならウェイトリストに登録）
    DELETE : 参加をキャンセル（繰り上げがあれば昇格）
    必須: body.id_token
    応答例: { ok: True, status: "joined"|"waiting"|"already"|"canceled"|"not_joined",
             is_waiting: bool, confirmed_count: int, capacity: int|null, promoted_user_id: str|null }
    """
    # イベント存在チェック
    try:
        e = Event.objects.get(id=event_id)
    except Event.DoesNotExist:
        return JsonResponse({'ok': False, 'reason': 'not found'}, status=404)

    # JSON読取（DELETEでもbodyを受理）
    try:
        body = json.loads(request.body.decode('utf-8')) if request.body else {}
    except Exception:
        return HttpResponseBadRequest('invalid json')

    id_token = (body.get('id_token') or "").strip()
    if not id_token:
        return JsonResponse({'ok': False, 'reason': 'id_token required'}, status=401)

    # IDトークン検証 → user_id
    try:
        payload = _verify_id_token_internal(id_token)
        user_id = payload.get('sub') or None
        if not user_id:
            return JsonResponse({'ok': False, 'reason': 'invalid_id_token'}, status=401)
    except Exception:
        return JsonResponse({'ok': False, 'reason': 'invalid_id_token'}, status=401)

    # POST: 参加
    if request.method == 'POST':
        # 既に参加済みか
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

    # DELETE: キャンセル
    if request.method == 'DELETE':
        p = Participant.objects.filter(user_id=user_id, event=e).first()
        if not p:
            return JsonResponse({'ok': True, 'status': 'not_joined'}, status=200)

        p.delete()

        # 繰り上げ（先着のウェイトリストを昇格）
        promoted_user_id = None
        if e.capacity is not None:
            w = Participant.objects.filter(event=e, is_waiting=True).order_by('joined_at').first()
            if w:
                w.is_waiting = False
                w.save(update_fields=['is_waiting'])
                promoted_user_id = w.user_id
                # TODO: ここで主催者/昇格者への通知を送る（将来実装）

        return JsonResponse({'ok': True, 'status': 'canceled', 'promoted_user_id': promoted_user_id}, status=200)

    return JsonResponse({'ok': False, 'reason': 'method_not_allowed'}, status=405)


@csrf_exempt
def rsvp_status(request):
    """
    POST: { id_token: "...", ids: [1,2,3,...] }
    応答: { ok: True, statuses: { "<id>": {"joined": bool, "is_waiting": bool} } }
    """
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
    # 未参加イベントは joined=False を返す
    for i in ids:
        mp.setdefault(str(i), {'joined': False, 'is_waiting': False})

    return JsonResponse({'ok': True, 'statuses': mp}, status=200)


@csrf_exempt
def group_validate(request):
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

    # 1) IDトークン検証（既存の作成処理と同様の手順）
    try:
        payload = _verify_id_token_internal(id_token)  # 下の小関数を利用
        user_id = payload.get('sub') or None
    except Exception as ex:
        logger.warning("verify failed: %s", ex)
        return JsonResponse({'ok': False, 'reason': 'invalid_id_token'}, status=401)

    # 2) グループ存在＆Bot参加チェック（名前・アイコン取得）
    try:
        summary = line_bot_api.get_group_summary(group_id)  # 要：Botがグループ参加中
        group_name = getattr(summary, 'group_name', None) or getattr(summary, 'groupName', None) or ''
        picture_url = getattr(summary, 'picture_url', None) or getattr(summary, 'pictureUrl', None) or ''
    except Exception as ex:
        logger.info("get_group_summary failed: %s", ex)
        return JsonResponse({'ok': False, 'reason': 'not_joined_or_invalid'}, status=400)

    # 3) ユーザー在籍確認（任意）
    user_in_group = None
    if user_id:
        try:
            _ = line_bot_api.get_group_member_profile(group_id, user_id)
            user_in_group = True
        except Exception:
            user_in_group = False

    return JsonResponse({
        'ok': True,
        'group': {
            'id': group_id,
            'name': group_name,
            'pictureUrl': picture_url,
        },
        'user_in_group': user_in_group,
    }, status=200)


def _verify_id_token_internal(id_token: str) -> dict:
    """
    LINEのIDトークンを **LIFFチャネルID(settings.MINIAPP_CHANNEL_ID)** で検証する。
    """
    import requests
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