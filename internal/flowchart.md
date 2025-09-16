# **フローチャート**

## **【作成者】イベント作成〜通知**

```
[ユーザー（1:1チャット）]
    │  0) 「イベント」と送信 → BotがLIFFリンクを返信（1:1スコープ）
    │
    ▼
[フロント(LIFF/JS)]
    │  1) リンクをタップしてLIFF起動（?userId=...付き）
    │
    │  2) 初期化・ログイン・IDトークン取得
    │     （URLの userId を scopeId として把握）
    │
    │  3) 「イベント作成」モーダルを開く
    │     ・タイトル／日付／開始時刻／終了(時刻or所要)／定員を入力
    │     ・任意で「共有するグループ」を選択 → サーバへ検証依頼
    │
    │  4) 「保存」クリック
    │     ・id_token と入力値（scope_id／notify を含む）をAPIに送信
    │
    ▼
[サーバ（Django/API）]
    │  5) IDトークン検証（正規ユーザー確認）
    │
    │  6) 入力バリデーション＆日時の正規化
    │
    │  7) Event を保存（created_by / scope_id を付与）
    │
    │  8) notify=ON かつ scope がグループなら
    │     ・一覧URL（LIFF URL）を生成して Flex Message を送信
    │
    │  9) JSON で結果を返す
    │
    ▼
[フロント(LIFF/JS)]
    │ 10) 成功メッセージ表示（「保存したよ」）
    │     ・一覧を再取得して再描画
    │
    └─ 完了（グループにも作成通知が届く）
```

#### 根拠コード（参照ポイント）

* **初期化・ログイン・IDトークン取得・scopeId補完**

  `initLiffAndLogin()`, `ensureFreshIdToken()`, `forceReloginOnce()`, URLから `groupId/userId`を読み取り → `scopeId` に反映
* **作成モーダルUI（終了入力の切替・フォーム構造）**

  `liff_app.html` のフォーム／モーダル構造、`:has()`で「終了時刻/所要時間」をCSSだけで切替
* **共有グループの検証（プレビュー表示・通知チェック解放）**

  `validateGroupSelection()` が `/api/groups/validate` を呼び、結果に応じてプレビューや通知UIを制御
* **保存リクエスト送信（作成/更新）**

  `handleSave()` → `api.createEvent()` / `api.updateEvent()` に `id_token` と入力値（`scope_id`, `notify` など）を同送
* **保存後のUI更新**

  成功時のアラート（「保存したよ」）と `loadAndRender()` による一覧の再取得・再描画

---



## **【作成者】イベント編集→保存→一覧更新**

```
[ユーザー（作成者）]
    │  1) 一覧カードの「編集」をクリック
    │
    ▼
[フロント(LIFF/JS)]
    │  2) クリックハンドラで act=edit を検出
    │
    │  3) openEditDialog(id)
    │     ・該当イベントを gItems から取得
    │     ・フォーム各欄へ既存値を流し込み
    │     ・isEditing=true / editingId=id
    │     ・モーダルを「イベント編集」タイトルで表示
    │
    │  4) （任意）共有グループ欄を変更したら validateGroupSelection()
    │     ・ID形式チェック→サーバ検証→プレビュー／通知UI切替
    │
    │  5) 「保存」をクリック → handleSave()
    │     ・必須チェック（タイトル/日付）
    │     ・ensureFreshIdToken()（失効なら再ログイン誘導）
    │     ・scopeId/notify を決定（URL or 入力欄→必要に応じ validate）
    │     ・isEditing && editingId なら PATCH を選択
    │
    ▼
[サーバ（Django/API）]
    │  6) /api/events/{id} に PATCH
    │     ・IDトークン検証（verify）
    │     ・入力バリデーション＆時刻の正規化
    │     ・Event を更新（created_by 権限チェック含む）
    │     ・JSON応答（ok / エラー理由）
    │
    ▼
[フロント(LIFF/JS)]
    │  7) 成功 → モーダル閉じる＆フォームクリア
    │     ・「保存したよ」
    │     ・loadAndRender() で一覧再取得→再描画
    │
    └─ 完了（編集内容がカードに反映される）
```

#### 根拠コード（参照ポイント）

* 「編集」ボタンクリック配線／act判定／`openEditDialog(id)` 呼び出し
* `openEditDialog(id)`：既存値のフォーム流し込み、編集フラグ設定、ダイアログ表示
* 共有グループの検証 `validateGroupSelection()`：ID形式→サーバ `/api/groups/validate`→プレビュー/通知UI
* `handleSave()`：必須チェック→IDトークン確保→scope/notify 決定→**更新時は `api.updateEvent(editingId, payload)`（PATCH）**
* 成功後：`hideDialog()`→`clearForm()`→「保存したよ」→`loadAndRender()` で一覧更新

