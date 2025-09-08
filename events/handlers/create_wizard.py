# events/handlers/create_wizard.py
# 役割: 「作成ウィザード」テキスト/ポストバックの処理を担当する

import re
import logging
from linebot.models import TextSendMessage
from ..models import Event, EventDraft, EventEditDraft
from .. import ui, utils

logger = logging.getLogger(__name__)

# --- ドラフトを1段階戻す ---
def _go_back_one_step(draft: "EventDraft"):
    """
    現在のdraft.stepから一段階前に戻し、必要なフィールドを巻き戻した上で
    適切なメニューを返す。
    """
    if draft.step == "title":
        return [
            TextSendMessage(text="これ以上は戻れないよ"),
            ui.msg("ask_title"),
        ]
        
    if draft.step == "start_date":
        draft.step = "title"; draft.save()
        return ui.msg("ask_title")

    if draft.step == "start_time":
        draft.start_time = None; draft.step = "start_date"; draft.save()
        return ui.ask_date_picker(data="pick=start_date")
    
    if draft.step == "end_mode":
        draft.end_time = None; draft.capacity = None
        try: draft.end_time_has_clock = False
        except Exception: pass
        draft.step = "start_time"; draft.save()
        return ui.ask_time_menu(prefix="start")
    
    if draft.step == "end_time":
        draft.end_time = None
        try: draft.end_time_has_clock = False
        except Exception: pass
        draft.step = "end_mode"; draft.save()
        return ui.ask_end_mode_menu()
    
    if draft.step == "duration":
        draft.end_time = None
        try: draft.end_time_has_clock = False
        except Exception: pass
        draft.step = "end_mode"; draft.save()
        return ui.ask_end_mode_menu()
    
    if draft.step == "cap":
        draft.capacity = None; draft.step = "end_mode"; draft.save()
        return ui.ask_end_mode_menu()

    return TextSendMessage(text="これ以上は戻れないよ")


# --- 確定処理 ---
def _finalize_event(draft: "EventDraft"):
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
        end_has_clock = getattr(draft, "end_time_has_clock", False)
        if end_has_clock:
            end_text = f"終了時間: {utils.local_fmt(e.end_time, True)}"
        elif not getattr(e, "start_time_has_clock", True):
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

    draft.delete()
    
    msg = TextSendMessage(text=summary, quick_reply=ui.make_quick_reply())
    return msg


# --- テキスト処理 ---
def handle_wizard_text(user_id: str, text: str):
    """
    ユーザーのテキスト入力を処理する。
    タイトル、開始時刻の手入力、終了時刻の手入力、所要時間、定員。
    """
    try:
        draft = EventDraft.objects.get(user_id=user_id)
    except EventDraft.DoesNotExist:
        return None

    # タイトル → 開始日
    if draft.step == "title":
        if not text:
            ui.msg("ask_title")
        draft.name = text; draft.step = "start_date"
        draft.save()
        return ui.ask_date_picker(data="pick=start_date")

    # 開始時刻 手入力
    if draft.step == "start_time":
        new_dt = utils.hhmm_to_utc_on_same_day(draft.start_time, text)
        if new_dt is None:
            return ui.msg("invalid_time")
        draft.start_time = new_dt
        draft.start_time_has_clock = True
        draft.step = "end_mode"
        draft.save()
        return ui.ask_end_mode_menu()

    # 終了時刻 手入力
    if draft.step == "end_time":
        new_dt = utils.hhmm_to_utc_on_same_day(draft.start_time, text)
        if new_dt is None:
            return ui.msg("invalid_time")
        if draft.start_time and new_dt <= draft.start_time:
            return ui.msg("invalid_end_time")
        draft.end_time = new_dt
        try: draft.end_time_has_clock = True
        except Exception: pass
        draft.step = "cap"; draft.save()
        return ui.ask_capacity_menu()

    # 所要時間 手入力
    if draft.step == "duration":
        delta = utils.parse_duration_to_delta(text)
        if not delta or delta.total_seconds() <= 0:
            return ui.msg("invalid_duration")
        draft.end_time = draft.start_time + delta
        try: draft.end_time_has_clock = False
        except Exception: pass
        draft.step = "cap"
        draft.save()
        return ui.ask_capacity_menu()

    # 定員 手入力
    if draft.step == "cap":
        capacity = utils.parse_int_safe(text)
        if capacity is None or capacity <= 0:
            return ui.msg("invalid_cap")
        draft.capacity = capacity
        draft.step = "done"
        draft.save()
        return _finalize_event(draft)

    return None


