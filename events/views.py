# events/views.py
import os
import re
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.core.exceptions import ImproperlyConfigured
from django.conf import settings
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from datetime import timedelta

from .models import Event, EventDraft  # Participantを後で追加

from linebot import LineBotApi, WebhookParser
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    PostbackEvent, ButtonsTemplate,
    DatetimePickerAction, TemplateSendMessage, PostbackAction
)
from linebot.exceptions import InvalidSignatureError



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
    """環境変数やsettingsからアクセストークン／チャネルシークレットを読み、LINE SDKクライアントを返す"""
    token = settings.LINE_CHANNEL_ACCESS_TOKEN or os.getenv("LINE_CHANNEL_ACCESS_TOKEN")  # トークンを取得
    secret = settings.LINE_CHANNEL_SECRET or os.getenv("LINE_CHANNEL_SECRET")             # シークレットを取得
    if not token or not secret:
        # どちらかでも欠けていればアプリの設定不備なので例外とする
        raise ImproperlyConfigured("LINEのトークン/シークレットが未設定である。")
    # 返信用のLineBotApiと、署名検証・イベント解析用のWebhookParserを返す
    return LineBotApi(token), WebhookParser(secret)


@csrf_exempt
def callback(request):   # LINEプラットフォームからのWebhookエンドポイントである
    if request.method != "POST":
        # WebhookはPOSTのみが許容されるため、その他メソッドは405で拒否する
        return HttpResponse("Method not allowed", status=405)

    # 送信用クライアントと、署名検証・パース用のパーサを取得
    line_bot_api, parser = get_line_clients()

    # リクエストヘッダから署名を取得（改ざん検出のため）
    signature = request.headers.get("X-Line-Signature", "")
    # リクエストボディ（JSON文字列）をUTF-8としてデコード
    body = request.body.decode("utf-8")

    try:
        # 署名を検証しつつ、MessageEventやPostbackEventなどのイベント配列にパースする
        events = parser.parse(body, signature)
    except InvalidSignatureError:
        # 署名が不正であれば400（Bad Request）を返す
        return HttpResponse(status=400)

    # 受け取ったイベントを1件ずつ処理する
    for ev in events:
        # --- テキストメッセージを受信した場合の分岐 ---
        if isinstance(ev, MessageEvent) and isinstance(ev.message, TextMessage):
            user_id = ev.source.user_id        # 送信者のLINEユーザーIDを取得
            text = ev.message.text.strip()     # 受信テキストをトリム（余計な空白や改行を除去）

            # ユーザーが「イベント作成」と送ったら、作成ウィザードを開始する
            if text == "イベント作成":
                # ユーザーごとの下書き（EventDraft）を新規作成または取得する
                draft, _ = EventDraft.objects.get_or_create(user_id=user_id, defaults={"step": "title"})
                # 下書き内容を初期化し、タイトル入力待ち状態（step=title）にする
                draft.step = "title"
                draft.name = ""
                draft.start_time = None
                draft.end_time = None
                draft.capacity = None
                draft.save()
                # タイトルの入力を促す
                line_bot_api.reply_message(
                    ev.reply_token,
                    TextSendMessage(text="イベントのタイトルは？")
                )
                # このイベント処理は完了なので次のイベントへ
                continue

            # すでにウィザード進行中（下書きが存在）なら、その状態に応じてテキストを処理する
            if EventDraft.objects.filter(user_id=user_id).exists():
                reply = handle_wizard_text(user_id, text)  # タイトル or 定員入力の処理
                if reply:
                    # 処理結果（メッセージやテンプレート）を返信する
                    line_bot_api.reply_message(ev.reply_token, reply)
                    continue  # このイベント処理は終わり

            # ウィザード外の通常コマンド（例：一覧表示や参加）を処理する（必要に応じて実装）
            reply_text = handle_command(text, user_id)
            if not reply_text:
                reply_text = "「イベント作成」と送ったらイベントが作れるよ！"
            line_bot_api.reply_message(ev.reply_token, TextSendMessage(text=reply_text))

        # --- Postback（ボタン押下・DatetimePickerの戻り）を受信した場合の分岐 ---
        elif isinstance(ev, PostbackEvent):
            user_id = ev.source.user_id               # 送信者のユーザーID
            data = ev.postback.data                   # PostbackActionに設定したdata（識別子）を取得
            params = ev.postback.params or {}         # DatetimePickerから返る日時などのパラメータ

            # ウィザードのPostback処理（開始・終了日時や定員メニュー）をハンドリングする
            reply = handle_wizard_postback(user_id, data, params)
            if reply:
                # 処理結果を返信する
                line_bot_api.reply_message(ev.reply_token, reply)

    # すべてのイベントを正常に処理したので200を返す
    return HttpResponse(status=200)


