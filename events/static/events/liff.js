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
    const title = (mode === "edit") ? "イベントを編集" : "イベントを作成";
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


  // 編集モーダル
  async function openEditDialog(id) {
    const item = (gItems || []).find(x => Number(x.id) === Number(id));
    if (!item) return;

    // 既存値をフォームに流し込む
    $("#f-title").value = item.name || "";
    $("#f-date").value  = item.start_time ? isoToLocalYmd(item.start_time) : "";
    $("#f-start").value = (item.start_time && item.start_time_has_clock) ? isoToLocalHhmm(item.start_time) : "";

    // 終了は「時刻入力」タブに寄せておく
    document.querySelector('input[name="endmode"][value="time"]').checked = true;
    $("#f-end").value = item.end_time ? isoToLocalHhmm(item.end_time) : "";
    $("#f-duration").value = "";

    $("#f-cap").value = (item.capacity == null ? "" : String(item.capacity));

    // 共有済みグループのプレビュー復元
    const gid = (item.scope_id || "").trim();
    const input = $("#f-group");
    if (input) input.value = gid;

    if (gid && /^[CR][0-9a-f]{32}$/i.test(gid)) {
      // サーバ側で validate → 名前・アイコンをプレビュー表示
      try { await validateGroupSelection({ silent: true }); } catch {}
    } else {
      // 共有なしならプレビュー/通知UIを隠す
      const gp = $("#group-preview"), rn = $("#row-notify"), fn = $("#f-notify");
      if (gp) gp.style.display = "none";
      if (rn) rn.style.display = "none";
      if (fn) { fn.checked = false; fn.disabled = true; }
    }

    isEditing = true;
    editingId = id;
    showDialog("edit");
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

    // いまのURLに groupId / userId があればヒントとして保存
    const hasScope = /[?&](groupId|userId)=/.test(location.search);
    if (hasScope) {
      const qs = new URLSearchParams(location.search);
      const hint = qs.get("groupId")
        ? `groupId=${qs.get("groupId")}`
        : (qs.get("userId") ? `userId=${qs.get("userId")}` : "");
      if (hint) sessionStorage.setItem("scopeHint", hint);
    }

    // redirectUri は「scope があるなら今のURL」を優先
    let redirectUri = location.href.replace(/^http:/, 'https:');
    if (!hasScope) {
      redirectUri = (window.LIFF_REDIRECT_ABS || redirectUri).replace(/^http:/, 'https:');
    }

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
      const hasScope = /[?&](groupId|userId)=/.test(location.search);
      if (hasScope) {
        const qs = new URLSearchParams(location.search);
        const hint = qs.get("groupId")
          ? `groupId=${qs.get("groupId")}`
          : (qs.get("userId") ? `userId=${qs.get("userId")}` : "");
        if (hint) sessionStorage.setItem("scopeHint", hint);
      }
      let redirectUri = location.href.replace(/^http:/, 'https:');
      if (!hasScope) {
        redirectUri = (window.LIFF_REDIRECT_ABS || redirectUri).replace(/^http:/, 'https:');
      }
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
    // 一覧取得：scopeId が空ならクエリを付けず「全件」取得に戻す
    async fetchEvents() {
      const url = (scopeId && String(scopeId).trim())
        ? `/api/events?scope_id=${encodeURIComponent(scopeId)}`
        : `/api/events`;
      const res = await fetch(url, {
        credentials: "same-origin",
        headers: { "Accept": "application/json" },
      });
      if (!res.ok) throw new Error(`fetch events failed: ${res.status}`);
      return await res.json();
    },


    // 自分が作成したイベント一覧（1:1用）
    async fetchMyEvents() {
      const token = await ensureFreshIdToken();
      if (!token) {
        // 再ログインを発火（空配列で誤表示にしない）
        forceReloginOnce(false);
        throw new Error("id_token missing");
      }
      const res = await fetch(`/api/events/mine`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "Accept": "application/json" },
        body: JSON.stringify({ id_token: token }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        const r = data && data.reason || `HTTP ${res.status}`;
        throw new Error(r);
      }
      return { items: data.items || [] };
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

    async joinEvent(id) {
      const token = await ensureFreshIdToken();
      if (!token) {
        // トークンが無ければこの場で再ログインを促す（クリック側のcatchでも拾えるが二重保険）
        forceReloginOnce(false);
        throw new Error("id_token missing");
      }
      const res = await fetch(`/api/events/${id}/rsvp`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "Accept": "application/json" },
        body: JSON.stringify({ id_token: token }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        const r = data.reason ? (typeof data.reason === "string" ? data.reason : JSON.stringify(data.reason)) : `HTTP ${res.status}`;
        throw new Error(r);
      }
      return data; // {status: 'joined'|'waiting'|'already', ...}
    },

    async cancelRsvp(id) {
      const token = await ensureFreshIdToken();
      if (!token) throw new Error("id_token missing");
      const res = await fetch(`/api/events/${id}/rsvp`, {
        method: "DELETE",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "Accept": "application/json" },
        body: JSON.stringify({ id_token: token }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        const r = data.reason ? (typeof data.reason === "string" ? data.reason : JSON.stringify(data.reason)) : `HTTP ${res.status}`;
        throw new Error(r);
      }
      return data; // {status: 'canceled', ...}
    },

    async fetchRsvpStatus(ids) {
      const token = await ensureFreshIdToken();
      if (!token) throw new Error("id_token missing");
      const res = await fetch(`/api/events/rsvp-status`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "Accept": "application/json" },
        body: JSON.stringify({ id_token: token, ids }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) return {};
      return data.statuses || {};
    },    

  };

  // 参加者一覧の取得
  async function fetchParticipants(eventId) {
    const token = await ensureFreshIdToken();
    if (!token) { if (forceReloginOnce(false)) return null; return null; }

    const res = await fetch(`/api/events/${eventId}/participants`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      body: JSON.stringify({ id_token: token })
    });

    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) {
      const msg = (data && (data.reason || data.message)) || `HTTP ${res.status}`;
      throw new Error(msg);
    }
    return data;
  }


  // ---- 一覧描画 ----
  async function loadAndRender() {
    const listEl = $("#event-list");
    listEl.innerHTML = `<p class="muted">読み込み中…</p>`;
    try {
      // Uで始まるscopeId（=1:1コンテキスト）のときは「自分が作成したイベント」を取得
      const data = (scopeId && /^U/.test(scopeId))
        ? await api.fetchMyEvents()
        : await api.fetchEvents();

      const items = (data && data.items) || [];

      if (!items.length) {
        listEl.innerHTML = `<p class="muted">イベントはまだないよ</p>`;
        return;
      }

      gItems = items;  // ← 一覧をキャッシュ

      // 追加: 自分の参加ステータスをまとめて取得（失敗時は空）
      let statuses = {};
      try {
        const ids = items.map(x => x.id);
        statuses = await api.fetchRsvpStatus(ids);
      } catch { statuses = {}; }

      listEl.innerHTML = items.map((e) => {
        const name = e.name || "（無題）";
        const range = buildLocalRange(e.start_time, !!e.start_time_has_clock, e.end_time);
        const cap = (e.capacity === null || e.capacity === undefined)
          ? "定員なし"
          : `定員: ${e.capacity}`;
        const isCreator = !!e.created_by && !!currentUserId && (e.created_by === currentUserId);

        // 追加: 参加状態の判定
        const st = statuses[String(e.id)] || { joined: false, is_waiting: false };
        const joined = !!st.joined;
        const waiting = !!st.is_waiting;

        // 参加/キャンセルボタン
        const rsvpButtons = joined
          ? `<button class="btn-outline" data-act="rsvp-cancel" data-id="${e.id}">キャンセル</button>`
          : `<button class="btn-primary" data-act="rsvp-join" data-id="${e.id}">参加</button>`;

        // イベント作成者には「参加者」ボタン＋展開ボックスを追加
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

        // 待機中なら行内に小注記（任意）
        const waitingNote = (joined && waiting) ? `<p class="muted">※ウェイトリスト登録中</p>` : ``;

        return `
          <article class="card" data-id="${e.id}">
            <h3>${escapeHtml(name)}</h3>
            <p>${escapeHtml(range)}</p>
            <p>${escapeHtml(cap)}</p>
            ${waitingNote}
            ${actionsHtml}
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

    // 共有先の決定とバリデーション
    const urlHasGroup   = /[?&]groupId=/.test(location.search);
    const inputGroup    = ($("#f-group")?.value || "").trim();
    const notifyChecked = !!$("#f-notify")?.checked;

    let chosenScopeId = scopeId || ""; // 既定は現在のコンテキスト（1:1なら userId / グループなら groupId）
    let notify = false;

    // 1) 「通知ONなのに共有グループ未選択」は保存不可
    if (notifyChecked && !urlHasGroup && !inputGroup) {
      alert("共有するグループを選んでね");
      return;
    }

    // 2) URLにgroupIdがある／入力がある／通知ON → validateを必ず実施
    if (urlHasGroup || inputGroup || notifyChecked) {
      const v = await validateGroupSelection({ silent: false });
      if (!v || !v.ok) return;  // validate失敗時は保存中止（アラートはvalidate側で出す）
      chosenScopeId = v.groupId;
      notify = notifyChecked;
    }

    // 3) （編集時）共有先UIを触っていない場合は、元の共有先(scope_id)を維持する
    if (isEditing && !(urlHasGroup || inputGroup || notifyChecked)) {
      const original = (gItems || []).find(x => Number(x.id) === Number(editingId));
      if (original && original.scope_id) {
        chosenScopeId = original.scope_id;
      }
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
      notify, // グループへの通知フラグ
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

    // ログイン復帰でURLから消えた場合のフォールバック
    if (!scopeId) {
      const hint = sessionStorage.getItem("scopeHint") || "";
      const [k, v] = hint.split("=");
      if (v) {
        scopeId = v;
        // URLにも書き戻して以後安定させる
        const u = new URL(location.href);
        u.searchParams.set(k, v);
        history.replaceState(null, "", u.toString());
        // 必要なら一度きりのヒントなので消しておく
        // sessionStorage.removeItem("scopeHint");
      }
    }

    $("#btn-create").addEventListener("click", async () => {
      showDialog();    // モーダルを開く

      const items = await fetchGroupSuggest("");  // グループ候補を取得
      renderSuggest(items);
    });

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


    async function fetchGroupSuggest(keyword = "") {
      const token = await ensureFreshIdToken(); // 既存
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
      const box = document.querySelector("#group-suggest");
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

      box.style.display = "block";

      box.innerHTML = items.map(it => {
        const name = it.name && it.name.trim() ? it.name : it.id;
        const pic  = it.pictureUrl && it.pictureUrl.trim() ? it.pictureUrl : "";
        return `
          <div class="card" data-gid="${it.id}" style="display:flex;align-items:center;gap:8px;cursor:pointer;">
            ${pic ? `<img src="${pic}" alt="" style="width:28px;height:28px;border-radius:50%;">` : ``}
            <div style="font-size:14px;">${escapeHtml(name)}</div>
          </div>
        `;
      }).join("");


    }

    // 候補クリックで入力→validate
    document.querySelector("#group-suggest")?.addEventListener("click", async (ev) => {
      const card = ev.target.closest(".card[data-gid]");
      if (!card) return;

      const gid   = card.dataset.gid || "";
      const gname = card.dataset.gname || "";
      const gpic  = card.dataset.pic || "";

      const input = document.querySelector("#f-group");
      if (input) input.value = gid;

      const sg = document.querySelector("#selected-group");
      if (sg) {
        sg.innerHTML = `
          ${gpic ? `<img src="${gpic}" alt="">` : ``}
          <span class="name">${escapeHtml(gname)}</span>
          <span class="clear" id="sg-clear">変更</span>
        `;
        sg.style.display = "flex";
      }

      // サジェストは閉じてもよい
      const box = document.querySelector("#group-suggest");
      if (box) { box.style.display = "none"; box.innerHTML = ""; }

      await validateGroupSelection({ silent: false });
    });

    // 「変更」を押したら再び候補表示（任意）
    document.addEventListener("click", (e) => {
      if (e.target && e.target.id === "sg-clear") {
        const box = document.querySelector("#group-suggest");
        if (box) { box.style.display = "block"; }
        // 既存の「候補を表示」ロジックを再利用
        document.querySelector("#btn-suggest")?.click();
      }
    });


    // 「候補を表示」ボタン
    document.querySelector("#btn-suggest")?.addEventListener("click", async () => {
      const kw = (document.querySelector("#f-group")?.value || "").trim();
      const items = await fetchGroupSuggest(kw);
      renderSuggest(items);
    });


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
        // URLにscopeが無ければ、IDトークンのsubで補完（=1:1スコープ）
        if (!scopeId && currentUserId) {
          scopeId = currentUserId;
          const u = new URL(location.href);
          u.searchParams.set("userId", currentUserId);
          history.replaceState(null, "", u.toString());
        }

        await loadAndRender();
        // 復帰時はドラフトだけ復元
        restoreDraftFromSession();
      }

    } catch (e) {
      console.error(e);
      $("#event-list").innerHTML = `<p class="muted">初期化に失敗したよ</p>`;
    }


    $("#event-list").addEventListener("click", async (ev) => {
      const btn = ev.target.closest("button[data-act]");
      if (!btn) return;
      const act = btn.dataset.act;
      const id  = Number(btn.dataset.id);

      try {
        if (act === "edit") {
          await openEditDialog(id);
          return;
        }
        if (act === "delete") {
          confirmDelete(id, btn.dataset.name || "");
          return;
        }
        if (act === "rsvp-join") {
          const res = await api.joinEvent(id);
          if (res.status === "waiting") alert("ウェイトリストに登録したよ");
          else if (res.status === "already") alert("もう参加登録しているよ");
          else alert("参加登録したよ");
          await loadAndRender(); // 最新状態に更新
          return;
        }
        if (act === "rsvp-cancel") {
          const res = await api.cancelRsvp(id);
          alert("キャンセルしたよ");
          await loadAndRender();
          return;
        }

        // 参加者表示トグル
        if (act === "members") {
          const box = document.getElementById(`att-${id}`);
          if (!box) return;

          // トグル表示（開いていれば閉じる）
          if (!box.hidden) { box.hidden = true; return; }

          box.hidden = false;
          box.innerHTML = `<p class="muted">読み込み中だよ...</p>`;
          try {
            const data = await fetchParticipants(id);
            if (!data) return;

            const capLabel = (data.counts && data.counts.capacity != null)
              ? `${data.counts.capacity}名`
              : "定員なし";

            // アイコンの下に名前を表示
            const listHtml = (arr) => (
              arr && arr.length
                ? `<ul class="att-grid">${
                    arr.map(a => {
                      const pic  = String(a.pictureUrl || "").replace(/"/g, "&quot;");
                      const name = (a.name && a.name.trim()) ? a.name : a.user_id;
                      return `
                        <li class="att-cell">
                          ${pic
                            ? `<img class="att-avatar" src="${pic}" alt="">`
                            : `<span class="att-avatar placeholder" aria-hidden="true"></span>`}
                          <div class="att-name">${escapeHtml(name)}</div>
                        </li>`;
                    }).join("")}
                  </ul>`
                : `<p class="muted">まだいません</p>`
            );



            box.innerHTML = `
              <div class="att-sec">
                <h4>参加者 (${data.participants.length}/${capLabel})</h4>
                ${listHtml(data.participants)}
              </div>
              <div class="att-sec" style="margin-top:8px;">
                <h4>ウェイトリスト (${data.waitlist.length})</h4>
                ${listHtml(data.waitlist)}
              </div>
            `;
          } catch (err) {
            const msg = String(err && err.message || err || "");
            box.innerHTML = `<p class="error">読み込みに失敗したよ: ${msg}</p>`;
          }
          return;
        }

      } catch (err) {
        const msg = String(err && err.message || err || "");

        if (
          /IdToken expired/i.test(msg) ||
          /invalid[_ ]?token/i.test(msg) ||
          /id[_ ]?token\s*missing/i.test(msg) ||
          /no\s+id[_ ]?token/i.test(msg)
        ) {
          if (forceReloginOnce(false)) return;
        }

        alert(`操作に失敗したよ: ${msg}`);
      }

    });


  });

})();
