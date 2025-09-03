# events/views.py
import os
import re

from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.core.exceptions import ImproperlyConfigured
from django.conf import settings
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from datetime import timedelta, datetime

from .models import Event, EventDraft, EventEditDraft
from . import ui, utils

from linebot import LineBotApi, WebhookParser
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    PostbackEvent,
    DatetimePickerAction, TemplateSendMessage, PostbackAction
)
from linebot.exceptions import InvalidSignatureError

import logging
logger = logging.getLogger(__name__)


def get_line_clients():
    """
    環境変数やsettingsからアクセストークン／チャネルシークレットを読み
    LINE SDKクライアントを返す
    """
    token  = getattr(settings, "LINE_CHANNEL_ACCESS_TOKEN", None) or os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    secret = getattr(settings, "LINE_CHANNEL_SECRET", None)       or os.getenv("LINE_CHANNEL_SECRET")

    if not token or not secret:
        raise ImproperlyConfigured("LINEのトークン/シークレットが未設定である。")

    return LineBotApi(token), WebhookParser(secret)


def _handle_evt_shortcut(user_id: str, scope_id: str, data: str):
    """
    一覧Carouselなどからのショートカット（evt=detail / evt=edit）を処理する。
    - detail: そのまま詳細を返す
    - edit  : ドラフトを作成して編集メニューへ
    """
    m = re.search(r"evt=(detail|edit)&id=(\d+)", data)
    if not m:
        return None
    kind, eid = m.group(1), int(m.group(2))
    
    try:
        e = Event.objects.get(id=eid, scope_id=scope_id)
    except Event.DoesNotExist:
        return TextSendMessage(text="該当するイベントが見つからないよ")

    if kind == "detail":
        # 確認用の詳細メッセージをそのまま返す
        return ui.build_event_summary(e)

    # kind == "edit": 編集権限チェック（作成者のみ） → OKなら編集ドラフト作成
    if e.created_by != user_id:
        return TextSendMessage(text="イベントの作成者だけが編集できるよ")
    
    EventEditDraft.objects.update_or_create(
        user_id=user_id,
        defaults={
            "event": e,
            "scope_id": scope_id,
            "step": "menu",
            "name": e.name,
            "start_time": e.start_time,
            "start_time_has_clock": getattr(e, "start_time_has_clock", True),
            "end_time": e.end_time,
            "end_time_has_clock": True,
            "capacity": e.capacity,
        }
    )
    return ui.ask_edit_menu()


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

    # --- Line SDK クライアントを取得 ---
    line_bot_api, parser = get_line_clients()

    # --- LINE 署名検証＆イベントパース ---
    signature = request.headers.get("X-Line-Signature", "")
    body = request.body.decode("utf-8")
    try:
        events = parser.parse(body, signature)
    except InvalidSignatureError:
        return HttpResponse(status=400)

    # --- 受信イベントを順次処理 ---
    for ev in events:
        scope_id = _resolve_scope_id(ev) 

        # =================================
        # 1) 通常メッセージ（テキスト）を受信
        # =================================
        if isinstance(ev, MessageEvent) and isinstance(ev.message, TextMessage):
            user_id = ev.source.user_id
            text = ev.message.text.strip()

            # 1-1) 「編集ドラフトがあるなら」編集テキストハンドラを優先
            if EventEditDraft.objects.filter(user_id=user_id).exists():
                reply = handle_edit_text(user_id, text)
                if reply:
                    line_bot_api.reply_message(ev.reply_token, reply)
                    continue

            # 1-2) 「イベント作成」開始コマンド
            if text == "イベント作成":
                draft, _ = EventDraft.objects.get_or_create(user_id=user_id, defaults={"step": "title"})
                draft.step = "title"
                draft.name = ""
                draft.start_time = None
                draft.start_time_has_clock = False
                draft.end_time = None
                draft.end_time_has_clock = False
                draft.capacity = None
                draft.scope_id = scope_id
                draft.save()
                line_bot_api.reply_message(ev.reply_token, TextSendMessage(text="イベントのタイトルは？"))
                continue

            # 1-3) 進行中の「作成ウィザード」テキスト処理
            if EventDraft.objects.filter(user_id=user_id).exists():
                reply = handle_wizard_text(user_id, text)
                if reply:
                    line_bot_api.reply_message(ev.reply_token, reply)
                    continue

            # 1-4) 通常コマンド（一覧/詳細:ID 等）
            reply_obj = handle_command(text, user_id, scope_id)
            if reply_obj:
                # 送信可能な型はそのまま送る（TextSendMessage / TemplateSendMessage / list）
                if isinstance(reply_obj, (TextSendMessage, TemplateSendMessage, list)):
                    line_bot_api.reply_message(ev.reply_token, reply_obj)
                # 文字列のみ TextSendMessage にラップ
                elif isinstance(reply_obj, str):
                    line_bot_api.reply_message(ev.reply_token, TextSendMessage(text=reply_obj))
            else:
                # それ以外はホームメニューへ
                line_bot_api.reply_message(ev.reply_token, ui.ask_home_menu())

        # =================================================
        # 2) Postback（ボタン押下・DatetimePicker戻り）を受信
        # =================================================
        elif isinstance(ev, PostbackEvent):
            user_id = ev.source.user_id
            data = ev.postback.data or ""       # ← None 安全化
            params = ev.postback.params or {}

            # 一覧カルーセル等からの evt=detail/edit を最優先で処理（scope渡す）
            shortcut = _handle_evt_shortcut(user_id, scope_id, data)
            if shortcut:
                line_bot_api.reply_message(ev.reply_token, shortcut)
                continue

            # 2-1) 編集ドラフトがあるなら編集ポストバックを優先
            if EventEditDraft.objects.filter(user_id=user_id).exists():
                reply = handle_edit_postback(user_id, scope_id, data, params)
                if reply:
                    line_bot_api.reply_message(ev.reply_token, reply)
                    continue

            # 2-2) 作成ウィザード（ドラフト前提）のポストバック処理
            reply = handle_wizard_postback(user_id, data, params, scope_id)
            if reply:
                line_bot_api.reply_message(ev.reply_token, reply)

    return HttpResponse(status=200)