def handle_wizard_text(user_id: str, text: str):
    """
    【ユーザーのテキスト入力データを処理】
    → タイトル、時刻の手入力、所要時間の手入力、定員
    """
    try:
        draft = EventDraft.objects.get(user_id=user_id)
    except EventDraft.DoesNotExist:
        return None

    # タイトル → 開始日時ピッカーへ
    if draft.step == "title":
        if not text:
            return TextSendMessage(text="イベントのタイトルを入力してね")
        draft.name = text
        draft.step = "start_date"
        draft.save()
        return ask_date_picker("イベントの開始日を教えてね", data="pick=start_date")

    # 時刻の「自由入力」: start_time フェーズ
    if draft.step == "start_time":
        ok, (h, m) = parse_hhmm(text)
        if not ok:
            return TextSendMessage(text="時刻は HH:MM 形式で入力してね（例 09:30）")
        # 既に start_time は日付 00:00 で入っている想定
        draft.start_time = draft.start_time.replace(hour=h, minute=m, second=0, microsecond=0)
        draft.step = "end_mode"
        draft.save()
        return ask_end_mode_menu()
    
    # 時刻の「自由入力」: end_time フェーズ
    if draft.step == "end_time":
        ok, (h, m) = parse_hhmm(text)
        if not ok:
            return TextSendMessage(text="時刻は HH:MM 形式で入力してね（例 19:00）")
        # 既に end_time は日付 00:00 で入っている想定
        tmp = draft.end_time.replace(hour=h, minute=m, second=0, microsecond=0)
        if draft.start_time and tmp <= draft.start_time:
            return TextSendMessage(text="終了が開始より前（同時刻含む）になっているよ。もう一度入力してね")
        draft.end_time = tmp
        draft.step = "cap"
        draft.save()
        return ask_capacity_menu()

    if draft.step == "duration":
        delta = parse_duration_to_delta(text)
        if not delta or delta.total_seconds() <= 0:
            return TextSendMessage(text="所要時間は 1:30 / 90m / 2h / 120 などで入力してね")
        # 開始日時が未設定ならエラー
        if not draft.start_time:
            return TextSendMessage(text="先に開始日時を選んでね")
        # 終了時刻を開始＋所要時間で自動計算
        draft.end_time = draft.start_time + delta
        draft.step = "cap"
        draft.save()
        return ask_capacity_menu()

    # 定員の数値入力
    if draft.step == "cap":
        capacity = parse_int_safe(text)
        if capacity is None or capacity <= 0:
            return TextSendMessage(text="定員は1以上の整数を入力してね。定員なしにするなら「スキップ」を選んでね")
        draft.capacity = capacity
        draft.step = "done"
        draft.save()
        return finalize_event(draft)

    return None


