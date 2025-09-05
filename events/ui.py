# events/ui.py
# 役割: LINEメッセージUIの共通関数群を集約し、viewsから呼び出すだけにする。

from . import utils
from linebot.models import (
    TemplateSendMessage, ButtonsTemplate, PostbackAction,
    DatetimePickerAction, QuickReply, QuickReplyButton,
    TextSendMessage, MessageAction,
    CarouselTemplate, CarouselColumn,
    ConfirmTemplate
)


# ---- ホームメニュー（QuickReply）を表示 ----
def ask_home_menu(data: str | None = None):
    """
    作成/一覧/ヘルプ/終わる のQuick Replyを付けたホーム画面を返す。
    - data によって上部テキストを出し分ける:
        * "home=launch": 起動時の挨拶
        * "home=help"  : ヘルプ文面
        * それ以外     : 汎用メッセージ
    """

    if data == "home=help":
        text = "メニューをクリックするとイベントを作成したり、作成済のイベントのリストが見れるよ"
    elif data == "home=launch":
        text = "イベントボットだよ。呼んだ？"
    else:
        text = "やりたいことを選んでね"

    items = [
        QuickReplyButton(action=PostbackAction(label="イベント作成", data="home=create")),
        QuickReplyButton(action=PostbackAction(label="イベント一覧", data="home=list")),
        QuickReplyButton(action=PostbackAction(label="ヘルプ",     data="home=help")),
        QuickReplyButton(action=PostbackAction(label="終わる",     data="home=exit")),
    ]

    return TextSendMessage(text=text, quick_reply=QuickReply(items=items))


# ---- イベント一覧を表示 ----
def build_event_list_carousel(events):
    """
    役割: 自分が作成したイベントの一覧をCarouselで返す。
         各列に「詳細」「編集」を配置（アクション数を2に抑えて上限を回避）。
    """
    events = list(events or [])[:10]  # 列数ガード
    if not events:
        return msg("list.empty")
    
    cols = []
    for e in events:
        title = (e.name or "（無題）")[:40]  # タイトル長ガード
        start_txt = utils.local_fmt(e.start_time, getattr(e, "start_time_has_clock", True))
        text = f"開始: {start_txt}"[:60]    # 本文長ガード
        cols.append(CarouselColumn(
            title=title,
            text=text,
            # 列全体タップで詳細へ（本体タップ）
            default_action=PostbackAction(label="詳細", data=f"evt=detail&id={e.id}"),
            # LINE仕様で空配列は禁止のため、最低1つボタンを置く
            actions=[
                PostbackAction(label="詳細", data=f"evt=detail&id={e.id}")
            ]
        ))
    return TemplateSendMessage(alt_text="イベント一覧", template=CarouselTemplate(columns=cols))


# ---- イベント作成ウィザード内の [戻る] [はじめからやり直す] QuickReply ----
def make_quick_reply(
        show_back: bool = False, 
        show_reset: bool = False, 
        show_home: bool = True, 
        show_exit: bool = True
    ):
    """
    役割: Quick Reply のボタンを必要に応じて生成する。
    """
    items = []
    if show_back:
        items.append(
            QuickReplyButton(action=PostbackAction(label="戻る", data="back"))
        )
    if show_reset:
        items.append(
            QuickReplyButton(action=PostbackAction(label="はじめからやり直す", data="reset"))
        )
    if show_home:
        items.append(
            QuickReplyButton(action=PostbackAction(label="ホームに戻る", data="back_home"))
        )
    if show_exit:
        items.append(
            QuickReplyButton(action=PostbackAction(label="ボットを終了する", data="exit"))
        )
    return QuickReply(items=items) if items else None


# ========= メッセージテンプレート =========