---



## 【作成者】イベント削除

```
[ユーザー（作成者）]
    │  1) 一覧カードの「削除」をクリック
    │
    ▼
[フロント(LIFF/JS)]
    │  2) クリックハンドラで act=delete を検出
    │
    │  3) confirmDelete(id, name)
    │     ・ブラウザの確認ダイアログ
    │     ・キャンセルなら中断
    │
    │  4) ensureFreshIdToken() で id_token を取得
    │     ・無ければ再ログイン誘導（1回だけ）→中断
    │
    │  5) /api/events/{id} へ DELETE を送信（id_token 同送）
    │
    ▼
[サーバ（Django/API）]
    │  6) IDトークン検証 → 作成者権限チェック
    │     ・OKなら Event を削除
    │     ・JSON応答（ok / error）
    │
    ▼
[フロント(LIFF/JS)]
    │  7) 成功: 「削除したよ」を表示
    │     ・loadAndRender() で一覧を再取得・再描画
    │
    │  8) 失敗:
    │     ・トークン失効/不正→再ログイン誘導（1回だけ）
    │     ・その他エラー→「削除に失敗したよ: ...」
    │
    └─ 完了（一覧から該当イベントが消える）
```

#### 根拠コード（参照ポイント）

* 「削除」ボタンクリック配線（`data-act="delete"`）と分岐
* `confirmDelete(id, name)`：確認ダイアログ→`ensureFreshIdToken()`→`api.deleteEvent(id, token)`→成功時「削除したよ」→`loadAndRender()`
* 失敗時の扱い：`IdToken expired` / `invalid token` 検知で `forceReloginOnce(false)` を発火、その他はアラート表示
* DELETE APIラッパ：`api.deleteEvent(id, idToken)` が `/api/events/{id}` に  **DELETE** （`{ id_token }` をJSONボディで送信）

---



## 【参加者】イベント一覧閲覧→**参加 → キャンセル**

```
[グループメンバー]
    │  1) グループ内の「イベント一覧」リンクをタップ（LIFF起動: ?groupId=... 付き）
    │
    ▼
[フロント(LIFF/JS)]
    │  2) 初期化: liff.init → 未ログインなら liff.login → id_token取得
    │     ・URLから groupId を取得して scopeId として保持
    │
    │  3) 一覧取得と描画
    │     ・/api/events?scope_id={groupId} をGET
    │     ・カードを生成（作成者ではないので「参加」ボタンが表示）
    │
    │  4) 参加操作（参加ボタン）
    │     ・クリック → /api/events/{id}/rsvp に POST（id_token 同送）
    │     ・結果に応じてメッセージ:
    │         - "参加登録したよ" / "もう参加登録しているよ" / "ウェイトリストに登録したよ"
    │     ・一覧を再取得してカードを更新
    │
    │  5) キャンセル操作（キャンセルボタン）
    │     ・クリック → /api/events/{id}/rsvp に DELETE（id_token 同送）
    │     ・"キャンセルしたよ" を表示
    │     ・一覧を再取得してカードを更新
    │
    └─ 完了（参加状態がUIに反映される）
```

#### 根拠コード（参照ポイント）

* **LIFF初期化・ログイン・IDトークン取得** : `initLiffAndLogin()` / `ensureFreshIdToken()` / `forceReloginOnce()`。
* **一覧取得と描画** : `api.fetchEvents()`（scopeId があれば `?scope_id=` 付きでGET）→ `loadAndRender()` がカード生成。
* **非作成者のカードに「参加」ボタン** : `isCreator` 判定で作成者以外は `rsvp-join` ボタンを配置。
* **参加（JOIN）** : `api.joinEvent(id)` が `/api/events/{id}/rsvp` に  **POST** （`id_token` 必須）。結果 `"waiting"|"already"|成功` に応じて  「ウェイトリストに登録したよ / もう参加登録しているよ / 参加登録したよ」を `alert` し、`loadAndRender()` で再描画。
* **キャンセル** : `api.cancelRsvp(id)` が `/api/events/{id}/rsvp` に  **DELETE** （`id_token`）し、  「キャンセルしたよ」を表示 → `loadAndRender()`。
* **ボタン配線** : 一覧のクリックハンドラで `data-act="rsvp-join"` / `"rsvp-cancel"` を分岐処理。
* **モーダルやリストの土台HTML/CSS** : `liff_app.html`（一覧描画先 `#event-list` など）、`liff.css`（カード/ボタンなどのスタイル）。

補足：トークン失効時は `ensureFreshIdToken()` → 失効検知で `forceReloginOnce()` を発火し、再ログイン後にフローを継続できる設計。

