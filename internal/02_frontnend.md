## 2. フロントエンド（JS / HTML / CSS）の設計

### 2-1. イベント作成フォームのモーダル

`liff_app.html` と `liff.css` によって、モーダルUIが構成されている。

例: 終了時刻 or 所要時間を切り替えるUI。

```html
<fieldset class="endpicker">
  <div class="endmode-switch">
    <label class="radio-pill">
      <input type="radio" name="endmode" value="time" checked> 終了時刻
    </label>
    <label class="radio-pill">
      <input type="radio" name="endmode" value="duration"> 所要時間
    </label>
  </div>
  <div class="end-input">
    <label class="subrow when-time">
      <span>終了時刻</span>
      <input id="f-end" name="end_time" type="time">
    </label>
    <label class="subrow when-duration">
      <span>所要時間</span>
      <input id="f-duration" name="duration" type="text" placeholder="例: 1:30 / 90m / 2h / 120">
    </label>
  </div>
</fieldset>
```

対応するCSS:

```css
.endpicker:has(input[name="endmode"][value="duration"]:checked) .when-time { display: none; }
.endpicker:has(input[name="endmode"][value="duration"]:checked) .when-duration { display: grid; }
```

**ポイント解説:**

* `:has()` 擬似クラスを使って、ラジオボタンの選択状態に応じて入力欄を動的に切り替えている。
* JavaScript不要でUXを改善している点がポイント。
* `duration` 入力はサーバ側で柔軟にパースされる（例: `1:30`, `90m`, `2h`, `120`分）。

---

### 2-2. イベント一覧の描画

`liff.js` 内で APIからイベント一覧を取得し、カードとして描画している。

```js
async function loadAndRender() {
  const data = (scopeId && /^U/.test(scopeId))
    ? await api.fetchMyEvents()
    : await api.fetchEvents();

  const items = (data && data.items) || [];
  listEl.innerHTML = items.map((e) => {
    const name = e.name || "（無題）";
    const range = buildLocalRange(e.start_time, !!e.start_time_has_clock, e.end_time);
    const cap = (e.capacity === null) ? "定員なし" : `定員: ${e.capacity}`;

    const rsvpButtons = `<button class="btn-primary" data-act="rsvp-join" data-id="${e.id}">参加</button>`;
    return `
      <article class="card" data-id="${e.id}">
        <h3>${escapeHtml(name)}</h3>
        <p>${escapeHtml(range)}</p>
        <p>${escapeHtml(cap)}</p>
        <div class="actions">${rsvpButtons}</div>
      </article>
    `;
  }).join("");
}
```

**ポイント解説:**

* DjangoのAPI `/api/events` が返すJSONをフロントで受け取り、カードHTMLを動的生成している。
* `scopeId` に応じて「自分のイベント一覧」or「共有グループの一覧」を切り替える設計。
* 開始時刻が未設定の場合は日付のみを表示するように整形されている（`buildLocalRange` 関数）。

---

### 2-3. 参加者リストのUI

イベント作成者は「参加者」ボタンを押すと、参加者のLINEアイコン＋名前が一覧表示される。

```js
const listHtml = (arr) => (
  arr && arr.length
    ? `<ul class="att-grid">${
        arr.map(a => `
          <li class="att-cell">
            ${a.pictureUrl ? `<img class="att-avatar" src="${a.pictureUrl}">` : `<span class="att-avatar placeholder"></span>`}
            <div class="att-name">${escapeHtml(a.name)}</div>
          </li>`).join("")
      }</ul>`
    : `<p class="muted">まだいません</p>`
);
```

対応するCSS:

```css
.att-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(72px, 1fr));
  gap: 10px;
}
.att-cell {
  display: grid;
  justify-items: start;
  row-gap: 6px;
}
.att-avatar { width: 48px; height: 48px; border-radius: 50%; }
.att-name { font-size: 12px; color: #333; }
```

**ポイント解説:**

* LINEの `get_group_member_profile` API を用いてサーバ側で名前・アイコンを取得し、フロントに渡している。
* 参加者はグリッド表示され、モバイルでも見やすいUIになっている。
* 「ウェイトリスト」も同じUIで別セクションに表示する。

---
