# events/models.py
from django.db import models

class Event(models.Model):
    name = models.CharField(max_length=200)
    start_time = models.DateTimeField()
    start_time_has_clock = models.BooleanField(default=True)
    end_time = models.DateTimeField(null=True, blank=True)
    capacity = models.IntegerField(null=True, blank=True)

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