def handle_wizard_text(user_id: str, text: str):
    """
    ユーザーのテキスト入力を処理する。
    タイトル、開始時刻の手入力、終了時刻の手入力、所要時間、定員。
    """
    try:
        draft = EventDraft.objects.get(user_id=user_id)
    except EventDraft.DoesNotExist:
        return None

    # タイトル → 開始日ピッカーへ
    if draft.step == "title":
        if not text:
            return TextSendMessage(text="イベントのタイトルを入力してね")
        draft.name = text
        draft.step = "start_date"
        draft.save()
        return ui.ask_date_picker("イベントの日付を教えてね", data="pick=start_date", with_back=True)

    # 開始時刻の手入力（HH:MM）
    if draft.step == "start_time":
        new_dt = utils.hhmm_to_utc_on_same_day(draft.start_time, text)
        if new_dt is None:
            return TextSendMessage(text="時刻は HH:MM 形式で入力してね（例 09:00）")
        draft.start_time = new_dt
        draft.start_time_has_clock = True
        draft.step = "end_mode"
        draft.save()
        return ui.ask_end_mode_menu(with_back=True, with_reset=True)

    # 終了時刻の手入力（HH:MM）
    if draft.step == "end_time":
        new_dt = utils.hhmm_to_utc_on_same_day(draft.start_time, text)
        if new_dt is None:
            return TextSendMessage(text="時刻は HH:MM 形式で入力してね（例 19:00）")
        if draft.start_time and new_dt <= draft.start_time:
            return TextSendMessage(text="終了が開始より前（同時刻含む）になっているよ。もう一度入力してね")
        draft.end_time = new_dt
        # フラグ：終了は時計入力/選択である
        try:
            draft.end_time_has_clock = True
        except Exception:
            pass
        draft.step = "cap"
        draft.save()
        return ui.ask_capacity_menu(with_back=True, with_reset=True)

    # 所要時間の手入力
    if draft.step == "duration":
        delta = utils.parse_duration_to_delta(text)
        if not delta or delta.total_seconds() <= 0:
            return TextSendMessage(text="所要時間は 1:30 / 90m / 2h / 120 などで入力してね")
        if not draft.start_time:
            return TextSendMessage(text="先に開始日時を選んでね")
        draft.end_time = draft.start_time + delta
        try:
            draft.end_time_has_clock = False
        except Exception:
            pass
        draft.step = "cap"
        draft.save()
        return ui.ask_capacity_menu(with_back=True, with_reset=True)

    # 定員の数値入力
    if draft.step == "cap":
        capacity = utils.parse_int_safe(text)
        if capacity is None or capacity <= 0:
            return TextSendMessage(text="定員は1以上の整数を入力してね。定員なしにするなら「スキップ」を選んでね")
        draft.capacity = capacity
        draft.step = "done"
        draft.save()
        return finalize_event(draft)

    return None


