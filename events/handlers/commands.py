# 役割: 「一覧/詳細/編集開始」などのコマンドと、カルーセルからのショートカットを処理する

import re
from linebot.models import TextSendMessage, TemplateSendMessage
from ..models import Event, EventEditDraft
from .. import ui
from .. import policies

def handle_command(text: str, user_id: str, scope_id: str):
    """
    役割: テキストで来るコマンド（イベント一覧/詳細/編集開始）を処理する。
    """
    text = (text or "").strip()

    # 一覧（グループ＝scope_id 全体を対象）
    if text == "イベント一覧":
        qs = Event.objects.filter(scope_id=scope_id).order_by("-id")[:10]
        if not qs:
            return "作成されたイベントはまだないよ"
        return ui.render_event_list(qs) 
    
    # イベント詳細
    m = re.fullmatch(r"イベント詳細[:：]\s*(\d+)", text)
    if m:
        eid = int(m.group(1))
        try:
            e = Event.objects.get(id=eid, scope_id=scope_id)
        except Event.DoesNotExist:
            return TextSendMessage(text="イベントが見つからないよ（または編集権限がないよ）")
        return ui.build_event_summary(e)

    # イベント編集開始
    m = re.fullmatch(r"編集[:：]\s*(\d+)", text)
    if m:
        eid = int(m.group(1))
        try:
            e = Event.objects.get(id=eid)
        except Event.DoesNotExist:
            return "イベントが見つからないよ（または編集権限がないよ）"

        # 権限チェック
        if not policies.can_edit_event(user_id, e):
            return "イベントが見つからないよ（または編集権限がないよ）"

        # 編集ドラフトを作成/初期化
        EventEditDraft.objects.update_or_create(
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


def handle_evt_shortcut(user_id: str, scope_id: str, data: str):
    """
    役割: 一覧Carousel等からのショートカット（evt=detail / evt=edit）を処理する。
    - detail: そのまま詳細表示
    - edit  : 編集ドラフトを作成して編集メニューへ
    """
    m = re.search(r"evt=(detail|edit)&id=(\d+)", data or "")
    if not m:
        return None
    kind, eid = m.group(1), int(m.group(2))

    try:
        e = Event.objects.get(id=eid, scope_id=scope_id)
    except Event.DoesNotExist:
        return TextSendMessage(text="該当するイベントが見つからないよ")

    if kind == "detail":
        return ui.build_event_summary(e)

    # edit
    if not policies.can_edit_event(user_id, e):
        return TextSendMessage(text="イベントの作成者だけが編集できるよ")

    EventEditDraft.objects.update_or_create(
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
            "scope_id": scope_id,
        }
    )
    return ui.ask_edit_menu()