---



## 【作成者】参加者一覧の展開

```
[ユーザー（作成者）]
    │  1) 一覧カードの「参加者」ボタンをクリック
    │
    ▼
[フロント(LIFF/JS)]
    │  2) クリックハンドラが act=members を検出
    │
    │  3) トグル動作
    │     ・開いていれば閉じる（非表示にして終了）
    │     ・閉じていれば表示して "読み込み中だよ..." を表示
    │
    │  4) /api/events/{id}/participants へ POST（id_token 同送）
    │
    │  5) 応答に基づき描画
    │     ・参加者: アイコン＋名前のグリッド
    │     ・ウェイトリスト: 同様に表示
    │     ・定員ラベル（定員が未設定なら「定員なし」）
    │     ・空なら「まだいません」
    │
    │  6) 失敗時
    │     ・エラーメッセージをボックス内に表示
    │     ・トークン失効など一部ケースでは再ログイン誘導
    │
    └─ 完了（再クリックで閉じる／他カードでも同様に表示）
```

#### 根拠コード（参照ポイント）

* **クリック配線とトグル** ：一覧領域のクリック委任で `data-act="members"` を判定し、同じカード内の `att-<eventId>` ボックスを開閉。初回は「読み込み中だよ...」を表示してから取得に進む。
* **参加者API呼び出し** ：`fetchParticipants(eventId)` が `id_token` を付けて `/api/events/{id}/participants` に  **POST** 。サーバ応答を検証し、失敗時は例外化。
* **描画テンプレート** ：参加者・ウェイトリストをそれぞれ `<ul class="att-grid">` で出力し、各要素は `<li class="att-cell">` 配下にアイコン（`att-avatar`）と名前（`att-name`）を縦配置。空配列時は「まだいません」を表示。
* **スタイル（グリッド/アバター/名前）** ：`att-grid`（自動折返しグリッド）、`att-cell`（左寄せ縦並び）、`att-avatar`（丸型48px）、`att-name`（12px・折返し耐性）などのCSS定義。

---

## 【全ユーザー】**トークン失効→再ログイン→ドラフト復元→保存再試行**

```
[ユーザー]
    │  1) 「保存」または「参加/キャンセル」などの操作を実行
    │
    ▼
[フロント(LIFF/JS)]
    │  2) ensureFreshIdToken() を呼び出し
    │     ・有効な id_token があれば 続行
    │     ・無い/期限切れなら 中断フラグで戻る
    │
    │  3) （id_token 無し/失効）→ forceReloginOnce(withDraft)
    │     ・withDraft=true の場合は saveDraftToSession() で入力内容を保存
    │     ・再ログインを1回だけ発火（無限ループ防止フラグを設定）
    │     ・scopeHint（groupId/userId）も必要に応じて保存
    │
    ▼
[LINEログイン画面]
    │  4) ログイン後、LIFFにリダイレクト
    │
    ▼
[フロント(LIFF/JS) 復帰時]
    │  5) URLに scopeId が無ければ scopeHint で補完→history.replaceState で書き戻し
    │
    │  6) restoreDraftFromSession()
    │     ・保存しておいたドラフトがあれば
    │       - フォーム各欄へ復元
    │       - モーダルを自動で再表示（編集再開）
    │
    │  7) ユーザーが再度「保存」や「参加/キャンセル」を実行
    │     ・この時点では新しい id_token が取得可能
    │
    └─ 完了（操作成功。「保存したよ」「参加登録したよ」等を表示し一覧更新）
```

### 根拠コード（参照ポイント）

* **トークンの鮮度判定・取得** ：`ensureFreshIdToken()` と `isTokenStale()`（`liff.getDecodedIDToken().exp` を参照）
* **1回だけの強制再ログインとドラフト保存** ：`forceReloginOnce(withDraft)`／`saveDraftToSession()`／`REL_LOGIN_FLAG` によるループ防止、`scopeHint` 保存
* **復帰時のドラフト復元とモーダル再表示** ：`restoreDraftFromSession()`（入力値を復元し `showDialog()` で再開）
* **scopeId の補完とURL書き戻し** ：`scopeHint` を使って `history.replaceState` で `groupId`/`userId` をURLに復元
* **保存/参加系の再試行動線** ：`handleSave()`／`api.joinEvent()`／`api.cancelRsvp()` 内で id_token 不足・失効を検知したら `forceReloginOnce(...)` を発火→復帰後に再操作
* **成功時のフィードバックと一覧更新** ：成功アラート（「保存したよ」「参加登録したよ」「キャンセルしたよ」）→ `loadAndRender()` で再描画