def handle_wizard_postback(user_id: str, data: str, params: dict, scope_id: str):
    """
    作成ウィザードのPostback（ボタン選択・DatetimePickerの戻り）を処理する。
    （日付ピッカー、時刻候補、所要時間候補、スキップ/戻る/リセットなど）
    """

    # ---- 1) ホームメニュー（ドラフトの有無に関係なく動く） ---- 
    if data == "home=create":
        # 作成ウィザードを初期化してタイトル入力へ
        draft, _ = EventDraft.objects.get_or_create(user_id=user_id, defaults={"step": "title"})
        draft.step = "title"
        draft.name = ""
        draft.start_time = None
        draft.end_time = None
        draft.capacity = None
        try:
            draft.end_time_has_clock = False
        except Exception:
            pass
        draft.scope_id = scope_id
        draft.save()
        return TextSendMessage(text="イベントのタイトルは？")

    if data == "home=list":
        qs = Event.objects.filter(scope_id=scope_id).order_by("-id")[:10]
        if hasattr(ui, "build_event_list_carousel"):
            return ui.build_event_list_carousel(qs)
        else:
            if not qs:
                return TextSendMessage(text="作成したイベントはまだないよ")
            lines = [f"{e.id}: {e.name}" for e in qs]
            return TextSendMessage(text="イベント一覧:\n" + "\n".join(lines))

    if data == "home=help":
        return TextSendMessage(
            text="イベント作成や編集はメニューから操作できる。作成→タイトル→日付→開始時刻→終了の指定→定員の順で進む。"
        )

    # ---- 2) 作成ウィザード（ドラフト前提）の処理 ---- 
    try:
        draft = EventDraft.objects.get(user_id=user_id)
    except EventDraft.DoesNotExist:
        return None

    if data == "back":
        return _go_back_one_step(draft)

    if data == "reset":
        draft.step = "title"
        draft.name = ""
        draft.start_time = None
        draft.end_time = None
        draft.capacity = None
        try:
            draft.end_time_has_clock = False
        except Exception:
            pass
        draft.save()
        return TextSendMessage(text="最初からやり直すよ。\nイベントのタイトルは？")

    logger.debug("wizard postback", extra={"step": draft.step, "data": data})

    # 開始日 選択（DatetimePicker: mode='date'）
    if data == "pick=start_date" and draft.step == "start_date":
        d0 = utils.extract_dt_from_params_date_only(params)
        if not d0:
            return TextSendMessage(text="日付が取得できなかったよ。もう一度選んでね")
        draft.start_time = d0  # 例: 2025-09-01 00:00 (+TZ aware)
        draft.start_time_has_clock = False
        draft.step = "start_time"
        draft.save()
        return ui.ask_time_menu(
            "開始時刻を HH:MM の形で入力するか、下から選んでね",
            prefix="start",
            with_back=True,
            with_reset=True
        )

    # 時刻候補のPostback（例: data="time=start&v=09:00"）
    m = re.search(r"time=(start|end)&v=([^&]+)$", data)
    if m:
        kind, v = m.group(1), m.group(2)

        # 開始時刻（候補/スキップ）
        if kind == "start" and draft.step == "start_time":
            if v == "__skip__":
                draft.start_time_has_clock = False
                draft.step = "end_mode"
                draft.save()
                return ui.ask_end_mode_menu(with_back=True, with_reset=True)

            new_dt = utils.hhmm_to_utc_on_same_day(draft.start_time, v)
            if new_dt is None:
                return TextSendMessage(text="時刻の形式が不正だよ。もう一度選ぶか「HH:MM」で入力してね")
            draft.start_time = new_dt
            draft.start_time_has_clock = True
            draft.step = "end_mode"
            draft.save()
            return ui.ask_end_mode_menu(with_back=True, with_reset=True)

        # 終了時刻（候補/スキップの両方を許可）
        if kind == "end" and draft.step == "end_time":
            if v == "__skip__":
                # 終了入力をスキップ → 定員入力へ
                draft.end_time = None
                try:
                    draft.end_time_has_clock = False
                except Exception:
                    pass
                draft.step = "cap"
                draft.save()
                return ui.ask_capacity_menu(with_back=True, with_reset=True)

            new_dt = utils.hhmm_to_utc_on_same_day(draft.start_time, v)
            if new_dt is None:
                return TextSendMessage(text="時刻の形式が不正だよ。もう一度選ぶか「HH:MM」で入力してね")
            if draft.start_time and new_dt <= draft.start_time:
                return TextSendMessage(text="開始時刻よりも後の時間を設定してね")

            draft.end_time = new_dt
            try:
                draft.end_time_has_clock = True
            except Exception:
                pass
            draft.step = "cap"
            draft.save()
            return ui.ask_capacity_menu(with_back=True, with_reset=True)

    # 終了の指定方法：終了時刻を入力
    if data == "endmode=enddt":
        draft.end_time = utils.hhmm_to_utc_on_same_day(draft.start_time, "00:00")
        try:
            draft.end_time_has_clock = False
        except Exception:
            pass
        draft.step = "end_time"
        draft.save()
        return ui.ask_time_menu(
            "終了時刻を HH:MM の形で入力するか、下から選んでね",
            prefix="end",
            with_back=True,
            with_reset=True
        )

    # 終了の指定方法：所要時間を入力
    if data == "endmode=duration":
        try:
            draft.end_time_has_clock = False
        except Exception:
            pass
        draft.step = "duration"
        draft.save()
        return ui.ask_duration_menu(with_back=True, with_reset=True)

    # 終了スキップ → 定員入力へ（cap）
    if data == "endmode=skip":
        draft.end_time = None
        try:
            draft.end_time_has_clock = False
        except Exception:
            pass
        draft.step = "cap"
        draft.save()
        return ui.ask_capacity_menu(with_back=True, with_reset=True)

    # 所要時間プリセット or スキップ
    if data.startswith("dur=") and draft.step == "duration":
        code = data.split("=", 1)[1]

        # スキップ
        if code == "skip":
            draft.end_time = None
            try:
                draft.end_time_has_clock = False
            except Exception:
                pass
            draft.step = "cap"
            draft.save()
            return ui.ask_capacity_menu(with_back=True, with_reset=True)

        # プリセット（例: 30m/60m/90m）
        delta = utils.parse_duration_to_delta(code)
        if not delta or delta.total_seconds() <= 0:
            return TextSendMessage(text="所要時間の形式が不正だよ。もう一度選んでね")
        draft.end_time = draft.start_time + delta
        try:
            draft.end_time_has_clock = False
        except Exception:
            pass
        draft.step = "cap"
        draft.save()
        return ui.ask_capacity_menu(with_back=True, with_reset=True)

    # 定員スキップ
    if data == "cap=skip" and draft.step == "cap":
        draft.capacity = None
        draft.step = "done"
        draft.save()
        return finalize_event(draft)

    return None


