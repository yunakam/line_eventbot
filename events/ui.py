# events/ui.py
# 役割: LINEメッセージUIの共通関数群を集約し、viewsから呼び出すだけにする。

from django.utils import timezone
from linebot.models import (
    TemplateSendMessage, ButtonsTemplate, PostbackAction,
    DatetimePickerAction, QuickReply, QuickReplyButton
)


# ---- QuickReplyユーティリティ ----
def make_quick_reply(show_back: bool = False, show_reset: bool = False):
    """
    役割: Quick Reply のボタンを必要に応じて生成する。
    - show_back=True で「戻る」(data='back')
    - show_reset=True で「はじめからやり直す」(data='reset')
    どちらも False の場合は None を返す（＝Quick Reply 非表示）
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
    return QuickReply(items=items) if items else None

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
def ask_date_picker(prompt_text: str, data: str, min_dt=None, max_dt=None,
                    with_back: bool = False, with_reset: bool = False):
    """
    役割: mode='date' の DatetimePicker を1つだけ持つメニューを返す。
    - data: 'pick=start_date' など識別子
    - min_dt/max_dt: 選択制約（例: 開始日以前を選ばせない など）
    - with_back/with_reset: QuickReplyの有無
    """
    kwargs = {"label": "日付を選ぶ", "data": data, "mode": "date"}
    if min_dt:
        kwargs["min"] = _fmt_line_date(min_dt)
    if max_dt:
        kwargs["max"] = _fmt_line_date(max_dt)
        
    qr = make_quick_reply(show_back=with_back, show_reset=with_reset)
    return TemplateSendMessage(
        alt_text="日付を選ぶ",
        template=ButtonsTemplate(
            text=prompt_text,
            actions=[DatetimePickerAction(**kwargs)]
        ),
        quick_reply=qr
    )   

# ---- 時刻入力メニュー（候補＋スキップ誘導）----
def ask_time_menu(prompt_text: str, prefix: str,
                  times: tuple[str, ...] = ("09:00", "10:00", "19:00"),
                  allow_skip: bool = True,
                  with_back: bool = False, with_reset: bool = False):
    """
    役割: 時刻候補（Postback）＋任意でスキップを提示する共通メニュー。
    - prefix: 'start' or 'end'（Postback data に埋め込む）
    - times: ボタンに出す候補時刻
    - allow_skip: スキップボタンを出すか
    """
    acts = [PostbackAction(label=t, data=f"time={prefix}&v={t}") for t in times]
    if allow_skip:
        acts.append(
            PostbackAction(
                label="スキップ", 
                data=f"time={prefix}&v=__skip__"
                )
            )
    qr = make_quick_reply(show_back=with_back, show_reset=with_reset)
    return build_buttons(
        text=prompt_text,
        actions=acts,
        alt_text="時刻入力",
        title=None,
        quick_reply=qr
    )

# ---- 終了指定方法メニュー ----
def ask_end_mode_menu(with_back: bool = False, with_reset: bool = False):
    """
    役割: 「終了時刻を入力/所要時間を入力/スキップ（入力しない）」を選ばせる。
    """
    acts = [
        PostbackAction(label="終了時刻を入力", data="endmode=enddt"),
        PostbackAction(label="所要時間を入力", data="endmode=duration"),
        PostbackAction(label="スキップ", data="endmode=skip"),
    ]
    qr = make_quick_reply(show_back=with_back, show_reset=with_reset)
    
    return build_buttons(
        text="イベント終了時刻はどうやって入力する？",
        actions=acts,
        alt_text="終了の指定方法",
        title=None,
        quick_reply=qr
    )

# ---- 所要時間プリセットメニュー ----
def ask_duration_menu(with_back: bool = False, with_reset: bool = False):
    """
    役割: 所要時間のプリセット（30/60/90分）と自由入力の案内を提示する。
    """
    acts = [
        PostbackAction(label="30分", data="dur=30m"),
        PostbackAction(label="60分", data="dur=60m"),
        PostbackAction(label="1時間30分", data="dur=90m"),
        PostbackAction(label="スキップ", data="dur=skip"),
    ]
    qr = make_quick_reply(show_back=with_back, show_reset=with_reset)
    return build_buttons(
        text="所要時間を入力するか、下から選んでね。\n例: 15分→【15】/ 1時間30分→【1:30】/ 2時間→【2h】",
        actions=acts,
        alt_text="所要時間の入力",
        title=None,
        quick_reply=qr
    )

# ---- 定員入力メニュー ----
def ask_capacity_menu(text: str = "定員を数字で入力してね",
                      with_back: bool = False, with_reset: bool = False):
    """
    役割: 定員を数字で入力させる前提の案内と、スキップボタンのみを出す共通メニュー。
    - text: 文言を差し替えたい場合に指定
    """
    acts = [PostbackAction(label="設定しない（スキップ）", data="cap=skip")]
    qr = make_quick_reply(show_back=with_back, show_reset=with_reset)
    return build_buttons(
        text=text,
        actions=acts,
        alt_text="定員の設定",
        title=None,
        quick_reply=qr
    )
