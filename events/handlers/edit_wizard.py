# 役割: 「編集ウィザード」テキスト/ポストバックの処理を担当する

import re
from linebot.models import TextSendMessage
from ..models import Event, EventEditDraft
from .. import ui, utils

def handle_edit_text(user_id: str, text: str):
    """
    編集ウィザードでのテキスト入力を処理する。
    """    
    try:
        draft = EventEditDraft.objects.get(user_id=user_id)
    except EventEditDraft.DoesNotExist:
        return None
    
    # メニュー状態でのテキスト入力（メニュー選択の代替）
    if draft.step == "menu":
        key = (text or "").strip().lower()
        if key in ("タイトル", "title"):
            draft.step = "title"
            draft.save()
            return TextSendMessage(text="タイトルを入力してね")
        if key in ("日付", "開始日", "date", "start date"):
            draft.step = "start_date"
            draft.save()
            return ui.ask_date_picker("日付を選んでね", data="pick=start_date")
        if key in ("開始時刻", "開始時間", "start time", "time"):
            if not draft.start_time:
                return TextSendMessage(text="先に開始日を設定してね")
            draft.step = "start_time"
            return ui.ask_time_menu("開始時刻を【HH:MM】で入力するか、下から選んでね", prefix="start")
        if key in ("終了時刻", "終了", "終了の指定", "end", "end time"):
            draft.step = "end_mode"
            return ui.ask_end_mode_menu(with_back=True, with_reset=True)
        if key in ("定員", "capacity", "cap"):
            draft.step = "cap"
            draft.save()
            return ui.ask_capacity_menu(text="定員を数字で入力してね。定員なしにするなら「スキップ」を選んでね")
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

    # 開始時刻 手入力
    if draft.step == "start_time":
        new_dt = utils.hhmm_to_utc_on_same_day(draft.start_time, text)
        if new_dt is None:
            return TextSendMessage(text="時刻は HH:MM 形式で入力してね（例 09:00）")
        draft.start_time = new_dt; draft.start_time_has_clock = True; draft.step = "menu"
        draft.save()
        return ui.ask_edit_menu()

    # 終了時刻 手入力
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

    # 所要時間 手入力
    if draft.step == "duration":
        delta = utils.parse_duration_to_delta(text)
        if not delta or delta.total_seconds() <= 0:
            return TextSendMessage(text="所要時間は 1:30 / 90m / 2h / 120 などの形で入力してね。")
        if not draft.start_time:
            return TextSendMessage(text="先に開始日時を設定してね")
        draft.end_time = draft.start_time + delta; draft.end_time_has_clock = False; draft.step = "menu"
        draft.save()
        return ui.ask_edit_menu()

    # 定員編集
    if draft.step == "cap":
        capacity = utils.parse_int_safe(text)
        if capacity is None or capacity <= 0:
            return TextSendMessage(text="定員は1以上の整数を入力してね。定員なしにするなら「スキップ」を選んでね")
        draft.capacity = capacity; draft.step = "menu"
        draft.save()
        return ui.ask_edit_menu()

    return None


def handle_edit_postback(user_id: str, scope_id: str, data: str, params: dict):
    """
    編集ウィザードでのPostbackを処理する：
    　ー編集メニューの各項目と日付/時刻/所要時間/スキップ
    カルーセル導線（evt=detail/edit）は commands で扱う
    """
    try:
        draft = EventEditDraft.objects.get(user_id=user_id)
    except EventEditDraft.DoesNotExist:
        return None

    if data == "back":
        draft.step = "menu"; draft.save()
        return ui.ask_edit_menu()
    
    if data == "edit=title":
        draft.step = "title"
        draft.save()
        return TextSendMessage(
            text="タイトルを入力してね",
            quick_reply=ui.make_quick_reply(show_back=True)
        )

    if data == "edit=start_date":
        draft.step = "start_date"
        draft.save()
        return ui.ask_date_picker(
            text="日付を選んでね", 
            data="pick=start_date",
            with_back=True
        )

    if data == "edit=start_time":
        if not draft.start_time:
            return TextSendMessage(text="先に日付を設定してね")
        draft.step = "start_time"
        draft.save()
        return ui.ask_time_menu(
            text="開始時刻を HH:MM で入力するか、下から選んでね", 
            prefix="start",
            with_back=True
        )

    if data == "edit=end":
        draft.step = "end_mode"
        draft.save()
        return ui.ask_end_mode_menu(with_back=True)

    if data == "edit=cap":
        draft.step = "cap"
        draft.save()
        return ui.ask_capacity_menu(
            text="定員を数字で入力してね。定員なしにするなら「スキップ」を選んでね",
            with_back=True
        )
    
    if data == "edit=cancel":
        draft.delete()
        return TextSendMessage(text="編集を中止したよ")

    if data == "edit=save":
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
        return ui.ask_edit_menu()

    # 時刻候補
    m = re.search(r"time=(start|end)&v=([^&]+)$", data or "")
    if m:
        kind, v = m.group(1), m.group(2)

        if kind == "start" and draft.step == "start_time":
            if v == "__skip__":
                draft.start_time_has_clock = False; draft.step = "menu"
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
                return TextSendMessage(
                    text="時刻は HH:MM の形で入力するか、下から選んでね",quick_reply=ui.make_quick_reply(show_back=True)
                )
            if draft.start_time and new_dt <= draft.start_time:
                return TextSendMessage(
                    text="開始時刻よりも後の時間を設定してね",quick_reply=ui.make_quick_reply(show_back=True)
                )
            draft.end_time = new_dt; draft.end_time_has_clock = True; draft.step = "menu"
            draft.save()
            return ui.ask_edit_menu()

    # 終了の指定方法（編集）
    if data == "endmode=enddt":
        draft.end_time = utils.hhmm_to_utc_on_same_day(draft.start_time, "00:00")
        draft.end_time_has_clock = False; draft.step = "end_time"
        draft.save()
        return ui.ask_time_menu("終了時刻を HH:MM で入力するか、下から選んでね", prefix="end", with_back=True)

    if data == "endmode=duration":
        draft.end_time_has_clock = False; draft.step = "duration"
        draft.save()
        return ui.ask_duration_menu(with_back=True)

    if data == "endmode=skip":
        draft.end_time = None; draft.end_time_has_clock = False; draft.step = "menu"
        draft.save()
        return ui.ask_edit_menu()

    # 所要時間プリセット
    if data.startswith("dur=") and draft.step == "duration":
        code = data.split("=", 1)[1]
        if code == "skip":
            draft.end_time = None; draft.end_time_has_clock = False; draft.step = "menu"
            draft.save()
            return ui.ask_edit_menu()
        delta = utils.parse_duration_to_delta(code)
        if not delta or delta.total_seconds() <= 0:
            return TextSendMessage(
                text="所要時間を入力するか、下から選んでね\n入力例： 1:30 / 90m / 2h / 120",
                quick_reply=ui.make_quick_reply(show_back=True)
            )
        draft.end_time = draft.start_time + delta; draft.end_time_has_clock = False; draft.step = "menu"
        draft.save()
        return ui.ask_edit_menu()

    if data == "cap=skip" and draft.step == "cap":
        draft.capacity = None
        draft.step = "menu"
        draft.save()
        return ui.ask_edit_menu()
    
    return None