# 編集ウィザードのテキスト入力を処理
def handle_edit_text(user_id: str, text: str):
    try:
        draft = EventEditDraft.objects.get(user_id=user_id)
    except EventEditDraft.DoesNotExist:
        return None

    # メニュー状態でのテキスト→Postback相当の解釈
    if draft.step == "menu":
        key = (text or "").strip().lower()
        # 代表的な揺れを吸収
        if key in ("タイトル", "title"):
            draft.step = "title"; draft.save()
            return TextSendMessage(text="タイトルを入力してね")
        if key in ("日付", "開始日", "date", "start date"):
            draft.step = "start_date"; draft.save()
            return ui.ask_date_picker("日付を選んでくれ。", data="pick=start_date", with_back=True, with_reset=True)
        if key in ("開始時刻", "開始時間", "start time", "time"):
            if not draft.start_time:
                return TextSendMessage(text="先に開始日を設定してね")
            draft.step = "start_time"; draft.save()
            return ui.ask_time_menu("開始時刻を【HH:MM】で入力するか、下から選んでね", prefix="start", with_back=True, with_reset=True)
        if key in ("終了時刻", "終了", "終了の指定", "end", "end time"):
            draft.step = "end_mode"; draft.save()
            return ui.ask_end_mode_menu(with_back=True, with_reset=True)
        if key in ("定員", "capacity", "cap"):
            draft.step = "cap"; draft.save()
            return ui.ask_capacity_menu(text="定員を数字で入力してね。定員なしにするなら「スキップ」を選んでね", with_back=True, with_reset=True)
        if key in ("保存", "編集を保存", "save"):
            e = draft.event
            e.name = draft.name or e.name
            e.start_time = draft.start_time or e.start_time
            e.start_time_has_clock = draft.start_time_has_clock if draft.start_time is not None else getattr(e, "start_time_has_clock", True)
            e.end_time = draft.end_time
            e.capacity = draft.capacity if draft.capacity is not None else e.capacity
            e.save()
            msg = ui.build_event_summary(e, end_has_clock=draft.end_time_has_clock)
            draft.delete()
            return [TextSendMessage(text="編集内容を保存したよ！"), msg]
        if key in ("中止", "編集を中止", "cancel"):
            draft.delete()
            return TextSendMessage(text="編集を中止したよ")

        # どれにも当たらなければメニューを出し直す
        return None


    # タイトル編集
    if draft.step == "title":
        if not text:
            return TextSendMessage(text="タイトルを入力してね")
        draft.name = text
        draft.step = "menu"
        draft.save()
        return ui.ask_edit_menu()

    # 開始時刻編集（手入力）
    if draft.step == "start_time":
        new_dt = utils.hhmm_to_utc_on_same_day(draft.start_time, text)
        if new_dt is None:
            return TextSendMessage(text="時刻は HH:MM 形式で入力してね（例 09:00）")
        draft.start_time = new_dt
        draft.start_time_has_clock = True
        draft.step = "menu"
        draft.save()
        return ui.ask_edit_menu()

    # 終了時刻編集（手入力）
    if draft.step == "end_time":
        new_dt = utils.hhmm_to_utc_on_same_day(draft.start_time, text)
        if new_dt is None:
            return TextSendMessage(text="時刻は HH:MM 形式で入力してね（例 19:00）")
        if draft.start_time and new_dt <= draft.start_time:
            return TextSendMessage(text="開始時刻よりも後の時間を入力してね")
        draft.end_time = new_dt
        draft.end_time_has_clock = True
        draft.step = "menu"
        draft.save()
        return ui.ask_edit_menu()

    # 所要時間編集（手入力）
    if draft.step == "duration":
        delta = utils.parse_duration_to_delta(text)
        if not delta or delta.total_seconds() <= 0:
            return TextSendMessage(text="所要時間は 1:30 / 90m / 2h / 120 などの形で入力してね。")
        if not draft.start_time:
            return TextSendMessage(text="先に開始日時を設定してね")
        draft.end_time = draft.start_time + delta
        draft.end_time_has_clock = False
        draft.step = "menu"
        draft.save()
        return ui.ask_edit_menu()

    # 定員編集
    if draft.step == "cap":
        capacity = utils.parse_int_safe(text)
        if capacity is None or capacity <= 0:
            return TextSendMessage(text="定員は1以上の整数を入力してね。定員なしにするなら「スキップ」を選んでね")
        draft.capacity = capacity
        draft.step = "menu"
        draft.save()
        return ui.ask_edit_menu()

    # それ以外のステップはメニューに戻す
    return None


