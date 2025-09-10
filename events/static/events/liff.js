// events/static/events/liff.js
// 役割: LIFF初期化、イベント一覧取得、作成ダイアログから保存まで

(() => {
  // ---- 状態（モジュール内） ----
  let idToken = "";
  let scopeId = "";  // groupId または userId
  let liffReady = false;

  let currentUserId = "";     // ← デコードしたIDトークンの sub
  let isEditing = false;      // ← モーダルが編集用途かどうか
  let editingId = null;       // ← 編集対象のイベントID
  let gItems = [];            // ← 一覧の最新データを保持

  // ---- ユーティリティ ----
  const $ = (sel) => document.querySelector(sel);
  const $all = (sel) => Array.from(document.querySelectorAll(sel));

  function getScopeIdFromUrl() {
    const qs = new URLSearchParams(location.search);
    // グループ優先、なければ1:1のuserId、なければ空
    return qs.get("groupId") || qs.get("userId") || "";
  }

  function showDialog(mode = "create") {
    $("#create-backdrop").hidden = false;
    $("#create-dialog").hidden = false;
    // タイトル文言を切り替え
    const title = (mode === "edit") ? "イベント編集" : "イベント作成";
    $("#dlg-title").textContent = title;
    $("#f-title").focus();
  }
  
  function hideDialog() {
    $("#create-backdrop").hidden = true;
    $("#create-dialog").hidden = true;
    // 編集状態をクリア
    isEditing = false;
    editingId = null;
  }

  function clearForm() {
    $("#create-form").reset();
    hideError("#err-title");
    hideError("#err-date");
    // 初期状態: endmode=time の行可視・duration非表示に戻す
    $("#row-endtime").hidden = false;
    $("#row-duration").hidden = true;
  }

  function isoToLocalYmd(iso) {
    const d = new Date(iso);
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const dd = String(d.getDate()).padStart(2, "0");
    return `${y}-${m}-${dd}`;
  }
  function isoToLocalHhmm(iso) {
    const d = new Date(iso);
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    return `${hh}:${mm}`;
  }

  function openEditDialog(id) {
    const item = (gItems || []).find(x => Number(x.id) === Number(id));
    if (!item) return;

    // 既存値をフォームに流し込む
    $("#f-title").value = item.name || "";
    $("#f-date").value  = item.start_time ? isoToLocalYmd(item.start_time) : "";
    $("#f-start").value = (item.start_time && item.start_time_has_clock) ? isoToLocalHhmm(item.start_time) : "";

    // 終了は「時刻入力」タブに寄せておく（duration かどうか判別しづらいため）
    document.querySelector('input[name="endmode"][value="time"]').checked = true;
    $("#row-endtime").hidden = false;
    $("#row-duration").hidden = true;
    $("#f-end").value = item.end_time ? isoToLocalHhmm(item.end_time) : "";
    $("#f-duration").value = "";

    $("#f-cap").value = (item.capacity == null ? "" : String(item.capacity));

    isEditing = true;
    editingId = id;
    showDialog("edit");  // タイトル差し替え
  }

  async function confirmDelete(id, name) {
    if (!window.confirm(`「${name || '（無題）'}」を削除する？`)) return;

    const token = await ensureFreshIdToken();
    if (!token) {
      if (forceReloginOnce(false)) return;
      return;
    }
    try {
      await api.deleteEvent(id, token);
      alert("削除したよ");
      await loadAndRender();
    } catch (err) {
      const msg = String(err && err.message || err || "");
      if (/IdToken expired/i.test(msg) || /invalid[_ ]?token/i.test(msg)) {
        if (forceReloginOnce(false)) return;
      }
      alert(`削除に失敗したよ: ${msg}`);
    }
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

    // ここから下を関数内にまとめる
    gIdToken = liff.getIDToken() || "";
    if (!gIdToken) throw new Error("no id_token");

    // 現在ユーザーID（sub）をここで記録
    try {
      const dec = liff.getDecodedIDToken && liff.getDecodedIDToken();
      currentUserId = (dec && dec.sub) || "";
    } catch {}

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

  async function validateGroupSelection({ silent = true } = {}) {
    const typed   = ($("#f-group")?.value || "").trim();
    const urlGrp  = new URLSearchParams(location.search).get("groupId") || "";
    const token   = await ensureFreshIdToken();
    const pat     = /^[CR][0-9a-f]{32}$/i;

    // ローカル形式チェック
    if (typed && !pat.test(typed)) {
      if (!silent) alert("不正なIDだよ");
      const rn = $("#row-notify"), fn = $("#f-notify"), gp = $("#group-preview");
      if (rn) rn.style.display = "none";
      if (fn) { fn.checked = false; fn.disabled = true; }
      if (gp) gp.style.display = "none";
      return { ok: false };
    }

    const candidate = typed || urlGrp;
    if (!candidate) {
      const rn = $("#row-notify"), fn = $("#f-notify"), gp = $("#group-preview");
      if (rn) rn.style.display = "none";
      if (fn) { fn.checked = false; fn.disabled = true; }
      if (gp) gp.style.display = "none";
      return { ok: false };
    }

    // サーバ検証
    const res = await fetch(`/api/groups/validate`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify({ id_token: token, group_id: candidate }),
    });
    const data = await res.json().catch(() => ({}));

    if (res.status === 401) {
      // トークン不正/期限切れ
      const rn = $("#row-notify"), fn = $("#f-notify"), gp = $("#group-preview");
      if (rn) rn.style.display = "none";
      if (fn) { fn.checked = false; fn.disabled = true; }
      if (gp) gp.style.display = "none";
      if (!silent) alert("ログインの有効期限が切れたみたい。もう一度ログインしてね");
      return { ok: false };
    }

    if (!res.ok || !data.ok) {
      // groupId 不正 or Bot未参加など（400）
      const rn = $("#row-notify"), fn = $("#f-notify"), gp = $("#group-preview");
      if (rn) rn.style.display = "none";
      if (fn) { fn.checked = false; fn.disabled = true; }
      if (gp) gp.style.display = "none";
      if (!silent) alert("不正なIDだよ");
      return { ok: false };
    }

    // 成功 → プレビューと通知UIを解放
    const gp = $("#group-preview"), gi = $("#group-icon"), gn = $("#group-name");
    if (gp && gi && gn) {
      const name = data.group?.name || candidate;
      const pic  = data.group?.pictureUrl || "";
      if (pic) { gi.src = pic; gi.style.display = "inline-block"; }
      else { gi.style.display = "none"; }
      gn.textContent = name;
      gp.style.display = "flex";
    }
    const rn = $("#row-notify"), fn = $("#f-notify");
    if (rn) rn.style.display = "flex";
    if (fn) fn.disabled = false;

    return { ok: true, groupId: data.groupId || candidate };
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

    async updateEvent(id, payload) {
      const res = await fetch(`/api/events/${id}`, {
        method: "PATCH",
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
    async deleteEvent(id, idToken) {
      const res = await fetch(`/api/events/${id}`, {
        method: "DELETE",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "Accept": "application/json" },
        body: JSON.stringify({ id_token: idToken }),
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

      gItems = items;  // ← 一覧をキャッシュ
      listEl.innerHTML = items.map((e) => {
        const name = e.name || "（無題）";

        // 追加：開始〜終了の1行レンジを生成
        const range = buildLocalRange(e.start_time, !!e.start_time_has_clock, e.end_time);

        const cap = (e.capacity === null || e.capacity === undefined)
          ? "定員なし"
          : `定員: ${e.capacity}`;

        const isAuthor = !!e.created_by && !!currentUserId && (e.created_by === currentUserId);

        return `
          <article class="card" data-id="${e.id}">
            <h3>${escapeHtml(name)}</h3>
            <p>${escapeHtml(range)}</p>
            <p>${escapeHtml(cap)}</p>
            ${isAuthor ? `
              <div class="actions">
                <button class="btn-outline" data-act="edit" data-id="${e.id}">編集</button>
                <button class="btn-outline dangerous" data-act="delete" data-id="${e.id}" data-name="${escapeHtml(name)}">削除</button>
              </div>
            ` : ``}
          </article>
        `;
      }).join("");


    } catch (err) {
      console.error(err);
      listEl.innerHTML = `<p class="muted">読み込みに失敗したよ</p>`;
    }
  }

  // ISO文字列を端末ローカル時刻で整形する。hasClock=false の場合は日付のみ。
  function formatLocalDateTime(iso, hasClock) {
    if (!iso) return "未設定";
    const d = new Date(iso);
    const y  = d.getFullYear();
    const m  = String(d.getMonth() + 1).padStart(2, "0");
    const dd = String(d.getDate()).padStart(2, "0");
    if (!hasClock) return `${y}/${m}/${dd}`;
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    return `${y}/${m}/${dd} ${hh}:${mm}`;
  }

    // ISO2つから「YYYY/MM/DD HH:MM ~ HH:MM」等の1行レンジを作る
  function buildLocalRange(startIso, hasClock, endIso) {
    if (!startIso) return "未設定";
    const s = new Date(startIso);

    const sy  = s.getFullYear();
    const sm  = String(s.getMonth() + 1).padStart(2, "0");
    const sd  = String(s.getDate()).padStart(2, "0");

    if (!hasClock) {
      // 開始が日付のみ → 日付だけ出す
      return `${sy}/${sm}/${sd}`;
    }

    const sh = String(s.getHours()).padStart(2, "0");
    const smin = String(s.getMinutes()).padStart(2, "0");
    const startStr = `${sy}/${sm}/${sd} ${sh}:${smin}`;

    if (!endIso) return startStr;

    const e = new Date(endIso);
    const ey  = e.getFullYear();
    const em  = String(e.getMonth() + 1).padStart(2, "0");
    const ed  = String(e.getDate()).padStart(2, "0");
    const eh  = String(e.getHours()).padStart(2, "0");
    const emin = String(e.getMinutes()).padStart(2, "0");

    // 同日なら「~ HH:MM」、日付が跨るなら終端も日付から出す
    const sameDay = (sy === ey && sm === em && sd === ed);
    return sameDay
      ? `${startStr} ~ ${eh}:${emin}`
      : `${startStr} ~ ${ey}/${em}/${ed} ${eh}:${emin}`;
  }

  function escapeHtml(s) {
    s = (s == null) ? "" : String(s);
    return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }


  // ---- フォームの保存ハンドラ ----
  async function handleSave() {
    // 必須チェック
    const name = ($("#f-title").value || "").trim();
    const date = ($("#f-date").value || "").trim();
    const start_time = ($("#f-start").value || "").trim();
    const endmode = ($all('input[name="endmode"]').find(r => r.checked)?.value || "").trim();
    const end_time = ($("#f-end").value || "").trim();
    const duration = ($("#f-duration").value || "").trim();
    const capacity = ($("#f-cap").value || "").trim();

    let hasError = false;
    if (!name) { showError("#err-title"); hasError = true; } else { hideError("#err-title"); }
    if (!date) { showError("#err-date");  hasError = true; } else { hideError("#err-date"); }
    if (hasError) return;

    const token = await ensureFreshIdToken();
    if (!token) {
      if (forceReloginOnce(true)) return;
      return;
    }

    // 共有先バリデーション → scope_id / notify 決定
    const urlHasGroup = /[?&]groupId=/.test(location.search);
    const inputGroup  = ($("#f-group")?.value || "").trim();
    const notifyChecked = !!$("#f-notify")?.checked;

    let chosenScopeId = scopeId;  // 既定は現コンテキスト（1:1なら userId / グループなら groupId）
    let notify = false;

    // URLにgroupIdがある / 入力がある / 通知ON のいずれかなら validate を強制
    if (urlHasGroup || inputGroup || notifyChecked) {
      const v = await validateGroupSelection({ silent: false }); // ← 保存時はアラート許可
      if (!v.ok) {
        alert("共有するグループを選んでね");
        return;
      }
      chosenScopeId = v.groupId;
      notify = notifyChecked;
    }

    const payload = {
      id_token: token,
      name, date,
      start_time,
      endmode,
      end_time,
      duration,
      capacity: capacity ? Number(capacity) : null,
      scope_id: chosenScopeId,
      notify,                   // グループへの通知フラグ
    };

    try {
      $("#btn-save").disabled = true;

      if (isEditing && editingId != null) {
        await api.updateEvent(editingId, payload);
      } else {
        await api.createEvent(payload);
      }

      hideDialog();
      clearForm();
      alert("保存したよ");
      await loadAndRender();
      sessionStorage.removeItem(REL_LOGIN_FLAG);
    } catch (err) {
      const msg = String(err && err.message || err || "");
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

    // 共有グループ入力で validate を発火（blur / change / input）
    const grp = document.querySelector('#f-group');
    if (grp) {
      const run = () => validateGroupSelection().catch(() => {});
      grp.addEventListener('blur',   run);
      grp.addEventListener('change', run);
      let t=null;
      grp.addEventListener('input', () => { clearTimeout(t); t=setTimeout(run, 400); });
    }

    // URLにgroupIdがある場合は自動validate（グループから開いたケース）
    if (/[?&]groupId=/.test(location.search)) {
      try { await validateGroupSelection(); } catch {}
    }

    // LIFF初期化 → 一覧表示（略）
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


    $("#event-list").addEventListener("click", (ev) => {
      const btn = ev.target.closest("button[data-act]");
      if (!btn) return;
      const act = btn.dataset.act;
      const id  = Number(btn.dataset.id);
      if (act === "edit") {
        openEditDialog(id);
      } else if (act === "delete") {
        confirmDelete(id, btn.dataset.name || "");
      }
    });

  });

})();