_MESSAGE_TEMPLATES = {
    # 汎用
    "home.welcome": {
        "text": "イベントボットだよ。呼んだ？",
        "qr": dict(show_home=False, show_exit=True)
    },
    "home.back": {
        "text": "やりたいことを選んでね",
        "qr": dict(show_home=True, show_exit=True)
    },
    "exit": {
        "text": "また必要になったら「ボット」と呼んでね👋",
        "qr": dict(show_home=False, show_exit=False)
    },
    "home.back": {
        "text": "やりたいことを選んでね",
        "qr": dict(show_home=True, show_exit=True)
    },
        
    # 作成/編集ウィザード共通
    "ask_title": {
        "text": "イベントのタイトルを送信してね",
        "qr": dict(show_back=False, show_reset=False, show_home=True, show_exit=True)
    },
    "invalid_date": {
        "text": "日付が取得できなかったよ。もう一度選んでね",
        "qr": dict(show_back=True, show_reset=True, show_home=True, show_exit=True)
    },
    "invalid_time": {
        "text": "時刻は HH:MM 形式で入力してね（例 09:00）",
        "qr": dict(show_back=True, show_reset=True, show_home=True, show_exit=True)
    },
    "invalid_end_time": {
        "text": "終了が開始より前（同時刻含む）になっているよ。もう一度入力してね）",
        "qr": dict(show_back=True, show_reset=True, show_home=True, show_exit=True)
    },
    "invalid_duration": {
        "text": "所要時間は 1:30 / 90m / 2h / 120 などで入力してね",
        "qr": dict(show_back=True, show_reset=True, show_home=True, show_exit=True)
    },    
    "invalid_cap": {
        "text": "定員は1以上の整数を入力してね",
        "qr": dict(show_back=True, show_reset=True, show_home=True, show_exit=True)
    },   

    # 一覧・詳細
    "list.empty": {
        "text": "作成したイベントはまだないよ",
        "qr": dict(show_home=True, show_exit=True)
    },
    "detail.not_found": {
        "text": "不正なIDだよ",
        "qr": dict(show_home=True, show_exit=True)
    },

    # 参加/キャンセル
    "rsvp.joined": {
        "text": "{event_name} に参加登録したよ",
        "qr": dict(show_home=True, show_exit=True)
    },
    "rsvp.canceled": {
        "text": "{event_name} の参加をキャンセルしたよ",
        "qr": dict(show_home=True, show_exit=True)
    },

    # 権限・バリデーション
    "auth.forbidden": {
        "text": "この操作はできないよ",
        "qr": dict(show_home=True, show_exit=True)
    },
    "validation.error": {
        "text": "{message}",
        "qr": dict(show_home=True, show_exit=True)
    },
}

def msg(
    key: str,
    *,
    text: str | None = None,
    quick_reply=None,
    qr_override: dict | None = None,
    no_qr: bool = False,
    **fmt
):
    """
    テンプレートキーから TextSendMessage を生成する。
    - text: テンプレートを上書き（差し替え）したい場合に使用
    - quick_reply: 既定QRの完全置換（make_quick_reply(...) を渡す）
    - qr_override: 既定QR(dict)に差分上書き（例: {"show_back": True}）
    - no_qr: True で QR を一切付けない    
    - fmt: テキスト中の {placeholder} へ差し込み
    """

    tpl = _MESSAGE_TEMPLATES.get(key)
    if not tpl:
        base_text = text or key
        tpl_qr = None
    else:
        base_text = text or tpl["text"]
        tpl_qr = dict(tpl.get("qr", {}))  # ← dict() でコピー（元を汚さない）

    # 文言レンダリング
    try:
        rendered = base_text.format(**fmt) if fmt else base_text
    except Exception:
        rendered = base_text

    # QR の決定ロジック
    if no_qr:
        qr = None
    elif quick_reply is not None:
        # 明示指定があれば完全置換
        qr = quick_reply
    else:
        # テンプレ既定 + 差分上書き
        if tpl_qr is None:
            qr = None
        else:
            if qr_override:
                tpl_qr.update(qr_override)
            qr = make_quick_reply(**tpl_qr)

    m = TextSendMessage(text=rendered)
    if qr is not None:
        m.quick_reply = qr
    return m


# ---- ButtonsTemplateの薄いラッパ（alt_text統一やQR付与を簡便化）----
def build_buttons(text: str, actions, alt_text: str = "選択メニュー", title: str | None = None,
                  quick_reply: QuickReply | None = None):
    """
    ButtonsTemplateをTemplateSendMessageに包んで返す共通ファクトリ。
    - text: 本文
    - actions: list[PostbackAction / DatetimePickerAction]
    - alt_text: 通知領域などに表示される概要テキスト
    - title: 任意のタイトル（不要ならNone）
    - quick_reply: 任意でQuickReplyを付与
    """
    tpl = ButtonsTemplate(text=text, actions=actions, title=title)
    return TemplateSendMessage(alt_text=alt_text, template=tpl, quick_reply=quick_reply)


# ---- 日付ピッカー ----
def ask_date_picker(data: str, min_dt=None, max_dt=None,
                    with_back: bool = False, with_reset: bool = True, with_home: bool = True, with_exit: bool = True):
    """
    役割: mode='date' の DatetimePicker を1つだけ持つメニューを返す。
    - data: 'pick=start_date' など識別子
    - min_dt/max_dt: 選択制約（例: 開始日以前を選ばせない など）
    - with_back/with_reset: QuickReplyの有無
    """
    kwargs = {"label": "日付を選ぶ", "data": data, "mode": "date"}
    if min_dt:
        kwargs["min"] = utils._fmt_line_date(min_dt)
    if max_dt:
        kwargs["max"] = utils._fmt_line_date(max_dt)
        
    qr = make_quick_reply(show_back=with_back, show_reset=with_reset, show_home=with_home, show_exit=with_exit)
    return TemplateSendMessage(
        alt_text="日付を選ぶ",
        template=ButtonsTemplate(
            text="日付を選んでね",
            actions=[DatetimePickerAction(**kwargs)]
        ),
        quick_reply=qr
    )   