# 編集ウィザードのPostbackを処理
def handle_edit_postback(user_id: str, scope_id: str, data: str, params: dict):
    try:
        draft = EventEditDraft.objects.get(user_id=user_id)
    except EventEditDraft.DoesNotExist:
        return None

    # イベント一覧からの導線: evt=detail / evt=edit
    m = re.search(r"evt=(detail|edit)&id=(\d+)", data)
    if m:
        kind, eid = m.group(1), int(m.group(2))
        try:
            e = Event.objects.get(id=eid, created_by=user_id)
        except Event.DoesNotExist:
            return TextSendMessage(text="イベントが見つからないよ（または編集権限がないよ）")
        if kind == "detail":
            return ui.build_event_summary(e)
        if kind == "edit":
            # 編集開始（ドラフト上書き）
            draft.event = e
            draft.step = "menu"
            draft.name = e.name
            draft.start_time = e.start_time
            draft.start_time_has_clock = getattr(e, "start_time_has_clock", True)
            draft.end_time = e.end_time
            draft.end_time_has_clock = True
            draft.capacity = e.capacity
            draft.save()
            return ui.ask_edit_menu()

    # ---- 編集メニューの各項目 ----
    if data == "edit=title":
        draft.step = "title"
        draft.save()
        return TextSendMessage(text="新しいタイトルを入力してね")

    if data == "edit=start_date":
        draft.step = "start_date"
        draft.save()
        return ui.ask_date_picker(
            text="新しい日付を選んでね",
            data="pick=start_date",
            with_back=True,
            with_reset=True
        )

    if data == "edit=start_time":
        # すでに開始日(draft.start_time)がある前提
        if not draft.start_time:
            return TextSendMessage(text="先に日付を設定してね")
        draft.step = "start_time"
        draft.save()
        return ui.ask_time_menu(text="開始時刻を HH:MM で入力するか、下から選んでね", prefix="start")

    if data == "edit=end":
        draft.step = "end_mode"
        draft.save()
        return ui.ask_end_mode_menu()

    if data == "edit=cap":
        draft.step = "cap"
        draft.save()
        return ui.ask_capacity_menu(text="定員を数字で入力してね。定員なしにするなら「スキップ」を選んでね")

    if data == "edit=cancel":
        draft.delete()
        return TextSendMessage(text="編集を中止したよ")

    if data == "edit=save":
        # ドラフト内容を本体Eventへ反映
        e = draft.event
        e.name = draft.name or e.name
        e.start_time = draft.start_time or e.start_time
        e.start_time_has_clock = draft.start_time_has_clock if draft.start_time is not None else getattr(e, "start_time_has_clock", True)
        e.end_time = draft.end_time
        e.capacity = draft.capacity if draft.capacity is not None else e.capacity
        e.save()
        msg = ui.build_event_summary(e, end_has_clock=draft.end_time_has_clock)
        draft.delete()
        return [TextSendMessage(text="編集内容を保存したよ！"), msg]

    # 日付ピッカー
    if data == "pick=start_date" and draft.step == "start_date":
        d0 = utils.extract_dt_from_params_date_only(params)
        if not d0:
            return TextSendMessage(text="開始日がわからなかったよ。もう一度選んでね")
        draft.start_time = d0
        draft.start_time_has_clock = False
        draft.step = "start_time"
        draft.save()
        return ui.ask_time_menu("開始時刻を HH:MM で入力するか、下から選んでね", prefix="start", with_back=True, with_reset=True)

    # 時刻候補（開始/終了）
    m = re.search(r"time=(start|end)&v=([^&]+)$", data)
    if m:
        kind, v = m.group(1), m.group(2)

        if kind == "start" and draft.step == "start_time":
            if v == "__skip__":
                draft.start_time_has_clock = False
                draft.step = "menu"
                draft.save()
                return ui.ask_edit_menu()

            new_dt = utils.hhmm_to_utc_on_same_day(draft.start_time, v)
            if new_dt is None:
                return TextSendMessage(text="時刻は HH:MM の形で入力するか、下から選んでね")
            draft.start_time = new_dt
            draft.start_time_has_clock = True
            draft.step = "menu"
            draft.save()
            return ui.ask_edit_menu()

        if kind == "end" and draft.step == "end_time":
            if v == "__skip__":
                return ui.ask_edit_menu()
            new_dt = utils.hhmm_to_utc_on_same_day(draft.start_time, v)
            if new_dt is None:
                return TextSendMessage(text="時刻は HH:MM の形で入力するか、下から選んでね")
            if draft.start_time and new_dt <= draft.start_time:
                return TextSendMessage(text="開始時刻よりも後の時間を設定してね")
            draft.end_time = new_dt
            draft.end_time_has_clock = True
            draft.step = "menu"
            draft.save()
            return ui.ask_edit_menu()

    # 終了の指定方法（編集）
    if data == "endmode=enddt":
        draft.end_time = utils.hhmm_to_utc_on_same_day(draft.start_time, "00:00")
        draft.end_time_has_clock = False
        draft.step = "end_time"
        draft.save()
        return ui.ask_time_menu("終了時刻を HH:MM で入力するか、下から選んでね", prefix="end")

    if data == "endmode=duration":
        draft.end_time_has_clock = False
        draft.step = "duration"
        draft.save()
        return ui.ask_duration_menu()

    if data == "endmode=skip":
        draft.end_time = None
        draft.end_time_has_clock = False
        draft.step = "menu"
        draft.save()
        return ui.ask_edit_menu()

    # 所要時間プリセット
    if data.startswith("dur=") and draft.step == "duration":
        code = data.split("=", 1)[1]
        if code == "skip":
            draft.end_time = None
            draft.end_time_has_clock = False
            draft.step = "menu"
            draft.save()
            return ui.ask_edit_menu()

        delta = utils.parse_duration_to_delta(code)
        if not delta or delta.total_seconds() <= 0:
            return TextSendMessage(text="""所要時間を入力するか、下から選んでね\n
                                   入力例： 1:30 / 90m / 2h / 120 で入力するか""")
        draft.end_time = draft.start_time + delta
        draft.end_time_has_clock = False
        draft.step = "menu"
        draft.save()
        return ui.ask_edit_menu()

    return None


