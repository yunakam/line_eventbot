// liff.js

// ---- ユーティリティ -------------------------------------------------

/** クエリ文字列から値を取得する */
function getQuery(name) {
  return new URLSearchParams(location.search).get(name);
}

// モーダル開閉
function openCreateDialog() {
  document.getElementById("create-backdrop")?.removeAttribute("hidden");
  document.getElementById("create-dialog")?.removeAttribute("hidden");
  // 初期値：今日の日付をセット
  const d = new Date();
  const yyyy = d.getFullYear(), mm = String(d.getMonth()+1).padStart(2,"0"), dd = String(d.getDate()).padStart(2,"0");
  const $date = document.getElementById("f-date");
  if ($date && !$date.value) $date.value = `${yyyy}-${mm}-${dd}`;
  document.getElementById("f-title")?.focus();
}
function closeCreateDialog() {
  document.getElementById("create-backdrop")?.setAttribute("hidden","");
  document.getElementById("create-dialog")?.setAttribute("hidden","");
}


/** ISO8601文字列をJSTの "YYYY-MM-DD HH:mm:ss" に整形する */
function formatIsoToJst(isoStr) {
  if (!isoStr) return "";
  const d = new Date(isoStr); // ISO(+00:00等)をUTCとして解釈
  const fmt = new Intl.DateTimeFormat("ja-JP", {
    timeZone: "Asia/Tokyo",
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
    hour12: false,
  });
  const parts = Object.fromEntries(fmt.formatToParts(d).map(p => [p.type, p.value]));
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
}

/** JSON取得（/api/events と /api/events/ の両方を順に試す） */
async function fetchEventsJson() {
  const urls = ["/api/events", "/api/events/"];
  let lastErr;
  for (const u of urls) {
    try {
      const res = await fetch(u, { credentials: "same-origin" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return await res.json();
    } catch (e) {
      lastErr = e;
    }
  }
  throw lastErr || new Error("イベント取得に失敗したよ");
}

/** シンプルなエラーメッセージ表示 */
function setMessage(msg) {
  const root = document.getElementById("event-list");
  if (!root) return;
  root.innerHTML = `<p class="muted">${msg}</p>`;
}

/** イベント一覧のDOM描画 */
function renderEvents(events) {
  const root = document.getElementById("event-list");
  if (!root) return;

  if (!Array.isArray(events) || events.length === 0) {
    root.innerHTML = `<p class="muted">イベントはまだないよ。右上メニューか「イベント作成」から作ってね。</p>`;
    return;
  }

  const html = events
    .map(ev => {
      const title = ev.title || ev.name || "（無題）";
      // 開始日時候補（API側のフィールド名ゆれに対応）
      const startIso = ev.start_time || ev.start || ev.start_at || ev.startDateTime || ev.starts_at || "";
      const when = formatIsoToJst(startIso) || "日時未設定";
      return `
        <div class="card">
          <h3>${title}</h3>
          <p>${when}</p>
        </div>
      `;
    })
    .join("");

  root.innerHTML = html;
}

/** イベント作成ダイアログを開く */
function goCreate() {
  openCreateDialog();
}


// ---- LIFF 初期化と起動フロー ---------------------------------------
document.addEventListener("DOMContentLoaded", async () => {
  // 1) LIFF ID 解決（テンプレ or クエリ）
  const urlLiffId = getQuery("liffClientId");
  const templateLiffId = (window.LIFF_ID && String(window.LIFF_ID).trim()) || "";
  const liffId = templateLiffId || urlLiffId;

  if (!liffId) {
    console.error("LIFF ID not found: pass LIFF_ID via template or liffClientId via query.");
    alert("初期化に失敗したよ（LIFF IDが未設定だよ）");
    setMessage("初期化できなかったよ。設定を確認してね。");
    return;
  }

  // 2) 作成ボタンのイベント
  const btn = document.getElementById("btn-create");
  if (btn) btn.addEventListener("click", goCreate);

  // ここから追加：モーダルのボタン動作
  document.getElementById("btn-cancel")?.addEventListener("click", closeCreateDialog);
  document.getElementById("create-backdrop")?.addEventListener("click", closeCreateDialog);
  document.getElementById("btn-save")?.addEventListener("click", async () => {
    // 入力値を集める
    const title = document.getElementById("f-title")?.value?.trim();
    const date  = document.getElementById("f-date")?.value;
    const start = document.getElementById("f-start")?.value;
    const end   = document.getElementById("f-end")?.value;

    if (!title || !date) { alert("タイトルと日付は必須だよ"); return; }

    // 必要に応じてAPIにPOST。ここでは例として /api/events へ投げる。
    // サーバ側の仕様に合わせてフィールド名は調整してほしい。
    try {
      const res = await fetch("/api/events", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({
          title, date, start_time: start || null, end_time: end || null
        })
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);

      // 成功：モーダルを閉じて一覧を再読込
      closeCreateDialog();
      setMessage("保存したよ。更新中…");
      const data = await fetchEventsJson();
      const events = Array.isArray(data) ? data : (data.results || data.items || []);
      renderEvents(events);
    } catch (e) {
      console.error("create failed:", e);
      alert("保存に失敗したよ");
    }
  });

  try {
    // 3) LIFF 初期化
    await liff.init({ liffId });
    if (!liff.isLoggedIn()) {
      liff.login({ redirectUri: location.href });
      return;
    }

    // 4) イベント一覧取得 → 描画
    setMessage("読み込み中…");
    const data = await fetchEventsJson();
    const events = Array.isArray(data) ? data : (data.results || data.items || []);
    renderEvents(events);
  } catch (err) {
    console.error("[LIFF] init/render error:", err);
    setMessage("読み込みに失敗したよ。時間をおいて再試行してね。");
  }
});