# --- ポストバック処理 ---
def handle_wizard_postback(user_id: str, data: str, params: dict, scope_id: str):
    """
    作成ウィザードのPostback（ボタン選択・DatetimePickerの戻り）を処理する。
    （日付ピッカー、時刻候補、所要時間候補、スキップ/戻る/リセットなど）
    """
        
    # ホームメニュー（ドラフトの有無に関係なく動く）
    if data == "home=create":
        draft, _ = EventDraft.objects.get_or_create(user_id=user_id, defaults={"step": "title"})
        draft.step = "title"; draft.name = ""
        draft.start_time = None
        draft.end_time = None
        draft.capacity = None
        try: 
            draft.end_time_has_clock = False
        except Exception: 
            pass
        draft.scope_id = scope_id; draft.save()
        return ui.msg("ask_title")

    if data == "home=help":
        return ui.ask_home_menu(data)

    if data == "home=exit":
        # イベントドラフトを破棄
        EventDraft.objects.filter(user_id=user_id).delete()
        EventEditDraft.objects.filter(user_id=user_id).delete()       
         
        return ui.msg("exit")
    
    # --- 以降、ドラフト必須 --------
    
    # 作成ウィザードの処理
    try:
        draft = EventDraft.objects.get(user_id=user_id)
    except EventDraft.DoesNotExist:
        return None

    if data == "back":
        return _go_back_one_step(draft)

    if data == "reset":
        draft.step = "title"; draft.name = ""
        draft.start_time = None
        draft.end_time = None
        draft.capacity = None
        try: draft.end_time_has_clock = False
        except Exception: pass
        draft.save()
        return ui.msg("ask_title")

    if data == "exit":
        # 作成ドラフトを破棄して終了
        draft.delete()
        return ui.msg("exit")
        
    logger.debug("wizard postback step=%s data=%s", draft.step, data)
    
    # 開始日選択（DatetimePicker）
    if data == "pick=start_date" and draft.step == "start_date":
        d0 = utils.extract_dt_from_params_date_only(params)
        if not d0:
            return ui.msg("invalid_date")
        draft.start_time = d0
        draft.start_time_has_clock = False
        draft.step = "start_time"
        draft.save()
        return ui.ask_time_menu(prefix="start")

    # 時刻候補（start/end）
    m = re.search(r"time=(start|end)&v=([^&]+)$", data or "")
    if m:
        kind, v = m.group(1), m.group(2)

        if kind == "start" and draft.step == "start_time":
            if v == "__skip__":
                draft.start_time_has_clock = False
                draft.step = "end_mode"
                draft.save()
                return ui.ask_end_mode_menu()
            new_dt = utils.hhmm_to_utc_on_same_day(draft.start_time, v)
            if new_dt is None:
                return ui.msg("invalid_time")
            draft.start_time = new_dt
            draft.start_time_has_clock = True
            draft.step = "end_mode"
            draft.save()
            return ui.ask_end_mode_menu()

        if kind == "end" and draft.step == "end_time":
            if v == "__skip__":
                draft.end_time = None
                try: draft.end_time_has_clock = False
                except Exception: pass
                draft.step = "cap"
                draft.save()
                return ui.ask_capacity_menu()
            new_dt = utils.hhmm_to_utc_on_same_day(draft.start_time, v)
            if new_dt is None:
                return ui.msg("invalid_time")
            if draft.start_time and new_dt <= draft.start_time:
                return ui.msg("invalid_end_time")
            draft.end_time = new_dt
            try: draft.end_time_has_clock = True
            except Exception: pass
            draft.step = "cap"
            draft.save()
            return ui.ask_capacity_menu()

    # 終了の指定方法
    if data == "endmode=enddt":
        draft.end_time = utils.hhmm_to_utc_on_same_day(draft.start_time, "00:00")
        try: draft.end_time_has_clock = False
        except Exception: pass
        draft.step = "end_time"
        draft.save()
        return ui.ask_time_menu(prefix="end")
    if data == "endmode=duration":
        try: draft.end_time_has_clock = False
        except Exception: pass
        draft.step = "duration"
        draft.save()
        return ui.ask_duration_menu()

    if data == "endmode=skip":
        draft.end_time = None
        try: draft.end_time_has_clock = False
        except Exception: pass
        draft.step = "cap"
        draft.save()
        return ui.ask_capacity_menu()

    # 所要時間プリセット/スキップ
    if data.startswith("dur=") and draft.step == "duration":
        code = data.split("=", 1)[1]
        if code == "skip":
            draft.end_time = None
            try: draft.end_time_has_clock = False
            except Exception: pass
            draft.step = "cap"
            draft.save()
            return ui.ask_capacity_menu()

        delta = utils.parse_duration_to_delta(code)
        if not delta or delta.total_seconds() <= 0:
            return ui.msg("invalid_duration")
        draft.end_time = draft.start_time + delta
        try: draft.end_time_has_clock = False
        except Exception: pass
        draft.step = "cap"; draft.save()
        return ui.ask_capacity_menu()

    # 定員スキップ
    if data == "cap=skip" and draft.step == "cap":
        draft.capacity = None; draft.step = "done"; draft.save()
        return _finalize_event(draft)

    return None