# ===== ドメイン処理 =====

def _go_back_one_step(draft: "EventDraft"):
    """
    現在のdraft.stepから一段階前に戻し、必要なフィールドを巻き戻した上で
    適切なメニューを返す。
    """
    if draft.step == "title":
        return [
            TextSendMessage(text="これ以上は戻れないよ"),
            TextSendMessage(text="イベントのタイトルは？"),
        ]

    if draft.step == "start_date":
        draft.step = "title"
        draft.save()
        return TextSendMessage(text="イベントのタイトルは？")

    if draft.step == "start_time":
        draft.start_time = None
        draft.step = "start_date"
        draft.save()
        return ui.ask_date_picker("イベントの日付を教えてね", data="pick=start_date", with_back=True, with_reset=True)

    if draft.step == "end_mode":
        draft.end_time = None
        draft.capacity = None
        try:
            draft.end_time_has_clock = False
        except Exception:
            pass
        draft.step = "start_time"
        draft.save()
        return ui.ask_time_menu("開始時刻を HH:MM の形で入力するか、下から選んでね", prefix="start", with_back=True, with_reset=True)

    if draft.step == "end_time":
        draft.end_time = None
        try:
            draft.end_time_has_clock = False
        except Exception:
            pass
        draft.step = "end_mode"
        draft.save()
        return ui.ask_end_mode_menu(with_back=True, with_reset=True)

    if draft.step == "duration":
        draft.end_time = None
        try:
            draft.end_time_has_clock = False
        except Exception:
            pass
        draft.step = "end_mode"
        draft.save()
        return ui.ask_end_mode_menu(with_back=True, with_reset=True)

    if draft.step == "cap":
        draft.capacity = None
        draft.step = "end_mode"
        draft.save()
        return ui.ask_end_mode_menu(with_back=True, with_reset=True)

    return TextSendMessage(text="これ以上は戻れないよ")