# ---- 時刻入力メニュー（候補＋スキップ誘導）----
def ask_time_menu(prefix: str,
                  times: tuple[str, ...] = ("09:00", "10:00", "19:00"),
                  allow_skip: bool = True,
                  with_back: bool = True, with_reset: bool = True, with_home: bool = True, with_exit: bool = True):
    """
    役割: 時刻候補（Postback）＋任意でスキップを提示する共通メニュー。
    ButtonsTemplate は actions 最大4件のため、候補数を丸める。
    """
    max_time_buttons = 3 if allow_skip else 4
    times = tuple(times[:max_time_buttons])  # ← これで常に4件以内に収める

    acts = [PostbackAction(label=t, data=f"time={prefix}&v={t}") for t in times]
    if allow_skip:
        acts.append(PostbackAction(label="設定しない", data=f"time={prefix}&v=__skip__"))

    qr = make_quick_reply(show_back=with_back, show_reset=with_reset, show_home=with_home, show_exit=with_exit)
    return build_buttons(
        text="時刻を HH:MM の形で入力するか、下から選んでね",
        actions=acts,
        alt_text="時刻入力",
        title=None,
        quick_reply=qr
    )


# ---- 終了指定方法メニュー ----
def ask_end_mode_menu(with_back: bool = True, with_reset: bool = True, with_home: bool = True, with_exit: bool = True):
    """
    役割: 「終了時刻を入力/所要時間を入力/スキップ（入力しない）」を選ばせる。
    """
    acts = [
        PostbackAction(label="終了時刻を入力", data="endmode=enddt"),
        PostbackAction(label="所要時間を入力", data="endmode=duration"),
        PostbackAction(label="設定しない", data="endmode=skip"),
    ]
    qr = make_quick_reply(show_back=with_back, show_reset=with_reset, show_home=with_home, show_exit=with_exit)
    
    return build_buttons(
        text="どっちを入力する？",
        actions=acts,
        alt_text="終了の指定方法",
        title=None,
        quick_reply=qr
    )

# ---- 所要時間プリセットメニュー ----
def ask_duration_menu(with_back: bool = True, with_reset: bool = True, with_home: bool = True, with_exit: bool = True):
    """
    役割: 所要時間のプリセット（30/60/90分）と自由入力の案内を提示する。
    """
    acts = [
        PostbackAction(label="30分", data="dur=30m"),
        PostbackAction(label="60分", data="dur=60m"),
        PostbackAction(label="1時間30分", data="dur=90m"),
        PostbackAction(label="設定しない", data="dur=skip"),
    ]
    qr = make_quick_reply(show_back=with_back, show_reset=with_reset, show_home=with_home, show_exit=with_exit)
    return build_buttons(
        text="所要時間を入力するか、下から選んでね。\n例: 15分→【15】/ 1時間30分→【1:30】/ 2時間→【2h】",
        actions=acts,
        alt_text="所要時間の入力",
        title=None,
        quick_reply=qr
    )

# ---- 定員入力メニュー ----
def ask_capacity_menu(text: str = "定員を数字で入力してね",
                      with_back: bool = True, with_reset: bool = True, with_home: bool = True, with_exit: bool = True):
    """
    役割: 定員を数字で入力させる前提の案内と、スキップボタンのみを出す共通メニュー。
    - text: 文言を差し替えたい場合に指定
    """
    acts = [PostbackAction(label="設定しない", data="cap=skip")]
    qr = make_quick_reply(show_back=with_back, show_reset=with_reset, show_home=with_home, show_exit=with_exit)
    return build_buttons(
        text=text,
        actions=acts,
        alt_text="定員の設定",
        title=None,
        quick_reply=qr
    )

# ---- 編集項目選択メニュー ----
def ask_edit_menu():
    """
    役割: 編集する項目の選択メニューを表示する（Quick Reply）。
    """
    items = [
        QuickReplyButton(action=PostbackAction(label="タイトル",   data="edit=title")),
        QuickReplyButton(action=PostbackAction(label="日付",     data="edit=start_date")),
        QuickReplyButton(action=PostbackAction(label="開始時刻",   data="edit=start_time")),
        QuickReplyButton(action=PostbackAction(label="終了時刻", data="edit=end")),
        QuickReplyButton(action=PostbackAction(label="定員",       data="edit=cap")),
        QuickReplyButton(action=PostbackAction(label="保存",       data="edit=save")),
        QuickReplyButton(action=PostbackAction(label="中止",       data="edit=cancel")),
    ]
    return TextSendMessage(
        text="編集する項目を選んでね。\n編集内容を保存するときは【保存】，編集をやめるときは【中止】を選んでね",
        quick_reply=QuickReply(items=items)
    )


