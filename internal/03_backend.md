
## 3. バックエンド（Django/Python）の設計知見

### 3-1. データモデルの分離設計

`models.py` では、 **イベント** （`Event`）、 **参加者** （`Participant`）、 **下書き** （`EventDraft` / `EventEditDraft`）を別テーブルに分離している。

```python
class Event(models.Model):
    name = models.CharField(max_length=200)
    start_time = models.DateTimeField()
    start_time_has_clock = models.BooleanField(default=True)
    end_time = models.DateTimeField(null=True, blank=True)
    capacity = models.IntegerField(null=True, blank=True)
    created_by = models.CharField(max_length=50, null=True, blank=True) 
    scope_id = models.CharField(max_length=128, null=True, blank=True, db_index=True)

class Participant(models.Model):
    user_id = models.CharField(max_length=50)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="participants")
    joined_at = models.DateTimeField(auto_now_add=True)
    is_waiting = models.BooleanField(default=False)

class EventDraft(models.Model):
    user_id = models.CharField(max_length=50, unique=True)
    step = models.CharField(max_length=10, choices=STEP_CHOICES, default="title")
    name = models.CharField(max_length=200, blank=True, default="")
    start_time = models.DateTimeField(null=True, blank=True)
```

**解説:**

* `Event` …確定済みのイベント
* `Participant` …イベントとユーザーを結びつけ、`is_waiting`でウェイトリストも表現
* `EventDraft` / `EventEditDraft` …フォーム入力途中の情報をサーバ側で保持

**JSエンジニア視点:**

* React/Vueでの `useState`やReduxストアに近い。
* 違いは「入力途中の状態をDBに保存」する点で、セッションをまたいでも状態が保持される。
* 「状態管理をサーバに逃がす」という発想はフロント開発にも応用できる。

---

### 3-2. スコープIDによる情報分離

イベントが「どのグループ／ユーザーに属するか」を識別するキーが `scope_id`。

サーバ側で必ず `scope_id` を条件に絞っている。

```python
qs = Event.objects.filter(
    Q(created_by=user_id) |
    Q(created_by__isnull=True, scope_id=user_id) |
    Q(created_by="", scope_id=user_id)
)
```

**解説:**

* これにより、Aグループで作成したイベントがBグループから見えてしまう事故を防止。
* LINEのマルチグループ利用を前提とした重要なセキュリティ設計。

**JSエンジニア視点:**

* Express/Koaで「必ず `req.user.id`や `req.group.id`でフィルタする」のと同じ。
* スコープを意識せずクエリすると**即情報漏洩**に直結するので、特に注意すべき部分。

---

### 3-3. IDトークンのサーバ検証

イベント作成や編集のAPIでは、必ずLINE公式APIでトークンを検証している。

```python
res = requests.post(
    'https://api.line.me/oauth2/v2.1/verify',
    data={'id_token': id_token, 'client_id': getattr(settings, 'MINIAPP_CHANNEL_ID', '')},
    timeout=10
)
vr = res.json()
if res.status_code != 200:
    return JsonResponse({'ok': False, 'reason': vr}, status=401)
user_id = vr.get('sub') or ''
```

**解説:**

* フロントから渡されたIDトークンをそのまま信じず、LINE公式に問い合わせて真正性を確認。
* 検証結果からユーザーID（`sub`）を得て、DB操作の主体とする。

**JSエンジニア視点:**

* Node.jsでもJWTを扱うことは多いが、LINEでは必ず**外部APIで検証**するのが特徴。
* `jsonwebtoken.verify()`に慣れていると、ここでの設計思想の違いは学びになる。

---

### 3-4. 通知メッセージの送信（Flex Message）

イベント作成時には、Flex Messageを用いてグループに通知できる。

```python
flex_contents = {
  "type": "bubble",
  "body": {
    "type": "box",
    "layout": "vertical",
    "contents": [
      { "type": "text", "text": f"「{e.name}」が作成されました！", "weight": "bold" },
      { "type": "text", "text": "グループのイベントはここから見れるよ",
        "action": { "type": "uri", "label": "ここ", "uri": liff_url } }
    ]
  }
}
msg = FlexSendMessage(
  alt_text=f"「{e.name}」が作成されました！グループのイベントは {liff_url} から見れるよ",
  contents=flex_contents
)
line_bot_api.push_message(scope_id, msg)
```

**解説:**

* LINEではテキストにリンクを直接埋め込めないため、Flex Messageを使う。
* `alt_text` は通知領域や古い端末用に必須。

**JSエンジニア視点:**

* SlackやDiscordの「リッチメッセージ」と同様の概念。
* UIをBotがレンダリングする仕組みと考えると理解しやすい。

---

### 3-5. バリデーションとセキュリティ

バックエンドでは、フロントで済ませていても**必ず再度検証**している。

```python
if not name or not date_str:
    return JsonResponse({'ok': False, 'reason': 'name and date are required'}, status=400)

if start_hhmm:
    start_dt = utils.hhmm_to_utc_on_same_day(base_dt, start_hhmm)
    if start_dt is None:
        return JsonResponse({'ok': False, 'reason': 'invalid start_time'}, status=400)
```

**JSエンジニア視点:**

* フロントでの入力チェックは **UX向上のため** 、サーバでのチェックは **セキュリティ担保のため** 。
* Expressでも同じだが、Djangoは標準で型・バリデーションを強めに扱う点が特徴。

---

## 小まとめ

* **Django = APIサーバ** として理解すれば、JS経験者でも親しみやすい。
* **状態管理をDBに逃がす設計**は新鮮で、フロント開発にも応用可能。
* **スコープ管理とIDトークン検証**がセキュリティの要。
* **Flex Messageでの通知**は「LINEの制約下でUXを工夫する」好例。

---
