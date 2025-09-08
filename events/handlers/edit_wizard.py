# events/handlers/edit_wizard.py
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
    
    if draft.step == "menu":
        return ui.ask_edit_menu()

    # タイトル編集
    if draft.step == "title":
        if not text:
            return ui.msg("ask_title", qr_override=dict(show_reset=False))
        draft.name = text
        draft.step = "menu"
        draft.save()
        return ui.ask_edit_menu()

    # 開始時刻 手入力
    if draft.step == "start_time":
        new_dt = utils.hhmm_to_utc_on_same_day(draft.start_time, text)
        if new_dt is None:
            return ui.msg("invalid_time", qr_override=dict(show_reset=False))
        draft.start_time = new_dt; draft.start_time_has_clock = True; draft.step = "menu"
        draft.save()
        return ui.ask_edit_menu()

    # 終了時刻 手入力
    if draft.step == "end_time":
        new_dt = utils.hhmm_to_utc_on_same_day(draft.start_time, text)
        if new_dt is None:
            return ui.msg("invalid_time", qr_override=dict(show_reset=False))
        if draft.start_time and new_dt <= draft.start_time:
            return ui.msg("invalid_end_time", qr_override=dict(show_reset=False))
        draft.end_time = new_dt
        draft.end_time_has_clock = True
        draft.step = "menu"
        draft.save()
        return ui.ask_edit_menu()

    # 所要時間 手入力
    if draft.step == "duration":
        delta = utils.parse_duration_to_delta(text)
        if not delta or delta.total_seconds() <= 0:
            return ui.msg("invalid_duration", qr_override=dict(show_reset=False))
        draft.end_time = draft.start_time + delta; draft.end_time_has_clock = False; draft.step = "menu"
        draft.save()
        return ui.ask_edit_menu()

    # 定員編集
    if draft.step == "cap":
        capacity = utils.parse_int_safe(text)
        if capacity is None or capacity <= 0:
            return ui.msg("invalid_cap", qr_override=dict(show_reset=False))
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
        return ui.msg("ask_title", qr_override=dict(show_back=True))

    if data == "edit=start_date":
        draft.step = "start_date"
        draft.save()
        return ui.ask_date_picker(data="pick=start_date", with_reset=False)

    if data == "edit=start_time":
        draft.step = "start_time"
        draft.save()
        return ui.ask_time_menu(prefix="start", with_reset=False)

    if data == "edit=end":
        draft.step = "end_mode"
        draft.save()
        return ui.ask_end_mode_menu(with_reset=False)

    if data == "edit=cap":
        draft.step = "cap"
        draft.save()
        return ui.ask_capacity_menu(with_reset=False)
    
    if data == "edit=cancel":
        draft.delete()
        return [
            ui.msg("edit.canceled"),
            ui.ask_home_menu()
        ]

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
            return ui.msg("invalid_date", qr_override=dict(show_reset=False))
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
                return ui.msg("ask_time", qr_override=dict(show_reset=False))
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
                return ui.msg("invalid_time", qr_override=dict(show_reset=False))
            if draft.start_time and new_dt <= draft.start_time:
                return ui.msg("invalid_end_time", qr_override=dict(show_reset=False))
            draft.end_time = new_dt; draft.end_time_has_clock = True; draft.step = "menu"
            draft.save()
            return ui.ask_edit_menu()

    # 終了の指定方法（編集）
    if data == "endmode=enddt":
        draft.end_time = utils.hhmm_to_utc_on_same_day(draft.start_time, "00:00")
        draft.end_time_has_clock = False
        draft.step = "end_time"
        draft.save()
        return ui.ask_time_menu(prefix="end", with_reset=False)

    if data == "endmode=duration":
        draft.end_time_has_clock = False
        draft.step = "duration"
        draft.save()
        return ui.ask_duration_menu(with_reset=False)

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
            draft.end_time = None; draft.end_time_has_clock = False; draft.step = "menu"
            draft.save()
            return ui.ask_edit_menu()
        delta = utils.parse_duration_to_delta(code)
        if not delta or delta.total_seconds() <= 0:
            return ui.ask_duration_menu(with_reset=False)
        draft.end_time = draft.start_time + delta; draft.end_time_has_clock = False; draft.step = "menu"
        draft.save()
        return ui.ask_edit_menu()

    if data == "cap=skip" and draft.step == "cap":
        draft.capacity = None
        draft.step = "menu"
        draft.save()
        return ui.ask_edit_menu()
    
    return None