def build_event_summary(e, end_has_clock: bool | None = None, with_edit_button: bool = True):
    """
    役割: イベント詳細（確認用）の文面を構築する。
    - with_edit_button=True のとき、本文の下に「編集」ボタン（Postback）を付与する。
    """
    start_text = utils.local_fmt(e.start_time, getattr(e, "start_time_has_clock", True))
    if e.end_time is None:
        end_text = "終了時間: （未設定）"
    else:
        has_clock = end_has_clock if end_has_clock is not None else True
        if not has_clock and not getattr(e, "start_time_has_clock", True):
            mins = int((e.end_time - e.start_time).total_seconds() // 60)
            end_text = f"所要時間: {utils.minutes_humanize(mins)}"
        else:
            end_text = f"終了時間: {utils.local_fmt(e.end_time, True)}"

    cap_text = "定員なし" if e.capacity is None else f"定員: {e.capacity}"
    body = f"ID:{e.id}\nタイトル:{e.name}\n開始:{start_text}\n{end_text}\n{cap_text}"

    if not with_edit_button:
        # 従来どおりテキストのみで返したい場合
        return TextSendMessage(text=body)

    # 「編集」ボタン付きで返す（押下で evt=edit に遷移）
    return build_buttons(
        text=body,
        actions=[
            PostbackAction(label="編集", data=f"evt=edit&id={e.id}"),
            PostbackAction(label="削除", data=f"evt=delete&id={e.id}"),
        ],
        alt_text="イベント詳細",
        title=None,
        quick_reply=None
    )


def ask_delete_confirm(e):
    """
    削除前の確認テンプレートを返す。
    """
    tpl = ConfirmTemplate(
        text=f"「{e.name or '（無題）'}」を削除していい？",
        actions=[
            PostbackAction(label="はい、削除する", data=f"evt=delete_confirm&id={e.id}&ok=1"),
            PostbackAction(label="やめる",       data=f"evt=delete_confirm&id={e.id}&ok=0"),
        ]
    )
    return TemplateSendMessage(alt_text="削除の確認", template=tpl)


# --- 一覧UIのディスパッチャ（将来 Flex / カレンダーに差し替え可）---
def render_event_list(events, style: str = "carousel"):
    """
    役割: イベント一覧の見た目を一元化して返す。
    - style='carousel' | 'flex' | 'calendar'（将来拡張）
    """
    if style == "carousel":
        msg = build_event_list_carousel(events)
    else:
        # 将来: if style == "flex": msg = build_event_list_flex(events)
        # 将来: if style == "calendar": msg = build_event_list_calendar(events)
        msg = build_event_list_carousel(events)

    # 一覧は常に「ホームに戻る」「ボットを終了する」のQRを既定で付与する。
    # ただし既に quick_reply が設定されている（カスタムQRを使いたい）場合は尊重する。
    if getattr(msg, "quick_reply", None) is None:
        msg.quick_reply = make_quick_reply(show_home=True, show_exit=True)
    return msg


SUPPRESS_EXIT_ATTR = "_suppress_exit_qr"

def suppress_exit_qr(reply):
    """
    この返信（単体 or list）には 'ボットを終了する' QR を付けないよう印を付ける。
    """
    if isinstance(reply, list):
        for m in reply:
            setattr(m, SUPPRESS_EXIT_ATTR, True)
        return reply
    setattr(reply, SUPPRESS_EXIT_ATTR, True)
    return reply

def _ensure_exit_on_message(msg):
    """
    単一メッセージに 'ボットを終了する' QuickReply を付与する。
    （既存QRがあればマージ）
    """
    if getattr(msg, SUPPRESS_EXIT_ATTR, False):
        return msg

    if not hasattr(msg, "quick_reply"):
        return msg  # 型が異なるなど、QRを持てない場合はそのまま

    # 既存がなければ新規付与
    if msg.quick_reply is None:
        msg.quick_reply = make_quick_reply(show_exit=True)
        return msg

    # 既存があれば 'exit' が無いときだけ追加
    items = list(getattr(msg.quick_reply, "items", []) or [])
    has_exit = any(
        isinstance(it.action, PostbackAction) and getattr(it.action, "data", "") == "exit"
        for it in items
    )
    if not has_exit:
        items.append(QuickReplyButton(action=PostbackAction(label="ボットを終了する", data="exit")))
        msg.quick_reply.items = items
    return msg


def attach_exit_qr(reply):
    """
    返信オブジェクト（単体 or list）に対し、一括で '終了' QR を付与する。
    """
    if isinstance(reply, list):
        return [_ensure_exit_on_message(m) for m in reply]
    return _ensure_exit_on_message(reply)
