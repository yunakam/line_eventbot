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

from .models import Event, EventDraft
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

"""
【動作フロー】
1.「イベント作成」と入力
2. タイトル入力
3. 開始日時：日付ピッカー → 時刻入力/選択
4.「終了の指定方法」メニュー
    1) 終了時刻入力/選択　※終了日=開始日
    2) 所要時間を入力/選択
5. 定員設定（数字入力 or スキップ）
6. イベント作成完了
"""

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


@csrf_exempt
def callback(request):
    """LINEプラットフォームからのWebhookエンドポイントである。"""
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
        # --- テキストメッセージ ---
        if isinstance(ev, MessageEvent) and isinstance(ev.message, TextMessage):
            user_id = ev.source.user_id
            text = ev.message.text.strip()

            if text == "イベント作成":
                draft, _ = EventDraft.objects.get_or_create(user_id=user_id, defaults={"step": "title"})
                draft.step = "title"
                draft.name = ""
                draft.start_time = None
                draft.end_time = None
                draft.capacity = None
                draft.save()
                line_bot_api.reply_message(ev.reply_token, TextSendMessage(text="イベントのタイトルは？"))
                continue

            if EventDraft.objects.filter(user_id=user_id).exists():
                reply = handle_wizard_text(user_id, text)
                if reply:
                    line_bot_api.reply_message(ev.reply_token, reply)
                    continue

            reply_text = handle_command(text, user_id)
            if not reply_text:
                reply_text = "「イベント作成」と送ったらイベントが作れるよ！"
            line_bot_api.reply_message(ev.reply_token, TextSendMessage(text=reply_text))

        # --- Postback（ボタン押下・DatetimePickerの戻り） ---
        elif isinstance(ev, PostbackEvent):
            user_id = ev.source.user_id
            data = ev.postback.data
            params = ev.postback.params or {}

            reply = handle_wizard_postback(user_id, data, params)
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


def handle_wizard_postback(user_id: str, data: str, params: dict):
    """
    ユーザーのPostback（ボタン選択・DatetimePickerの戻り）を処理する。
    日付ピッカー、時刻候補、所要時間候補、スキップ/戻る/リセットなど。
    """
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
            return TextSendMessage(text="開始日が取得できなかったよ。もう一度選んでね")
        draft.start_time = d0  # 例: 2025-09-01 00:00 (+TZ aware)
        draft.start_time_has_clock = False
        draft.step = "start_time"
        draft.save()
        return ui.ask_time_menu("開始時刻を【HH:MM】の形で入力するか、下から選んでね", prefix="start", with_back=True, with_reset=True)

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

        # 終了時刻（候補のみ。スキップは endmode=skip を使用）
        if kind == "end" and draft.step == "end_time":
            if v == "__skip__":
                return TextSendMessage(text="終了時刻は入力するか、前の画面で『終了時刻入力をスキップ』を選んでね")

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
        # 終了日ベースに開始日と同日の00:00（ローカル）→UTCを設定
        draft.end_time = utils.hhmm_to_utc_on_same_day(draft.start_time, "00:00")
        try:
            draft.end_time_has_clock = False
        except Exception:
            pass
        draft.step = "end_time"
        draft.save()
        return ui.ask_time_menu("終了時刻を【HH:MM】の形で入力するか、下から選んでね", prefix="end", with_back=True, with_reset=True)

    # 終了の指定方法：所要時間を入力
    if data == "endmode=duration":
        try:
            draft.end_time_has_clock = False
        except Exception:
            pass
        draft.step = "duration"
        draft.save()
        return ui.ask_duration_menu(with_back=True, with_reset=True)

    # 終了スキップ
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
        return ui.ask_time_menu("開始時刻を【HH:MM】の形で入力するか、下から選んでね", prefix="start", with_back=True, with_reset=True)

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
    return TextSendMessage(text=summary)


def handle_command(text, user_id):
    """
    ウィザード外コマンドのフック（未実装なら None を返す）。
    例: 'イベント一覧', '参加:ID' など。
    """
    return None
