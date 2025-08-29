# events/models.py
from django.db import models

class Event(models.Model):
    name = models.CharField(max_length=100)
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    capacity = models.IntegerField(null=True, blank=True)  # ← null/blankを許可

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
        ("end_mode",   "終了指定方法の選択待ち"),  # 「終了日時」か「所要時間」か
        ("end_date",   "終了日入力待ち"),
        ("end_time",   "終了時刻入力待ち"),
        ("duration",   "所要時間入力待ち"),
        ("cap",        "定員入力待ち"),
        ("done",       "完了"),
    ]
    user_id = models.CharField(max_length=50, unique=True)  # ユーザーごとに1件の下書き
    step = models.CharField(max_length=10, choices=STEP_CHOICES, default="title")
    
    # 一時保存カラム
    name = models.CharField(max_length=100, blank=True)
    start_time = models.DateTimeField(null=True, blank=True)
    end_time = models.DateTimeField(null=True, blank=True)
    capacity = models.IntegerField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