def handle_wizard_postback(user_id: str, data: str, params: dict):
    """
    【ユーザーのPostbackデータを処理】
    → 日付ピッカー、時刻・所要時間を候補ボタンから選択、定員スキップ
    
    ※Postback：ボタンテンプレートや日時ピッカーを押したときに返ってくる「隠しデータ」
    """

    try:
        draft = EventDraft.objects.get(user_id=user_id)
    except EventDraft.DoesNotExist:
        return None

    print(f"[DEBUG] step={draft.step}, data={data}")

    # --- 開始日 選択 ---
    if data == "pick=start_date" and draft.step == "start_date":
        d0 = extract_dt_from_params_date_only(params)
        if not d0:
            return TextSendMessage(text="開始日が取得できなかったよ。もう一度選んでね")
        # いったん 00:00 で保存し、任意の時刻入力へ
        draft.start_time = d0  # 00:00
        draft.step = "start_time"
        draft.save()
        return ask_time_menu("開始時刻を【HH:MM】の形で入力するか、下から選んでね", prefix="start")

    # --- 終了日 選択 ---　※開始日と異なる終了日の設定を許可する場合には回復
    # if data == "pick=end_date" and draft.step == "end_date":
    #     d0 = extract_dt_from_params_date_only(params)
    #     if not d0:
    #         return TextSendMessage(text="終了日が取得できなかったよ。もう一度選んでね")
    #     draft.end_time = d0  # 00:00
    #     draft.step = "end_time"
    #     draft.save()
    #     return ask_time_menu("終了時刻を【HH:MM】の形で入力するか、下から選んでね", prefix="end")

    # --- 時刻（候補 or スキップ）選択 ---
    if data.startswith("time="):
        # data: 'time=start' or 'time=end'、paramsは使わず data の後続と v= を見る
        # ただし LINE SDK では Postback の 'data' しか来ないので、'v=..' は data に埋め込む設計にしている
        # 例: data='time=start&v=09:00'
        m = re.search(r"time=(start|end)&v=([^&]+)$", data)
        if not m:
            return None
        kind, v = m.group(1), m.group(2)
        if kind == "start" and draft.step == "start_time":
            new_dt = combine_date_time(draft.start_time, None if v == "__skip__" else v, is_end=False)
            if not new_dt:
                return TextSendMessage(text="時刻の形式が不正だよ。もう一度選ぶか「HH:MM」で入力してね")
            draft.start_time = new_dt
            draft.step = "end_mode" 
            draft.save()
            return ask_end_mode_menu() 

        if kind == "end" and draft.step == "end_time":
            new_dt = combine_date_time(draft.end_time, None if v == "__skip__" else v, is_end=True)
            if not new_dt:
                return TextSendMessage(text="時刻の形式が不正だよ。もう一度選ぶか「HH:MM」で入力してね")
            if draft.start_time and new_dt <= draft.start_time:
                return TextSendMessage(text="終了が開始より前（同時刻含む）になっているよ。別の時刻にしてね")
            draft.end_time = new_dt
            draft.step = "cap"
            draft.save()
            return ask_capacity_menu()

    # 終了の指定方法：終了時刻を入力
    if data == "endmode=enddt": 
        # 終了日を開始日と同じ日に自動設定
        draft.end_time = draft.start_time.replace(hour=0, minute=0, second=0, microsecond=0)
        draft.step = "end_time"
        draft.save()
        return ask_time_menu("終了時刻を【HH:MM】の形で入力するか、下から選んでね", prefix="end")

    # 終了の指定方法：所要時間を入力
    if data == "endmode=duration":
        draft.step = "duration"
        draft.save()
        return ask_duration_menu()

    # 所要時間のプリセットボタンを押したときの処理
    if data.startswith("dur=") and draft.step == "duration":
        code = data.split("=", 1)[1]
        delta = parse_duration_to_delta(code)
        if not delta or delta.total_seconds() <= 0:
            return TextSendMessage(text="所要時間の形式が不正だよ。もう一度選んでね")
        draft.end_time = draft.start_time + delta
        draft.step = "cap"
        draft.save()
        return TemplateSendMessage(
            alt_text="定員の設定",
            template=ButtonsTemplate(
                title="定員",
                text="定員を設定する？",
                actions=[
                    PostbackAction(label="設定しない（スキップ）", data="cap=skip"),
                    PostbackAction(label="定員を数字で入力する", data="cap=input"),
                ],
            ),
        )


    # --- 既存: 定員スキップ/入力 ---
    if data == "cap=skip" and draft.step == "cap":
        draft.capacity = None
        draft.step = "done"
        draft.save()
        return finalize_event(draft)

    if data == "cap=input" and draft.step == "cap":
        return TextSendMessage(text="定員の数値を入力してね。（例: 10）")

    return None



# ===== ヘルパー / ユーティリティ =====

def fmt_line_date(dt):
    """
    LINEのDatetimePicker(mode='date')向けに 'YYYY-MM-DD' へ整形する。
    """
    local = timezone.localtime(dt, timezone.get_current_timezone())
    return local.strftime("%Y-%m-%d")


def parse_hhmm(s: str):
    """
    'HH:MM' を検証して (ok, (H, M)) を返す。
    """
    s = (s or "").strip()
    m = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", s)
    if not m:
        return False, (0, 0)
    return True, (int(m.group(1)), int(m.group(2)))


def parse_duration_to_delta(s: str):
    """
    所要時間文字列を timedelta に変換する。
    受理形式: 'H:MM' / '90m' / '2h' / '120'(分)
    """
    s = (s or "").strip().lower()
    m = re.fullmatch(r"(\d{1,2}):([0-5]\d)", s)
    if m:
        h, mm = int(m.group(1)), int(m.group(2))
        return timedelta(minutes=h*60 + mm)
    m = re.fullmatch(r"(\d{1,4})m", s)
    if m:
        return timedelta(minutes=int(m.group(1)))
    m = re.fullmatch(r"(\d{1,3})h", s)
    if m:
        return timedelta(hours=int(m.group(1)))
    if re.fullmatch(r"\d{1,4}", s):
        return timedelta(minutes=int(s))
    return None


def parse_int_safe(s: str):
    """
    数字のみの文字列を int に変換する。数字以外を含めば None。
    """
    s = (s or "").strip()
    if not re.fullmatch(r"\d+", s):
        return None
    try:
        return int(s)
    except Exception:
        return None


