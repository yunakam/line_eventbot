// events/static/events/liff.js
// 役割: LIFF初期化、イベント一覧取得、作成ダイアログから保存まで

(() => {
  // ---- 状態（モジュール内） ----
  let idToken = "";
  let scopeId = "";  // groupId または userId
  let liffReady = false;

  // ---- ユーティリティ ----
  const $ = (sel) => document.querySelector(sel);
  const $all = (sel) => Array.from(document.querySelectorAll(sel));

  function getScopeIdFromUrl() {
    const qs = new URLSearchParams(location.search);
    // グループ優先、なければ1:1のuserId、なければ空
    return qs.get("groupId") || qs.get("userId") || "";
  }

  function showDialog() {
    $("#create-backdrop").hidden = false;
    $("#create-dialog").hidden = false;
    $("#f-title").focus();
  }
  function hideDialog() {
    $("#create-backdrop").hidden = true;
    $("#create-dialog").hidden = true;
  }
  function clearForm() {
    $("#create-form").reset();
    hideError("#err-title");
    hideError("#err-date");
    // 初期状態: endmode=time の行可視・duration非表示に戻す
    $("#row-endtime").hidden = false;
    $("#row-duration").hidden = true;
  }
  function showError(sel) { const el = $(sel); if (el) el.hidden = false; }
  function hideError(sel) { const el = $(sel); if (el) el.hidden = true; }

  // ドラフトをセッションに保存（ログインで離脱しても入力を保持）
  function saveDraftToSession() {
    const draft = {
      name: ($("#f-title").value || "").trim(),
      date: ($("#f-date").value || "").trim(),
      start_time: ($("#f-start").value || "").trim(),
      endmode: ($all('input[name="endmode"]').find(r => r.checked)?.value || "").trim(),
      end_time: ($("#f-end").value || "").trim(),
      duration: ($("#f-duration").value || "").trim(),
      capacity: ($("#f-cap").value || "").trim()
    };
    sessionStorage.setItem("eventDraft", JSON.stringify(draft));
  }
  function restoreDraftFromSession() {
    const raw = sessionStorage.getItem("eventDraft");
    if (!raw) return false;
    try {
      const d = JSON.parse(raw);
      $("#f-title").value = d.name || "";
      $("#f-date").value = d.date || "";
      $("#f-start").value = d.start_time || "";
      if (d.endmode) {
        const radio = document.querySelector(`input[name="endmode"][value="${d.endmode}"]`);
        if (radio) radio.checked = true;
        $("#row-endtime").hidden = (d.endmode !== "time");
        $("#row-duration").hidden = (d.endmode !== "duration");
      }
      $("#f-end").value = d.end_time || "";
      $("#f-duration").value = d.duration || "";
      $("#f-cap").value = d.capacity || "";
      sessionStorage.removeItem("eventDraft");
      showDialog(); // 復帰時にモーダルを再表示
      return true;
    } catch { return false; }
  }

    // 1回だけ強制再ログインするためのフラグ（無限ループ防止）
  const REL_LOGIN_FLAG = "didForceReloginOnce";

  function forceReloginOnce(withDraft = false) {
    if (withDraft) saveDraftToSession();
    if (sessionStorage.getItem(REL_LOGIN_FLAG)) {
      // 既に一度やった → これ以上はやらない
      alert("ログインの更新に失敗したみたい。時間を置いて試してね");
      return false;
    }
    sessionStorage.setItem(REL_LOGIN_FLAG, "1");
    const redirectUri = (window.LIFF_REDIRECT_ABS || location.href).replace(/^http:/, 'https:');
    try { liff.logout(); } catch {}
    liff.login({ redirectUri });
    return true;
  }


  // LIFFトークン管理
  let gIdToken = "";

  // expが近い/切れているか（30秒マージン）
  function isTokenStale() {
    try {
      const dec = liff.getDecodedIDToken && liff.getDecodedIDToken();
      if (!dec || !dec.exp) return false;
      const now = Math.floor(Date.now() / 1000);
      return now >= (dec.exp - 30);
    } catch { return false; }
  }

  async function initLiffAndLogin(liffId) {
    await liff.init({ liffId, withLoginOnExternalBrowser: true });
    if (!liff.isLoggedIn()) {
      // サーバから受け取った絶対URLを優先し、http → https 置換
      const redirectUri = (window.LIFF_REDIRECT_ABS || location.href).replace(/^http:/, 'https:');
      liff.login({ redirectUri });
      return false;
    }

    gIdToken = liff.getIDToken() || "";
    if (!gIdToken) throw new Error("no id_token");
    return true;
  }

  async function ensureFreshIdToken() {
    // まず最新を拾う
    gIdToken = liff.getIDToken() || "";
    if (!gIdToken) return "";  // 無い場合は呼び出し側で対処
    // expが近い/過ぎているなら "" を返し、呼び出し側で強制再ログイン
    if (isTokenStale()) return "";
    return gIdToken;
  }


  // ---- APIラッパ ----
  const api = {
    async fetchEvents() {
      const res = await fetch(`/api/events?scope_id=${encodeURIComponent(scopeId)}`, {
        credentials: "same-origin",
        headers: { "Accept": "application/json" },
      });
      if (!res.ok) throw new Error(`fetch events failed: ${res.status}`);
      return await res.json();
    },
    async createEvent(payload) {
      // サーバでIDトークンを再検証するため、ここで id_token を必ず渡す
      const res = await fetch(`/api/events`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "Accept": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        const r = data.reason ? (typeof data.reason === "string" ? data.reason : JSON.stringify(data.reason)) : `HTTP ${res.status}`;
        throw new Error(r);
      }
      return data;
    },
  };


  // ---- 一覧描画 ----
  async function loadAndRender() {
    const listEl = $("#event-list");
    listEl.innerHTML = `<p class="muted">読み込み中…</p>`;
    try {
      const data = await api.fetchEvents();
      const items = (data && data.items) || [];
      if (!items.length) {
        listEl.innerHTML = `<p class="muted">イベントはまだないよ</p>`;
        return;
      }
      listEl.innerHTML = items.map((e) => {
        const name = e.name || "（無題）";
        const start = e.start_time ? e.start_time.replace("T", " ").slice(0, 16) : "未設定";
        const cap = (e.capacity === null || e.capacity === undefined) ? "定員なし" : `定員: ${e.capacity}`;
        return `
          <article class="card">
            <h3>${escapeHtml(name)}</h3>
            <p>開始: ${escapeHtml(start)}</p>
            <p>${escapeHtml(cap)}</p>
          </article>
        `;
      }).join("");
    } catch (err) {
      console.error(err);
      listEl.innerHTML = `<p class="muted">読み込みに失敗したよ</p>`;
    }
  }

  function escapeHtml(s) {
    s = (s == null) ? "" : String(s);
    return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }


  // ---- LIFF初期化＆IDトークン取得 ----
  async function initLiffAndAuth() {
    const liffId = (window.LIFF_ID || "").trim();
    if (!liffId) {
      alert("初期化に失敗したよ（LIFF IDが未設定だよ）");
      throw new Error("LIFF_ID missing");
    }
await liff.init({ liffId, withLoginOnExternalBrowser: true });

    if (!liff.isLoggedIn()) {
      // ここでリダイレクト先を明示（/liff/ 固定のはず）
      const redirectUri = (window.LIFF_REDIRECT_ABS || location.href);
      liff.login({ redirectUri });
      return false; // 以降はリダイレクト後に実行される
    }
    idToken = liff.getIDToken() || "";
    if (!idToken) {
      alert("初期化に失敗したよ（IDトークンが取得できないよ）");
      throw new Error("no idToken");
    }
    // 参考：検証APIはサーバ作成時にも呼ぶため、ここでのverifyは省略可
    liffReady = true;
    return true;
  }


  // ---- フォームの保存ハンドラ ----
  async function handleSave() {
    // 必須入力チェック
    const name = ($("#f-title").value || "").trim();
    const date = ($("#f-date").value || "").trim();
    const start_time = ($("#f-start").value || "").trim(); // 任意
    const endmode = ($all('input[name="endmode"]').find(r => r.checked)?.value || "").trim();
    const end_time = ($("#f-end").value || "").trim();
    const duration = ($("#f-duration").value || "").trim();
    const capacity = ($("#f-cap").value || "").trim();

    let hasError = false;
    if (!name) { showError("#err-title"); hasError = true; } else { hideError("#err-title"); }
    if (!date) { showError("#err-date");  hasError = true; } else { hideError("#err-date"); }
    if (hasError) return;


    // 直前に鮮度チェック。足りなければ一度だけ強制リフレッシュへ。
    const token = await ensureFreshIdToken();
    if (!token) {
      if (forceReloginOnce(true)) return; // ドラフト保存→ログアウト→ログイン → 復帰後に再試行
      return; // 既に一度試していればここで終了
    }

    const payload = {
      id_token: token,
      name, date,
      start_time,
      endmode,
      end_time,
      duration,
      capacity: capacity ? Number(capacity) : null,
      scope_id: scopeId,
    };

    try {
      $("#btn-save").disabled = true;
      await api.createEvent(payload);
      hideDialog();
      clearForm();
      alert("保存したよ");
      await loadAndRender();
      // 成功したのでフラグは掃除
      sessionStorage.removeItem(REL_LOGIN_FLAG);

    } catch (err) {
      const msg = String(err && err.message || err || "");
      // 期限切れ or 無効トークンのときだけ再ログインを試す
      if (/IdToken expired/i.test(msg) || /invalid[_ ]?token/i.test(msg)) {
        if (forceReloginOnce(true)) return;
      }
      console.error(err);
      alert(`保存に失敗したよ: ${msg}`);

    } finally {
      $("#btn-save").disabled = false;
    }

  }


  // ---- 画面初期化 ----
  document.addEventListener("DOMContentLoaded", async () => {
    scopeId = getScopeIdFromUrl();

    // endmode 切替（終了時刻 or 所要時間）
    $all('input[name="endmode"]').forEach(radio => {
      radio.addEventListener("change", () => {
        const v = $all('input[name="endmode"]').find(r => r.checked)?.value;
        $("#row-endtime").hidden = (v !== "time");
        $("#row-duration").hidden = (v !== "duration");
      });
    });

    $("#btn-create").addEventListener("click", () => { showDialog(); });
    $("#btn-cancel").addEventListener("click", () => { hideDialog(); });
    $("#create-backdrop").addEventListener("click", () => { hideDialog(); });
    $("#btn-save").addEventListener("click", () => { handleSave(); });

    // LIFF初期化（新：トークン管理付き）→ 一覧表示
    try {
      const liffId = (window.LIFF_ID || "").trim();
      if (!liffId) {
        alert("初期化に失敗したよ（LIFF IDが未設定だよ）");
        $("#event-list").innerHTML = `<p class="muted">初期化に失敗したよ</p>`;
        return;
      }
      const ok = await initLiffAndLogin(liffId);
      if (ok) {
        await loadAndRender();
        // 復帰時はドラフトだけ復元（フラグはここでは消さない：ループ防止）
        restoreDraftFromSession();
      }


    } catch (e) {
      console.error(e);
      $("#event-list").innerHTML = `<p class="muted">初期化に失敗したよ</p>`;
    }
  });

})();
