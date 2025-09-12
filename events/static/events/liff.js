// events/static/events/liff.js
// 役割: LIFF初期化、イベント一覧描画、作成/編集/削除、参加/キャンセル、参加者表示
// I/F互換: 既存のDOM/エンドポイント/ボタン data-act を維持

(() => {
  // ==============================
  // 0) モジュール内状態
  // ==============================
  let scopeId = "";            // groupId or userId（URL or 復元）
  let currentUserId = "";      // IDトークンのsub
  let gItems = [];             // 直近のイベント一覧キャッシュ
  const REL_LOGIN_FLAG = "didForceReloginOnce"; // 再ログイン一度だけ
  let lastValidatedGroupId = "";                 // 直近でOKだった groupId を保持

  // ==============================
  // 1) ユーティリティ
  // ==============================
  const $ = (sel) => document.querySelector(sel);
  const $all = (sel) => Array.from(document.querySelectorAll(sel));
  const escapeHtml = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const getScopeIdFromUrl = () => {
    const qs = new URLSearchParams(location.search);
    return qs.get("groupId") || qs.get("userId") || "";
  };

  const isoToLocalYmd = (iso) => {
    const d = new Date(iso);
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const dd = String(d.getDate()).padStart(2, "0");
    return `${y}-${m}-${dd}`;
  };
  const isoToLocalHhmm = (iso) => {
    const d = new Date(iso);
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    return `${hh}:${mm}`;
  };

  const showDialog = (mode = "create") => {
    $("#create-backdrop").hidden = false;
    $("#create-dialog").hidden = false;
    $("#dlg-title").textContent = mode === "edit" ? "イベントを編集" : "イベントを作成";
    $("#f-title").focus();
  };
  const hideDialog = () => {
    $("#create-backdrop").hidden = true;
    $("#create-dialog").hidden = true;
    isEditing = false;
    editingId = null;
  };
  const clearForm = () => {
    $("#create-form").reset();
    hideError("#err-title");
    hideError("#err-date");
  };

  const showError = (sel) => { const el = $(sel); if (el) el.hidden = false; };
  const hideError = (sel) => { const el = $(sel); if (el) el.hidden = true; };

  // 1回だけドラフト保存（再ログイン誘導時）
  const saveDraftToSession = () => {
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
  };
  const restoreDraftFromSession = () => {
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
      }
      $("#f-end").value = d.end_time || "";
      $("#f-duration").value = d.duration || "";
      $("#f-cap").value = d.capacity || "";
      sessionStorage.removeItem("eventDraft");
      showDialog(); // 復帰時にモーダル再表示
      return true;
    } catch { return false; }
  };

  // ==============================
  // 2) LIFFトークン管理
  // ==============================
  let gIdToken = "";

  const isTokenStale = () => {
    try {
      const dec = liff.getDecodedIDToken && liff.getDecodedIDToken();
      if (!dec?.exp) return false;
      const now = Math.floor(Date.now() / 1000);
      return now >= (dec.exp - 30); // 30秒マージン
    } catch { return false; }
  };

  const initLiffAndLogin = async (liffId) => {
    await liff.init({ liffId, withLoginOnExternalBrowser: true });
    if (!liff.isLoggedIn()) {
      // scopeヒント保存（groupId/userIdがURLにあれば）
      const hasScope = /[?&](groupId|userId)=/.test(location.search);
      if (hasScope) {
        const qs = new URLSearchParams(location.search);
        const hint = qs.get("groupId") ? `groupId=${qs.get("groupId")}` : (qs.get("userId") ? `userId=${qs.get("userId")}` : "");
        if (hint) sessionStorage.setItem("scopeHint", hint);
      }
      let redirectUri = location.href.replace(/^http:/, 'https:');
      if (!hasScope) redirectUri = (window.LIFF_REDIRECT_ABS || redirectUri).replace(/^http:/, 'https:');
      liff.login({ redirectUri });
      return false;
    }
    gIdToken = liff.getIDToken() || "";
    if (!gIdToken) throw new Error("no id_token");
    try {
      const dec = liff.getDecodedIDToken && liff.getDecodedIDToken();
      currentUserId = dec?.sub || "";
    } catch {}
    return true;
  };

  const ensureFreshIdToken = async () => {
    gIdToken = liff.getIDToken() || "";
    if (!gIdToken) return "";
    if (isTokenStale()) return "";
    return gIdToken;
  };

  // 無限ループを避けつつ再ログイン誘導
  const forceReloginOnce = (withDraft = false) => {
    if (withDraft) saveDraftToSession();
    if (sessionStorage.getItem(REL_LOGIN_FLAG)) {
      alert("ログインの更新に失敗したみたい。時間を置いて試してね");
      return false;
    }
    sessionStorage.setItem(REL_LOGIN_FLAG, "1");

    // scopeヒント保存
    const hasScope = /[?&](groupId|userId)=/.test(location.search);
    if (hasScope) {
      const qs = new URLSearchParams(location.search);
      const hint = qs.get("groupId") ? `groupId=${qs.get("groupId")}` : (qs.get("userId") ? `userId=${qs.get("userId")}` : "");
      if (hint) sessionStorage.setItem("scopeHint", hint);
    }

    // redirectUriはscope付きURLを優先
    let redirectUri = location.href.replace(/^http:/, 'https:');
    if (!hasScope) redirectUri = (window.LIFF_REDIRECT_ABS || redirectUri).replace(/^http:/, 'https:');
    try { liff.logout(); } catch {}
    liff.login({ redirectUri });
    return true;
  };

  // ==============================
  // 3) サーバAPIラッパ
  // ==============================
  const api = {
    async fetchEvents() {
      const url = (scopeId && String(scopeId).trim())
        ? `/api/events?scope_id=${encodeURIComponent(scopeId)}`
        : `/api/events`;
      const res = await fetch(url, { credentials: "same-origin", headers: { "Accept": "application/json" } });
      if (!res.ok) throw new Error(`fetch events failed: ${res.status}`);
      return await res.json();
    },
    async fetchMyEvents() {
      const token = await ensureFreshIdToken();
      if (!token) { forceReloginOnce(false); throw new Error("id_token missing"); }
      const res = await fetch(`/api/events/mine`, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "Accept": "application/json" },
        body: JSON.stringify({ id_token: token }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) throw new Error(data?.reason || `HTTP ${res.status}`);
      return { items: data.items || [] };
    },
    async createEvent(payload) {
      const res = await fetch(`/api/events`, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "Accept": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) throw new Error(data.reason ? (typeof data.reason === "string" ? data.reason : JSON.stringify(data.reason)) : `HTTP ${res.status}`);
      return data;
    },
    async updateEvent(id, payload) {
      const res = await fetch(`/api/events/${id}`, {
        method: "PATCH", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "Accept": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) throw new Error(data.reason ? (typeof data.reason === "string" ? data.reason : JSON.stringify(data.reason)) : `HTTP ${res.status}`);
      return data;
    },
    async deleteEvent(id, idToken) {
      const res = await fetch(`/api/events/${id}`, {
        method: "DELETE", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "Accept": "application/json" },
        body: JSON.stringify({ id_token: idToken }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) throw new Error(data.reason ? (typeof data.reason === "string" ? data.reason : JSON.stringify(data.reason)) : `HTTP ${res.status}`);
      return data;
    },
    async joinEvent(id) {
      const token = await ensureFreshIdToken();
      if (!token) { forceReloginOnce(false); throw new Error("id_token missing"); }
      const res = await fetch(`/api/events/${id}/rsvp`, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "Accept": "application/json" },
        body: JSON.stringify({ id_token: token }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) throw new Error(data.reason ? (typeof data.reason === "string" ? data.reason : JSON.stringify(data.reason)) : `HTTP ${res.status}`);
      return data;
    },
    async cancelRsvp(id) {
      const token = await ensureFreshIdToken();
      if (!token) throw new Error("id_token missing");
      const res = await fetch(`/api/events/${id}/rsvp`, {
        method: "DELETE", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "Accept": "application/json" },
        body: JSON.stringify({ id_token: token }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) throw new Error(data.reason ? (typeof data.reason === "string" ? data.reason : JSON.stringify(data.reason)) : `HTTP ${res.status}`);
      return data;
    },
    async fetchRsvpStatus(ids) {
      const token = await ensureFreshIdToken();
      if (!token) throw new Error("id_token missing");
      const res = await fetch(`/api/events/rsvp-status`, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "Accept": "application/json" },
        body: JSON.stringify({ id_token: token, ids }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) return {};
      return data.statuses || {};
    },
  };

  // 参加者一覧（作成者向け）
  const fetchParticipants = async (eventId) => {
    const token = await ensureFreshIdToken();
    if (!token) { if (forceReloginOnce(false)) return null; return null; }
    const res = await fetch(`/api/events/${eventId}/participants`, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      body: JSON.stringify({ id_token: token })
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) throw new Error((data && (data.reason || data.message)) || `HTTP ${res.status}`);
    return data;
  };

  // ==============================
  // 4) 画面描画/フォーム処理
  // ==============================
  const formatLocalDateTime = (iso, hasClock) => {
    if (!iso) return "未設定";
    const d = new Date(iso);
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const dd = String(d.getDate()).padStart(2, "0");
    if (!hasClock) return `${y}/${m}/${dd}`;
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    return `${y}/${m}/${dd} ${hh}:${mm}`;
  };

  const buildLocalRange = (startIso, hasClock, endIso) => {
    if (!startIso) return "未設定";
    const s = new Date(startIso);
    const sy = s.getFullYear();
    const sm = String(s.getMonth() + 1).padStart(2, "0");
    const sd = String(s.getDate()).padStart(2, "0");
    if (!hasClock) return `${sy}/${sm}/${sd}`;
    const sh = String(s.getHours()).padStart(2, "0");
    const smin = String(s.getMinutes()).padStart(2, "0");
    const startStr = `${sy}/${sm}/${sd} ${sh}:${smin}`;
    if (!endIso) return startStr;
    const e = new Date(endIso);
    const ey = e.getFullYear();
    const em = String(e.getMonth() + 1).padStart(2, "0");
    const ed = String(e.getDate()).padStart(2, "0");
    const eh = String(e.getHours()).padStart(2, "0");
    const emin = String(e.getMinutes()).padStart(2, "0");
    const sameDay = (sy === ey && sm === em && sd === ed);
    return sameDay ? `${startStr} ~ ${eh}:${emin}` : `${startStr} ~ ${ey}/${em}/${ed} ${eh}:${emin}`;
  };


  async function fetchGroupSuggest(keyword = "") {
    const token = await ensureFreshIdToken();

    if (!token) { forceReloginOnce(false); throw new Error("id_token missing"); }

    const res = await fetch("/api/groups/suggest", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify({ id_token: token, q: keyword, limit: 20, only_my: false }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) return [];
    return data.items || [];
  }

  function renderSuggest(items) {
    const box = $("#group-suggest");
    if (!box) return;
    box.style.display = "block";
    if (!items.length) {
      box.innerHTML = `
        <div class="card muted">
          イベントが共有できるグループが見つからないよ。<br>
          まずはイベントボットを共有したいLINEグループに招待してね。
        </div>`;
      return;
    }
    box.innerHTML = items.map(it => {
      const name = it.name?.trim() ? it.name : it.id;
      const pic  = it.pictureUrl?.trim() ? it.pictureUrl : "";
      return `
        <div class="card" data-gid="${it.id}" style="display:flex;align-items:center;gap:8px;cursor:pointer;">
          ${pic ? `<img src="${pic}" alt="" style="width:28px;height:28px;border-radius:50%;">` : ``}
          <div style="font-size:14px;">${escapeHtml(name)}</div>
        </div>`;
    }).join("");
  }

  // ===== 共有UIコンポーネント（作成・編集モーダル用） =====
  class GroupShareSection {
    constructor(opts = {}) {
      this.previewEl = $("#group-preview");   // 選択済みプレビュー（アイコン＋名前）
      this.suggestEl = $("#group-suggest");   // 候補リスト（カード群）
      this.inputEl   = $("#f-group");         // hidden相当：選択されたgroupIdを保持
      this.nrowEl    = $("#row-notify");      // 通知行（ラベル＋チェックボックス）
      this.notifyEl  = $("#f-notify");        // 「イベント作成通知をグループに送る」
      this._onSuggestClick = this._onSuggestClick.bind(this);
    }

    // 通知の可否をUIに反映
    _toggleNotify(enabled) {
      if (this.nrowEl) this.nrowEl.style.display = enabled ? "" : "none";
      if (this.notifyEl) {
        this.notifyEl.disabled = !enabled;
        if (!enabled) this.notifyEl.checked = false;
      }
    }

    // 候補のクリック委譲ハンドラ
    async _onSuggestClick(ev) {
      const card = ev.target.closest('.card[data-gid]');
      if (!card) return;
      const gid = card.getAttribute('data-gid') || "";
      if (this.inputEl) this.inputEl.value = gid;

      // 選択を検証→プレビュー描画（既存関数をそのまま使用）
      try { await validateGroupSelection({ silent: false }); } catch {}
      if (this.previewEl) this.previewEl.style.display = "block";
      if (this.suggestEl) this.suggestEl.style.display = "none";
      this._toggleNotify(true);
    }

    _mountSuggestHandlers() {
      if (!this.suggestEl) return;
      this._unmountSuggestHandlers();
      this.suggestEl.addEventListener('click', this._onSuggestClick);
    }
    _unmountSuggestHandlers() {
      if (!this.suggestEl) return;
      this.suggestEl.removeEventListener('click', this._onSuggestClick);
    }

    // 未共有用：候補を表示（作成時・未共有編集時の初期表示）
    async showSuggest(keyword = "") {
      if (this.previewEl) this.previewEl.style.display = "none";
      if (this.suggestEl) {
        this.suggestEl.style.display = "block";
        this.suggestEl.innerHTML = `<div class="card muted">共有できるグループの候補を読み込み中だよ…</div>`;
      }
      this._toggleNotify(false);
      try {
        const items = await fetchGroupSuggest(keyword);
        renderSuggest(items);
        this._mountSuggestHandlers();
      } catch (e) {
        if (this.suggestEl) this.suggestEl.innerHTML = `<div class="card muted">候補の読み込みに失敗したよ</div>`;
      }
    }

    // 共有済み用：プレビュー表示（編集時、scope_idを保持しているケース）
    async showPreviewFor(scopeId) {
      if (this.inputEl) this.inputEl.value = scopeId || "";
      try { await validateGroupSelection({ silent: true }); } catch {}
      if (this.previewEl) this.previewEl.style.display = "block";
      if (this.suggestEl) { this.suggestEl.style.display = "none"; this.suggestEl.innerHTML = ""; }
      this._toggleNotify(true);
    }

    // 作成モード初期化
    async resetForCreate() {
      if (this.inputEl) this.inputEl.value = "";
      lastValidatedGroupId = "";
      await this.showSuggest("");
    }


    // 編集モード初期化（scopeIdがあればプレビュー、なければ候補）
    async resetForEdit(scopeId) {
      if (scopeId) {
        await this.showPreviewFor(scopeId);
      } else {
        await this.resetForCreate();
      }
    }

    // 保存用値の取り出し
    getValue() {
      const scope_id = (this.inputEl?.value || "").trim();
      const notify   = !!(this.notifyEl && this.notifyEl.checked);
      return { scope_id, notify };
    }
  }

  // シングルトン的に使う
  const groupShare = new GroupShareSection();

  // ===== 共有UIコンポーネントここまで =====


  let isEditing = false;
  let editingId = null;


  const openEditDialog = async (id) => {
    const item = (gItems || []).find(x => Number(x.id) === Number(id));
    if (!item) return;

    // 既存値をフォームに復元
    $("#f-title").value = item.name || "";
    $("#f-date").value  = item.start_time ? isoToLocalYmd(item.start_time) : "";
    $("#f-start").value = (item.start_time && item.start_time_has_clock) ? isoToLocalHhmm(item.start_time) : "";

    document.querySelector('input[name="endmode"][value="time"]').checked = true;
    $("#f-end").value = item.end_time ? isoToLocalHhmm(item.end_time) : "";
    $("#f-duration").value = "";
    $("#f-cap").value = (item.capacity == null ? "" : String(item.capacity));

    // 共通コンポーネントで初期化
    await groupShare.resetForEdit((item.scope_id || "").trim());

    isEditing = true;
    editingId = id;
    showDialog("edit");
  };

  const confirmDelete = async (id, name) => {
    if (!window.confirm(`「${name || '（無題）'}」を削除する？`)) return;
    const token = await ensureFreshIdToken();
    if (!token) { if (forceReloginOnce(false)) return; return; }
    try {
      await api.deleteEvent(id, token);
      alert("削除したよ");
      await loadAndRender();
    } catch (err) {
      const msg = String(err?.message || err || "");
      if (/IdToken expired/i.test(msg) || /invalid[_ ]?token/i.test(msg)) {
        if (forceReloginOnce(false)) return;
      }
      alert(`削除に失敗したよ: ${msg}`);
    }
  };

  const validateGroupSelection = async ({ silent = true } = {}) => {
    const typed = ($("#f-group")?.value || "").trim();
    const urlGrp = new URLSearchParams(location.search).get("groupId") || "";
    const token = await ensureFreshIdToken();

    // トークン欠落/期限切れは「ログイン更新」扱いで早期リターン
    if (!token) {
      const rn = $("#row-notify"), fn = $("#f-notify"), gp = $("#group-preview");
      if (rn) rn.style.display = "none";
      if (fn) { fn.checked = false; fn.disabled = true; }
      if (gp) gp.style.display = "none";
      if (!silent) alert("ログインの有効期限が切れたみたい。もう一度ログインしてね");
      forceReloginOnce(true);
      return { ok: false };
    }

    const pat = /^[CR][0-9a-f]{32}$/i;

    // 形式NG
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
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify({ id_token: token, group_id: candidate }),
    });
    const data = await res.json().catch(() => ({}));

    if (res.status === 401) {
      const rn = $("#row-notify"), fn = $("#f-notify"), gp = $("#group-preview");
      if (rn) rn.style.display = "none";
      if (fn) { fn.checked = false; fn.disabled = true; }
      if (gp) gp.style.display = "none";
      if (!silent) alert("ログインの有効期限が切れたみたい。もう一度ログインしてね");
      return { ok: false };
    }

    if (!res.ok || !data.ok) {
      const rn = $("#row-notify"), fn = $("#f-notify"), gp = $("#group-preview");
      if (rn) rn.style.display = "none";
      if (fn) { fn.checked = false; fn.disabled = true; }
      if (gp) gp.style.display = "none";

      // サーバ由来の“パラメータ不足/トークン系”はログイン更新を促す
      const reason = String(data?.reason || "");
      if (res.status === 401 || /missing_params/i.test(reason) || /id[_ ]?token/i.test(reason)) {
        if (!silent) alert("ログインの有効期限が切れたみたい。もう一度ログインしてね");
        forceReloginOnce(true);
      } else {
        if (!silent) alert("不正なIDだよ");
      }
      return { ok: false };
    }

    // 成功: プレビュー/通知UIを解放
    lastValidatedGroupId = (data.groupId || candidate);

    const gp = $("#group-preview"), gi = $("#group-icon"), gn = $("#group-name");
    if (gp && gi && gn) {
      const name = data.group?.name || candidate;
      const pic = data.group?.pictureUrl || "";
      if (pic) { gi.src = pic; gi.style.display = "inline-block"; }
      else { gi.style.display = "none"; }
      gn.textContent = name;
      gp.style.display = "flex";
    }
    const rn = $("#row-notify"), fn = $("#f-notify");
    if (rn) rn.style.display = "flex";
    if (fn) fn.disabled = false;

    return { ok: true, groupId: data.groupId || candidate };
  };

  const handleSave = async () => {
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
    if (!token) { if (forceReloginOnce(true)) return; return; }

    const urlHasGroup = /[?&]groupId=/.test(location.search);
    const { scope_id: inputGroup, notify: notifyChecked } = groupShare.getValue();

    let chosenScopeId = scopeId || ""; // 既定は現在のスコープ
    let notify = false;

    // 通知ONで共有先未選択は不可
    if (notifyChecked && !urlHasGroup && !inputGroup) {
      alert("共有するグループを選んでね");
      return;
    }

    // 共有先の最終決定
    if (urlHasGroup) {
      // グループから起動している場合は必ず検証
      const v = await validateGroupSelection({ silent: false });
      if (!v?.ok) return;
      chosenScopeId = v.groupId;
      notify = notifyChecked;
    } else if (inputGroup) {
      // 直前に同じIDで検証済み & プレビューが出ているなら再検証を省略
      const previewVisible = ($("#group-preview") && $("#group-preview").style.display !== "none");
      if (previewVisible && lastValidatedGroupId && lastValidatedGroupId === inputGroup) {
        chosenScopeId = inputGroup;
        notify = notifyChecked;
      } else {
        const v = await validateGroupSelection({ silent: false });
        if (!v?.ok) return;
        chosenScopeId = v.groupId;
        notify = notifyChecked;
      }
    } else if (notifyChecked) {
      alert("共有するグループを選んでね");
      return;
    }


    // 編集時: 共有UI未操作なら元のscope_idを維持
    if (isEditing && !(urlHasGroup || inputGroup || notifyChecked)) {
      const original = (gItems || []).find(x => Number(x.id) === Number(editingId));
      if (original?.scope_id) chosenScopeId = original.scope_id;
    }

    const payload = {
      id_token: token,
      name, date, start_time, endmode, end_time, duration,
      capacity: capacity ? Number(capacity) : null,
      scope_id: chosenScopeId,
      notify,
    };

    try {
      $("#btn-save").disabled = true;
      if (isEditing && editingId != null) await api.updateEvent(editingId, payload);
      else await api.createEvent(payload);

      hideDialog();
      clearForm();
      alert("保存したよ");
      await loadAndRender();
      sessionStorage.removeItem(REL_LOGIN_FLAG);
    } catch (err) {
      const msg = String(err?.message || err || "");
      if (/IdToken expired/i.test(msg) || /invalid[_ ]?token/i.test(msg)) {
        if (forceReloginOnce(true)) return;
      }
      console.error(err);
      alert(`保存に失敗したよ: ${msg}`);
    } finally {
      $("#btn-save").disabled = false;
    }
  };

  const loadAndRender = async () => {
    const listEl = $("#event-list");
    listEl.innerHTML = `<p class="muted">読み込み中…</p>`;
    try {
      // 1:1（U〜）は「自分が作成したイベント」
      const data = (scopeId && /^U/.test(scopeId))
        ? await api.fetchMyEvents()
        : await api.fetchEvents();

      const items = data?.items || [];
      if (!items.length) { listEl.innerHTML = `<p class="muted">イベントはまだないよ</p>`; return; }
      gItems = items;

      // 自分の参加状態をまとめて取得
      let statuses = {};
      try {
        const ids = items.map(x => x.id);
        statuses = await api.fetchRsvpStatus(ids);
      } catch { statuses = {}; }

      listEl.innerHTML = items.map((e) => {
        const name = e.name || "（無題）";
        const range = buildLocalRange(e.start_time, !!e.start_time_has_clock, e.end_time);
        const cap = (e.capacity == null) ? "定員なし" : `定員: ${e.capacity}`;
        const isCreator = !!e.created_by && !!currentUserId && (e.created_by === currentUserId);

        const st = statuses[String(e.id)] || { joined: false, is_waiting: false };
        const joined = !!st.joined;
        const waiting = !!st.is_waiting;

        const rsvpButtons = joined
          ? `<button class="btn-outline" data-act="rsvp-cancel" data-id="${e.id}">キャンセル</button>`
          : `<button class="btn-primary" data-act="rsvp-join" data-id="${e.id}">参加</button>`;

        const actionsHtml = isCreator
          ? `<div class="actions">
              <button class="btn-secondary" data-act="members" data-id="${e.id}">参加者</button>
              <button class="btn-outline" data-act="edit" data-id="${e.id}">編集</button>
              <button class="btn-outline dangerous" data-act="delete" data-id="${e.id}" data-name="${escapeHtml(name)}">削除</button>
            </div>
            <div class="att-box" id="att-${e.id}" hidden>
              <p class="muted">読み込み中だよ...</p>
            </div>`
          : `<div class="actions">${rsvpButtons}</div>`;

        const waitingNote = (joined && waiting) ? `<p class="muted">※ウェイトリスト登録中</p>` : ``;

        return `
          <article class="card" data-id="${e.id}">
            <h3>${escapeHtml(name)}</h3>
            <p>${escapeHtml(range)}</p>
            <p>${escapeHtml(cap)}</p>
            ${waitingNote}
            ${actionsHtml}
          </article>`;
      }).join("");
    } catch (err) {
      console.error(err);
      listEl.innerHTML = `<p class="muted">読み込みに失敗したよ</p>`;
    }
  };

  // ==============================
  // 5) DOMイベント登録 / 起動
  // ==============================
  document.addEventListener("DOMContentLoaded", async () => {
    // scope確定（URL→ヒント復元）
    scopeId = getScopeIdFromUrl();
    if (!scopeId) {
      const hint = sessionStorage.getItem("scopeHint") || "";
      const [k, v] = hint.split("=");
      if (v) {
        scopeId = v;
        const u = new URL(location.href);
        u.searchParams.set(k, v);
        history.replaceState(null, "", u.toString());
      }
    }

    // ========== 作成：ボタンをクリックでモーダルを開く ==========
    $("#btn-create")?.addEventListener("click", async () => {
      clearForm();               // 入力クリア（タイトル/日付/時間/定員・エラー非表示）
      showDialog("create");      // 先に開く（体感を速く）
      await groupShare.resetForCreate(); // 共有UI：候補を表示、通知はOFF
    });

    // ========== 編集：イベント委譲でボタンを拾う（動的カードでも効く） ==========
    document.addEventListener("click", async (ev) => {
      const btn = ev.target.closest(".js-edit"); // 例：<button class="js-edit" data-id="123">
      if (!btn) return;

      const id = Number(btn.dataset.id || btn.getAttribute("data-id"));
      if (!id) return;

      const item = (gItems || []).find(x => Number(x.id) === id);
      if (!item) { alert("不正なIDだよ"); return; }

      // 既存値をフォームへ反映
      $("#f-title").value = item.name || "";
      $("#f-date").value  = item.start_time ? isoToLocalYmd(item.start_time) : "";
      $("#f-start").value = (item.start_time && item.start_time_has_clock) ? isoToLocalHhmm(item.start_time) : "";
      document.querySelector('input[name="endmode"][value="time"]').checked = true;
      $("#f-end").value = item.end_time ? isoToLocalHhmm(item.end_time) : "";
      $("#f-duration").value = "";
      $("#f-cap").value = (item.capacity == null ? "" : String(item.capacity));

      // 共有UI：scope_id があればプレビュー表示、なければ候補表示
      showDialog("edit"); // 先に開く
      await groupShare.resetForEdit((item.scope_id || "").trim());

      // 編集フラグの管理（あなたの既存ロジックに合わせて）
      isEditing = true;
      editingId = id;
    });

    $("#btn-cancel").addEventListener("click", hideDialog);
    $("#create-backdrop").addEventListener("click", hideDialog);
    $("#btn-save").addEventListener("click", handleSave);

    // 共有グループの入力検証
    const grp = $('#f-group');
    if (grp) {
      const run = () => validateGroupSelection().catch(() => {});
      grp.addEventListener('blur', run);
      grp.addEventListener('change', run);
      let t=null;
      grp.addEventListener('input', () => { clearTimeout(t); t=setTimeout(run, 400); });
    }

    // 「候補を表示」
    $("#btn-suggest")?.addEventListener("click", async () => {
      const kw = ($("#f-group")?.value || "").trim();
      await groupShare.showSuggest(kw);
    });


    // URLにgroupIdがあれば自動validate（グループから起動）
    if (/[?&]groupId=/.test(location.search)) {
      try { await validateGroupSelection(); } catch {}
    }

    // LIFF起動→一覧
    try {
      const liffId = (window.LIFF_ID || "").trim();
      if (!liffId) {
        alert("初期化に失敗したよ（LIFF IDが未設定だよ）");
        $("#event-list").innerHTML = `<p class="muted">初期化に失敗したよ</p>`;
        return;
      }
      const ok = await initLiffAndLogin(liffId);
      if (ok) {
        // scope未指定ならsubで補完（1:1）
        if (!scopeId && currentUserId) {
          scopeId = currentUserId;
          const u = new URL(location.href);
          u.searchParams.set("userId", currentUserId);
          history.replaceState(null, "", u.toString());
        }
        await loadAndRender();
        restoreDraftFromSession(); // 復帰時
      }
    } catch (e) {
      console.error(e);
      $("#event-list").innerHTML = `<p class="muted">初期化に失敗したよ</p>`;
    }

    // 一覧のボタン操作
    $("#event-list").addEventListener("click", async (ev) => {
      const btn = ev.target.closest("button[data-act]");
      if (!btn) return;
      const act = btn.dataset.act;
      const id  = Number(btn.dataset.id);

      try {
        if (act === "edit") { await openEditDialog(id); return; }
        if (act === "delete") { confirmDelete(id, btn.dataset.name || ""); return; }
        if (act === "rsvp-join") {
          const res = await api.joinEvent(id);
          if (res.status === "waiting") alert("ウェイトリストに登録したよ");
          else if (res.status === "already") alert("もう参加登録しているよ");
          else alert("参加登録したよ");
          await loadAndRender();
          return;
        }
        if (act === "rsvp-cancel") {
          await api.cancelRsvp(id);
          alert("キャンセルしたよ");
          await loadAndRender();
          return;
        }
        if (act === "members") {
          const box = document.getElementById(`att-${id}`);
          if (!box) return;
          if (!box.hidden) { box.hidden = true; return; } // トグル
          box.hidden = false;
          box.innerHTML = `<p class="muted">読み込み中だよ...</p>`;
          try {
            const data = await fetchParticipants(id);
            if (!data) return;
            const capLabel = (data.counts && data.counts.capacity != null) ? `${data.counts.capacity}名` : "定員なし";
            const listHtml = (arr) => (arr?.length
              ? `<ul class="att-grid">${
                  arr.map(a => {
                    const pic  = String(a.pictureUrl || "").replace(/"/g, "&quot;");
                    const name = (a.name?.trim()) ? a.name : a.user_id;
                    return `
                      <li class="att-cell">
                        ${pic ? `<img class="att-avatar" src="${pic}" alt="">`
                              : `<span class="att-avatar placeholder" aria-hidden="true"></span>`}
                        <div class="att-name">${escapeHtml(name)}</div>
                      </li>`;
                  }).join("")}
                </ul>`
              : `<p class="muted">まだいません</p>`);
            box.innerHTML = `
              <div class="att-sec">
                <h4>参加者 (${data.participants.length}/${capLabel})</h4>
                ${listHtml(data.participants)}
              </div>
              <div class="att-sec" style="margin-top:8px;">
                <h4>ウェイトリスト (${data.waitlist.length})</h4>
                ${listHtml(data.waitlist)}
              </div>`;
          } catch (err) {
            const msg = String(err?.message || err || "");
            box.innerHTML = `<p class="error">読み込みに失敗したよ: ${msg}</p>`;
          }
          return;
        }
      } catch (err) {
        const msg = String(err?.message || err || "");
        if (/IdToken expired/i.test(msg) || /invalid[_ ]?token/i.test(msg) || /id[_ ]?token\s*missing/i.test(msg) || /no\s+id[_ ]?token/i.test(msg)) {
          if (forceReloginOnce(false)) return;
        }
        alert(`操作に失敗したよ: ${msg}`);
      }
    });
  });
})();
