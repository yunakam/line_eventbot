# events/views.py
import os
import re
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.core.exceptions import ImproperlyConfigured
from django.conf import settings
from django.utils.dateparse import parse_datetime
from django.utils import timezone

from .models import Event, Participant, EventDraft

from linebot import LineBotApi, WebhookParser
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    PostbackEvent, TemplateSendMessage, ButtonsTemplate,
    DatetimePickerAction, QuickReply, QuickReplyButton,
    TemplateSendMessage, ButtonsTemplate, PostbackAction
)
from linebot.exceptions import InvalidSignatureError


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
                # 既定メッセージ（ヘルプ的なオウム返し）を返す
                reply_text = f"「イベント作成」と送ったらイベントが作れるよ！"
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
    ウィザードのうち、テキスト入力で進むステップ（タイトル入力、定員数値入力）を処理する。
    """
    try:
        # 進行中の下書きを取得する（なければウィザード外なのでNoneを返す）
        draft = EventDraft.objects.get(user_id=user_id)
    except EventDraft.DoesNotExist:
        return None

    # --- タイトル入力ステップ ---
    if draft.step == "title":
        if not text:
            # 未入力はエラーとして再入力を促す
            return TextSendMessage(text="イベントのタイトルを入力してね")
        # タイトルを保存し、開始日時選択ステップへ遷移
        draft.name = text
        draft.step = "start"
        draft.save()
        # 開始日時のDatetimePickerを提示する（Postbackで戻ってくる想定）
        return ask_datetime_picker("イベントの開始日時を選んでね", data="pick=start")

    # --- 定員入力ステップ ---
    if draft.step == "cap":
        # ユーザーが数値を入力してきた前提で安全に整数化を試みる
        capacity = parse_int_safe(text)
        if capacity is None or capacity <= 0:
            # 不正入力の場合は、再入力を促す（クイックリプライは使わないスタイル）
            return TextSendMessage(text="定員は1以上の整数を入力してね。定員なしにするなら「スキップ」を選んで。")
        # 有効な整数なので保存し、完了へ
        draft.capacity = capacity
        draft.step = "done"
        draft.save()
        # 最終確定：Eventを作成し、結果メッセージを返す
        return finalize_event(draft)

    # それ以外（開始・終了日時）はPostbackで処理するのでここでは何もしない
    return None


def handle_wizard_postback(user_id: str, data: str, params: dict):
    """
    日時ピッカーや定員メニューのPostbackを処理する。
    - pick=start … 開始日時の決定
    - pick=end   … 終了日時の決定
    - cap=skip   … 定員なしで確定
    - cap=input  … 定員を手入力へ遷移（次のテキスト入力を待つ）
    """
    try:
        # 進行中の下書きを取得する
        draft = EventDraft.objects.get(user_id=user_id)
    except EventDraft.DoesNotExist:
        return None

    # --- 開始日時の選択（DatetimePickerの戻り） ---
    if data == "pick=start" and draft.step == "start":
        dt = extract_dt_from_params(params)  # paramsからdatetimeを取り出す（タイムゾーン付与まで実施）
        if not dt:
            # 取得失敗時は再提示
            return TextSendMessage(text="開始日時が取得できなかったよ。もう一度選んでね")
        # 開始日時を保存し、終了日時選択ステップへ遷移
        draft.start_time = dt
        draft.step = "end"
        draft.save()
        # 終了日時のDatetimePickerを提示
        return ask_datetime_picker("イベントの終了日時を選んでね", data="pick=end")

    # --- 終了日時の選択（DatetimePickerの戻り） ---
    if data == "pick=end" and draft.step == "end":
        dt = extract_dt_from_params(params)  # 終了日時を取り出す
        if not dt:
            # 取得失敗時は再提示
            return TextSendMessage(text="終了日時が取得できなかったよ。もう一度選んでね")
        # 終了が開始以前であればエラーとして再提示
        if draft.start_time and dt <= draft.start_time:
            return ask_datetime_picker("終了日時が開始日時よりも前になってるよ。もう一度選んでね", data="pick=end")
        # 終了日時を保存し、定員ステップへ遷移
        draft.end_time = dt
        draft.step = "cap"
        draft.save()
        # ここで「定員を設定するか？」のボタンを提示する（スキップ or 数値入力へ）
        return TemplateSendMessage(
            alt_text="定員の設定",
            template=ButtonsTemplate(
                title="定員",
                text="定員を設定する？",
                actions=[
                    PostbackAction(label="設定しない（スキップ）", data="cap=skip"),   # 押すと定員なしで確定へ
                    PostbackAction(label="定員を数字で入力する", data="cap=input"),   # 押すと数値入力ガイダンスへ
                ],
            ),
        )

    # --- 定員なし（スキップ）を選んだ場合 ---
    if data == "cap=skip" and draft.step == "cap":
        # capacityはNone（無制限）として確定
        draft.capacity = None
        draft.step = "done"
        draft.save()
        # Eventを作成して結果を返す
        return finalize_event(draft)

    # --- 定員を手入力に進む場合 ---
    if data == "cap=input" and draft.step == "cap":
        # 次のテキストで数値を入力してもらう
        return TextSendMessage(text="定員の数値を入力してね。（例: 10）")

    # それ以外のPostbackはウィザード対象外として無視
    return None


# ===== 補助関数群 =====

def ask_datetime_picker(prompt_text: str, data: str):
    """
    DatetimePicker（日時選択UI）を含むテンプレートメッセージを作成して返す。
    - prompt_text: ユーザーに表示する文言
    - data:       Postbackで戻す識別子（'pick=start' など）
    """
    template = ButtonsTemplate(                 # ボタンテンプレートを構築
        title="日時選択",                        # タイトル行
        text=prompt_text,                      # 本文（説明）
        actions=[                              # アクション（ここでは日時ピッカーを1つ）
            DatetimePickerAction(
                label="日時を選ぶ",             # ボタンに表示するラベル
                data=data,                     # 戻りのPostbackに含める識別子
                mode="datetime"                # 日付＋時刻の両方を選ばせる
            )
        ]
    )
    # ユーザーへ送れるテンプレートメッセージに包んで返す
    return TemplateSendMessage(alt_text="日時選択", template=template)


def parse_int_safe(s: str):
    """
    文字列を安全に整数へ変換する。数字のみで構成されていなければNoneを返す。
    """
    s = (s or "").strip()                      # まずは前後空白を削る
    if not re.fullmatch(r"\d+", s):            # 正規表現で「数字のみ」かを判定
        return None
    try:
        return int(s)                          # int変換を試みる
    except Exception:
        return None                            # 失敗時はNone


def extract_dt_from_params(params: dict):
    """
    DatetimePickerAction の Postback params から datetime を取り出し、timezone-aware にして返す。
    例: params = {'datetime': '2025-09-01T10:00'}
    """
    iso = params.get("datetime")               # 'YYYY-MM-DDTHH:MM' 形式が想定
    if not iso:
        return None
    # 'T' をスペースに置換し、秒を':00'で補完して 'YYYY-MM-DD HH:MM:SS' に整形
    dt_str = iso.replace("T", " ") + ":00"
    dt = parse_datetime(dt_str)                # 文字列→datetimeにパース
    if not dt:
        return None
    # naive（タイムゾーンなし）であれば、Djangoの現在のタイムゾーンを付与する
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def finalize_event(draft: "EventDraft"):
    """
    下書き（EventDraft）に溜めた値を元にEventを作成し、ユーザーへ作成結果を返す。
    """
    # 実際にEventを1件作成する（capacityはNoneも許容：定員なし）
    e = Event.objects.create(
        name=draft.name,
        start_time=draft.start_time,
        end_time=draft.end_time,
        capacity=draft.capacity,
    )
    # 完了メッセージを作って返す（IDや概要を含める）
    cap_text = "定員なし" if e.capacity is None else f"定員:{e.capacity}"
    summary = (
        "イベントを作成した！\n"
        f"ID:{e.id}\n"
        f"タイトル:{e.name}\n"
        f"開始:{e.start_time}\n"
        f"終了:{e.end_time}\n"
        f"{cap_text}"
    )
    return TextSendMessage(text=summary)


def handle_command(text, user_id):
    """
    ウィザード外の通常コマンドを処理するためのフックである。
    例：
      - 'イベント一覧' を受けて一覧を返す
      - '参加:ID' を受けて参加登録する（capacity=Noneは無制限として扱う 等）
    いまは説明簡潔化のため未実装でNoneを返す。必要に応じて既存実装を移植する。
    """
    return None
