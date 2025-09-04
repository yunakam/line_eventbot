# events/views.py
import os
import re
import unicodedata
from datetime import timedelta, datetime

from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.core.exceptions import ImproperlyConfigured
from django.conf import settings
from django.utils.dateparse import parse_datetime
from django.utils import timezone

from linebot import LineBotApi, WebhookParser
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    PostbackEvent,
    DatetimePickerAction, TemplateSendMessage, PostbackAction
)
from linebot.exceptions import InvalidSignatureError

from .models import Event, EventDraft, EventEditDraft
from . import ui, utils
from .handlers import create_wizard as cw, edit_wizard as ew, commands as cmd

import logging
logger = logging.getLogger(__name__)


def _is_home_menu_trigger(text: str) -> bool:
    """
    'ボット' / 'ぼっと' / 'BOT'(全半角・大小文字許容) / 🤖 のときだけ True
    """
    if not text:
        return False
    # 絵文字は正規化せずダイレクトに判定（バリエーションセレクタも吸収）
    if "🤖" in text:
        return True

    # 全角半角の差やケース差を吸収して 'bot' と一致させる
    norm = unicodedata.normalize("NFKC", text).strip().lower()
    if norm in ("ボット", "ぼっと", "bot"):
        return True
    return False


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

    line_bot_api, parser = get_line_clients()

    signature = request.headers.get("X-Line-Signature", "")
    body = request.body.decode("utf-8")
    try:
        events = parser.parse(body, signature)
    except InvalidSignatureError:
        return HttpResponse(status=400)

    for ev in events:
        scope_id = _resolve_scope_id(ev)

        # --- Text ---
        if isinstance(ev, MessageEvent) and isinstance(ev.message, TextMessage):
            user_id = ev.source.user_id
            text = ev.message.text.strip()
            
            # ボット起動語彙は最優先で処理。イベントドラフトを破棄してホームメニュー
            if _is_home_menu_trigger(text):
                # ドラフトがある場合は破棄してリセット
                EventDraft.objects.filter(user_id=user_id).delete()
                EventEditDraft.objects.filter(user_id=user_id).delete()
                line_bot_api.reply_message(ev.reply_token, ui.ask_home_menu("home=launch"))
                continue

            # 編集ドラフトがあるなら編集のテキスト優先
            if EventEditDraft.objects.filter(user_id=user_id).exists():
                reply = ew.handle_edit_text(user_id, text)
                if reply:
                    line_bot_api.reply_message(ev.reply_token, reply)
                    continue

            # 作成ウィザード中のテキスト
            if EventDraft.objects.filter(user_id=user_id).exists():
                reply = cw.handle_wizard_text(user_id, text)
                if reply:
                    reply = ui.attach_exit_qr(reply)
                    line_bot_api.reply_message(ev.reply_token, reply)
                    continue

            # それ以外のテキストは全てメニューへ誘導
            line_bot_api.reply_message(ev.reply_token, ui.ask_home_menu("home=help"))
            continue


        # --- Postback ---        
        elif isinstance(ev, PostbackEvent):
            user_id = ev.source.user_id
            data = ev.postback.data or ""
            params = ev.postback.params or {}

            if data == "back_home":
                line_bot_api.reply_message(ev.reply_token, ui.ask_home_menu())
                continue

            # 1) 一覧/詳細/編集のショートカットを最優先で処理
            shortcut = cmd.handle_evt_shortcut(user_id, scope_id, data)
            if shortcut:
                if isinstance(shortcut, (TextSendMessage, TemplateSendMessage)):
                    shortcut.quick_reply = ui.make_quick_reply(show_home=True, show_exit=True)
                line_bot_api.reply_message(ev.reply_token, shortcut)
                continue

            # 2) ホームメニューの「イベント一覧」
            if data == "home=list":
                qs = Event.objects.filter(scope_id=scope_id).order_by("-id")[:10]
                reply = ui.render_event_list(qs)  # QRはこの時点では付いていない
                if isinstance(reply, list):
                    reply = ui.attach_exit_qr(reply)
                else:
                    reply.quick_reply = ui.make_quick_reply(show_home=True, show_exit=True)
                line_bot_api.reply_message(ev.reply_token, reply)
                continue

            # 3) 編集ドラフトがあるなら編集ポストバック優先
            if EventEditDraft.objects.filter(user_id=user_id).exists():
                reply = ew.handle_edit_postback(user_id, scope_id, data, params)
                if reply:
                    line_bot_api.reply_message(ev.reply_token, reply)
                    continue

            # 4) 作成ウィザードのポストバック（home=create / endmode等 含む）
            reply = cw.handle_wizard_postback(user_id, data, params, scope_id)
            if reply:
                line_bot_api.reply_message(ev.reply_token, reply)


    return HttpResponse(status=200)