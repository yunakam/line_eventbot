# 4. Django（JS経験者向け）

### 4-1. Djangoを「APIサーバ」として捉える

フロント（`liff.js`）からのリクエストは、すべてDjangoのAPIエンドポイント（例: `/api/events`）に向かう。

その中身を覗くと、**REST APIに近い構造**を持っている。

```python
@csrf_exempt
def events_list(request):
    if request.method == 'GET':
        # 一覧取得
        ...
        return JsonResponse({'ok': True, 'items': items}, status=200)

    if request.method == 'POST':
        # イベント作成
        ...
        return JsonResponse({'ok': True, 'item': {...}}, status=201)
```

**JSエンジニア視点:**

* Express/Koaで `app.get("/events")`や `app.post("/events")`を書くのと近い感覚。
* 違いは「型やモデルがDjango ORMで厳格に定義されている」こと。これによりフロントから渡すJSONとDBの整合性をサーバ側で保証できる。

---

### 4-2. 状態管理をDBに逃がす設計

ReactやVueではフォーム入力の途中状態を `useState`や `Vuex`に保持する。

本プロジェクトでは、それを **`EventDraft` モデルとしてDBに保存**している。

```python
class EventDraft(models.Model):
    user_id = models.CharField(max_length=50, unique=True)
    step = models.CharField(max_length=10, choices=STEP_CHOICES, default="title")
    name = models.CharField(max_length=200, blank=True, default="")
    start_time = models.DateTimeField(null=True, blank=True)
```

**JSエンジニア視点:**

* 「入力中の状態をDBに持たせる＝Reduxのストアをサーバに置いた感じ」。
* これにより、ユーザーがログアウト・再ログインしても状態が保持される。
* フロント側のコードがシンプルになり、セッションまたぎでも安全。

---

### 4-3. バリデーションはサーバ主導

JS側でも入力チェックをしているが、最終的にはサーバ側で再検証している。

```python
if not name or not date_str:
    return JsonResponse({'ok': False, 'reason': 'name and date are required'}, status=400)

if start_hhmm:
    start_dt = utils.hhmm_to_utc_on_same_day(base_dt, start_hhmm)
    if start_dt is None:
        return JsonResponse({'ok': False, 'reason': 'invalid start_time'}, status=400)
```

**JSエンジニア視点:**

* JSでは「必須チェック」や「形式チェック」はフロントで済ませがち。
* しかしセキュリティ上、必ずサーバで再検証している。
* これはNode.jsでも同じ考え方を適用できるので、良い実践例となる。

---

### 4-4. LINE特有の制約にどう対応しているか

普通のWebアプリにはない制約がある。

1. **WebView（LIFF内）でしか動かない**

   → PC検証のため、`withLoginOnExternalBrowser: true` を利用。
2. **Botがグループにいないとプロフィールが取れない**

   ```python
   if scope_id.startswith('C'):
       prof = line_bot_api.get_group_member_profile(scope_id, uid)
   ```

→ 取得できないときはIDだけ返し、UIはフォールバックする設計。

3. **テキストにリンクを埋め込めない**

   → Flex Messageを利用して解決。

**JSエンジニア視点:**

* LINEの制約は「ブラウザが制限付きサンドボックスで動く」と考えると理解しやすい。
* その上でどう回避策を取るか（Flex Message、再ログイン、プロフィールフォールバックなど）が学びになる。

---

### 4-5. Python/Djangoならではの学び

* **Django ORM = TypeScriptの型安全なモデル定義のようなもの**

  JSでの `zod`や `Prisma`に近いイメージ。
* **デコレータ（@csrf_exempt, @handler.addなど）**

  → Expressの `app.use(middleware)`のデコレータ版。
* **ビュー関数 = APIルート**

  → JSエンジニアが理解しやすい設計。

---

### まとめ（JSエンジニアに響くポイント）

1. Djangoは「APIサーバ」として理解すれば抵抗が少ない。
2. 状態管理をDBに持たせる設計は新鮮で学びになる。
3. バリデーションをサーバで必ずやる姿勢はNode.jsにも応用可能。
4. LINE特有の制約（リンク不可、プロフィール取得制限など）をどう工夫しているかが実践的。

---

👉 これでポイント1〜5まで一通り整理した。

この資料をベースに「社内勉強会スライド」や「Wiki記事」に落とし込むと、JSエンジニアでもLINEミニアプリ開発の全体像を掴みやすいと思う。

次は、このまとめを **スライド用の構成案** として変換してみようか？（例: 1スライドに1テーマで要点だけ）
