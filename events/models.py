# events/models.py
from django.db import models
from django.utils import timezone

class KnownGroup(models.Model):
    group_id     = models.CharField(max_length=64, unique=True, db_index=True)
    name         = models.CharField(max_length=255, blank=True, default="")
    picture_url  = models.TextField(blank=True, default="")
    joined       = models.BooleanField(default=True)  # 退出検知でFalseにする想定
    last_seen_at = models.DateTimeField(default=timezone.now)
    last_summary_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.name or self.group_id
    
class Event(models.Model):
    name = models.CharField(max_length=200)
    start_time = models.DateTimeField()
    start_time_has_clock = models.BooleanField(default=True)
    end_time = models.DateTimeField(null=True, blank=True)
    capacity = models.IntegerField(null=True, blank=True)
    created_by = models.CharField(max_length=50, null=True, blank=True) 
    scope_id = models.CharField(max_length=128, null=True, blank=True, db_index=True)

    def __str__(self):
        return self.name
    
class Participant(models.Model):
    user_id = models.CharField(max_length=50)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="participants")
    joined_at = models.DateTimeField(auto_now_add=True)
    is_waiting = models.BooleanField(default=False)


# ---- イベント作成の進行状態を保存する下書き ---- #
class EventDraft(models.Model):
    """
    ユーザー単位で「いまの質問段階」と「入力済フィールド」を保持
    ユーザーは「イベント作成」とだけ入力すれば、以降は質問に答えるだけ
    """
    STEP_CHOICES = [
        ("title", "タイトル入力待ち"),
        ("start_date", "開始日入力待ち"),
        ("start_time", "開始時刻入力待ち"),
        ("end_mode",   "終了指定方法の選択待ち"),
        # ("end_date",   "終了日入力待ち"),     # 終了日 = 開始日のため未実装
        ("end_time",   "終了時刻入力待ち"),
        ("duration",   "所要時間入力待ち"),
        ("cap",        "定員入力待ち"),
        ("done",       "完了"),
    ]
    user_id = models.CharField(max_length=50, unique=True)  # ユーザーごとに1件の下書き
    scope_id = models.CharField(max_length=128, null=True, blank=True)
    
    step = models.CharField(max_length=10, choices=STEP_CHOICES, default="title")
    
    # 一時保存カラム
    name = models.CharField(max_length=200, blank=True, default="")
    start_time = models.DateTimeField(null=True, blank=True)
    start_time_has_clock = models.BooleanField(default=False)
    end_time_has_clock = models.BooleanField(
        default=False,
        help_text="終了時刻が HH:MM で指定された場合に True"
    )
    end_time = models.DateTimeField(null=True, blank=True)
    capacity = models.IntegerField(null=True, blank=True)


# ---- イベント編集の進行状態を保存する下書き ---- #
class EventEditDraft(models.Model):
    """
    ユーザーごとに、どのイベントをどの項目まで編集しているかを保存する。
    """
    STEP_CHOICES = [
        ("menu", "編集項目メニュー表示中"),
        ("title", "タイトル編集中"),
        ("start_date", "開始日編集中"),
        ("start_time", "開始時刻編集中"),
        ("end_mode", "終了指定方法選択中"),
        ("end_time", "終了時刻編集中"),
        ("duration", "所要時間編集中"),
        ("cap", "定員編集中"),
        ("confirm", "確認"),
    ]
    user_id = models.CharField(max_length=50, unique=True)
    scope_id = models.CharField(max_length=128, null=True, blank=True)

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="edit_drafts")
    step = models.CharField(max_length=12, choices=STEP_CHOICES, default="menu")

    # 一時保存カラム（未入力なら元イベント値を採用する想定）
    name = models.CharField(max_length=200, blank=True, default="")
    start_time = models.DateTimeField(null=True, blank=True)
    start_time_has_clock = models.BooleanField(default=False)
    end_time = models.DateTimeField(null=True, blank=True)
    end_time_has_clock = models.BooleanField(default=False)
    capacity = models.IntegerField(null=True, blank=True)