def extract_dt_from_params_date_only(params: dict):
    """
    DatetimePicker(mode='date') の params から 'date' を取り出し、
    その日の 00:00 の aware datetime を返す。
    例: params = {'date': '2025-09-01'}
    """
    d = params.get("date")
    if not d:
        return None
    dt = parse_datetime(d + " 00:00:00")
    if not dt:
        return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def combine_date_time(date_dt, hhmm: str | None, is_end: bool = False):
    """
    日付のみ(00:00)の aware datetime に時刻を合成する。
    hhmm が None/ '__skip__' の場合、開始=00:00 / 終了=23:59 を補完する。
    """
    if hhmm in (None, "__skip__"):
        h, m = (23, 59) if is_end else (0, 0)
    else:
        ok, (h, m) = parse_hhmm(hhmm)
        if not ok:
            return None
    return date_dt.replace(hour=h, minute=m, second=0, microsecond=0)


# ===== メニュー生成 =====

def ask_date_picker(prompt_text: str, data: str, min_dt=None, max_dt=None):
    """
    日付のみをカレンダーで選ばせるテンプレートを返す。
    min_dt / max_dt を与えると選択範囲を制約できる。
    """
    kwargs = {"label": "日付を選ぶ", "data": data, "mode": "date"}
    if min_dt:
        kwargs["min"] = fmt_line_date(min_dt)
    if max_dt:
        kwargs["max"] = fmt_line_date(max_dt)

    template = ButtonsTemplate(
        text=prompt_text,
        actions=[DatetimePickerAction(**kwargs)]
    )
    return TemplateSendMessage(alt_text="日付選択", template=template)


def ask_time_menu(prompt_text: str, prefix: str):
    """
    時刻候補（09:00/10:00/19:00）とスキップを提示する。
    prefix は 'start' / 'end' で、Postback data に埋め込む。
    """
    actions = [
        PostbackAction(label="09:00", data=f"time={prefix}&v=09:00"),
        PostbackAction(label="10:00", data=f"time={prefix}&v=10:00"),
        PostbackAction(label="19:00", data=f"time={prefix}&v=19:00"),
        PostbackAction(label="スキップ", data=f"time={prefix}&v=__skip__"),
    ]
    return TemplateSendMessage(
        alt_text="時刻入力",
        template=ButtonsTemplate(text=prompt_text, actions=actions)
    )


def ask_end_mode_menu():
    """
    終了の指定方法（終了時刻入力 / 所要時間入力）を選ばせる。
    """
    return TemplateSendMessage(
        alt_text="終了の指定方法",
        template=ButtonsTemplate(
            text="終了時刻を教えてね",
            actions=[
                PostbackAction(label="終了時刻を入力", data="endmode=enddt"),
                PostbackAction(label="所要時間を入力", data="endmode=duration"),
            ],
        ),
    )


def ask_duration_menu():
    """
    所要時間のプリセットを提示する（自由入力はメッセージで受ける運用）。
    """
    return TemplateSendMessage(
        alt_text="所要時間の入力",
        template=ButtonsTemplate(
            text="所要時間を入力するか、下から選んでね。\n"
                 "入力例: 15 / 1:10 / 2h / 90m（メッセージ欄に直接入力OK）",
            actions=[
                PostbackAction(label="30分", data="dur=30m"),
                PostbackAction(label="60分", data="dur=60m"),
                PostbackAction(label="1時間30分", data="dur=90m"),
            ],
        ),
    )


def ask_capacity_menu():
    """
    定員入力の案内（数値はテキストで入力 / スキップ可）を返す。
    """
    return TemplateSendMessage(
        alt_text="定員の設定",
        template=ButtonsTemplate(
            text="定員を設定する場合は数字で入力してね（スキップもできるよ）",
            actions=[PostbackAction(label="設定しない（スキップ）", data="cap=skip")],
        ),
    )


# ===== ドメイン処理 =====

def finalize_event(draft: "EventDraft"):
    """
    Draft を Event に確定し、作成結果メッセージを返す。
    """
    e = Event.objects.create(
        name=draft.name,
        start_time=draft.start_time,
        end_time=draft.end_time,
        capacity=draft.capacity,
    )
    cap_text = "定員なし" if e.capacity is None else f"定員:{e.capacity}"
    summary = (
        "イベントを作成したよ！\n"
        f"ID:{e.id}\n"
        f"タイトル:{e.name}\n"
        f"開始:{e.start_time}\n"
        f"終了:{e.end_time}\n"
        f"{cap_text}"
    )
    return TextSendMessage(text=summary)


def handle_command(text, user_id):
    """
    ウィザード外コマンドのフック（未実装なら None を返す）。
    例: 'イベント一覧', '参加:ID' など。
    """
    return None
