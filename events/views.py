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

            # (1) 編集ドラフトがあるなら編集のテキスト優先
            if EventEditDraft.objects.filter(user_id=user_id).exists():
                reply = ew.handle_edit_text(user_id, text)
                if reply:
                    line_bot_api.reply_message(ev.reply_token, reply)
                    continue

            # (2) 作成開始の合言葉
            if text == "イベント作成":
                draft, _ = EventDraft.objects.get_or_create(user_id=user_id, defaults={"step": "title"})
                draft.step = "title"; draft.name = ""; draft.start_time = None
                draft.start_time_has_clock = False; draft.end_time = None; draft.end_time_has_clock = False
                draft.capacity = None; draft.scope_id = scope_id; draft.save()
                line_bot_api.reply_message(ev.reply_token, TextSendMessage(text="イベントのタイトルは？"))
                continue

            # (3) 作成ウィザード中のテキスト
            if EventDraft.objects.filter(user_id=user_id).exists():
                reply = cw.handle_wizard_text(user_id, text)
                if reply:
                    line_bot_api.reply_message(ev.reply_token, reply)
                    continue

            # (4) 一般コマンド（一覧/詳細/編集）
            reply_obj = cmd.handle_command(text, user_id, scope_id)
            if reply_obj:
                if isinstance(reply_obj, (TextSendMessage, TemplateSendMessage, list)):
                    line_bot_api.reply_message(ev.reply_token, reply_obj)
                elif isinstance(reply_obj, str):
                    line_bot_api.reply_message(ev.reply_token, TextSendMessage(text=reply_obj))
            else:
                line_bot_api.reply_message(ev.reply_token, ui.ask_home_menu())

        # --- Postback ---
        elif isinstance(ev, PostbackEvent):
            user_id = ev.source.user_id
            data = ev.postback.data or ""
            params = ev.postback.params or {}

            # 一覧カルーセル等のショートカット（detail/edit）を最優先
            shortcut = cmd.handle_evt_shortcut(user_id, scope_id, data)
            if shortcut:
                line_bot_api.reply_message(ev.reply_token, shortcut)
                continue

            # 編集ドラフトがあるなら編集ポストバック優先
            if EventEditDraft.objects.filter(user_id=user_id).exists():
                reply = ew.handle_edit_postback(user_id, scope_id, data, params)
                if reply:
                    line_bot_api.reply_message(ev.reply_token, reply)
                    continue

            # 作成ウィザードのポストバック
            reply = cw.handle_wizard_postback(user_id, data, params, scope_id)
            if reply:
                line_bot_api.reply_message(ev.reply_token, reply)

    return HttpResponse(status=200)