def finalize_event(draft: "EventDraft"):
    """
    Draft を Event へ確定し、要約メッセージを現在TZで整形して返す。
    """
    e = Event.objects.create(
        name=draft.name,
        start_time=draft.start_time,
        end_time=draft.end_time,
        capacity=draft.capacity,
        start_time_has_clock=draft.start_time_has_clock,
        created_by=draft.user_id,
        scope_id=draft.scope_id, 
    )

    start_text = utils.local_fmt(e.start_time, getattr(e, "start_time_has_clock", True))

    if e.end_time is None:
        end_text = "終了時間: （未設定）"
    else:
        # 終了が時計入力/選択であれば絶対時刻表示
        end_has_clock = getattr(draft, "end_time_has_clock", False)
        if end_has_clock:
            end_text = f"終了時間: {utils.local_fmt(e.end_time, True)}"  # "yyyy-MM-dd HH:MM"
        elif not getattr(e, "start_time_has_clock", True):
            # 開始に時計がなく、終了は所要時間方式
            mins = int((e.end_time - e.start_time).total_seconds() // 60)
            end_text = f"所要時間: {utils.minutes_humanize(mins)}"
        else:
            end_text = utils.local_fmt(e.end_time, True)

    cap_text = "定員なし" if e.capacity is None else f"定員: {e.capacity}"

    summary = (
        "イベントを作成したよ！\n"
        f"ID: {e.id}\n"
        f"タイトル: {e.name}\n"
        f"開始: {start_text}\n"
        f"{end_text}\n"
        f"{cap_text}"
    )
    
    msg = TextSendMessage(text=summary)
    draft.delete()  # 確定後はドラフトを掃除
    return msg

# ---- テキストコマンドを処理（一覧／詳細／編集開始）----
def handle_command(text: str, user_id: str, scope_id: str):
    text = (text or "").strip()

    # 1) 一覧（グループ＝scope_id 全体を対象）
    if text == "イベント一覧":
        qs = Event.objects.filter(scope_id=scope_id).order_by("-id")[:5]
        if not qs:
            return "作成されたイベントがないよ"

        lines = [f"{e.id}: {e.name}" for e in qs]
        return "イベント一覧（直近5件）：\n" + "\n".join(lines) + \
               "\n\nイベントの詳細を見る→「イベント詳細:ID」\nイベントを編集する→『編集:ID』"

    # 2) 詳細（イベント詳細:3）
    m = re.fullmatch(r"イベント詳細[:：]\s*(\d+)", text)
    if m:
        eid = int(m.group(1))
        try:
            e = Event.objects.get(id=eid, scope_id=scope_id)
        except Event.DoesNotExist:
            # 文字列ではなく TextSendMessage を返す（型を揃える）
            return TextSendMessage(text="イベントが見つからないよ（または編集権限がないよ）")

        msg = ui.build_event_summary(e)
        return msg

    # 3) 編集開始（編集:3）
    m = re.fullmatch(r"編集[:：]\s*(\d+)", text)
    if m:
        eid = int(m.group(1))
        try:
            e = Event.objects.get(id=eid, created_by=user_id)
        except Event.DoesNotExist:
            return "イベントが見つからないよ（または編集権限がないよ）"
        # 編集ドラフトを作成/初期化
        from .models import EventEditDraft
        draft, _ = EventEditDraft.objects.update_or_create(
            user_id=user_id,
            defaults={
                "event": e,
                "step": "menu",
                "name": e.name,
                "start_time": e.start_time,
                "start_time_has_clock": getattr(e, "start_time_has_clock", True),
                "end_time": e.end_time,
                "end_time_has_clock": True,
                "capacity": e.capacity,
            }
        )
        return ui.ask_edit_menu()
    return None

