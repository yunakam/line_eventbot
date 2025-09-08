// liff.js  — トップページでカード表示・編集／削除ハンドリング
// 想定API: GET /api/events/ -> イベント一覧
//          DELETE /api/events/{id}/ -> イベント削除
// 編集画面: /liff/edit/?id={id} へ遷移（必要に応じて調整）
// ※エンドポイント名が異なる場合は、API_BASEやURL組み立て部分を合わせて修正してね。

// ====== 設定ここから ======
const API_BASE = '/api';
const LIST_ENDPOINT = `${API_BASE}/events`;
const DETAIL_ENDPOINT = (id) => `${API_BASE}/events/${id}`;
// 編集画面のLIFF内URL（urls_liff.py側のルーティングに合わせて調整）
const EDIT_PAGE = (id) => `/liff/edit/?id=${encodeURIComponent(id)}`;
// ====== 設定ここまで ======

// 小さなログラッパー（開発時に便利）
function log(...args) {
  console.log('[LIFF]', ...args);
}

// DjangoのCSRFトークンをCookieから取得する関数
 function getCSRFToken() {
  const m = document.cookie.match(/csrftoken=([^;]+)/);
   return m ? decodeURIComponent(m[1]) : '';
 }


/** IDトークン検査と検証関数
ページ初期化時にIDトークンの有効期限を検査し、切れていたら自動で再ログイン
検証APIが「期限切れ」で返したときも再ログインにフォールバック **/

// id_tokenのexpを秒で返す。取れない場合は0
function getIdTokenExpSec() {
  try {
    const dec = liff.getDecodedIDToken(); 
    return dec?.exp ? Number(dec.exp) : 0;
  } catch (_) {
    return 0;
  }
}

function logIdTokenDebug() {
  try {
    const dec = liff.getDecodedIDToken();
    const expMs = (dec?.exp ?? 0) * 1000;
    log('id_token exp(ms)', expMs, 'now(ms)', Date.now(), 'delta(ms)', expMs - Date.now());
  } catch (e) {
    log('id_token decode failed', e);
  }
}


// 期限切れ or 間もなく切れるならtrue
function isIdTokenStale(graceMs = 30_000) { // 猶予30秒
  const expSec = getIdTokenExpSec();
  if (!expSec) return true;
  return (expSec * 1000) <= (Date.now() + graceMs);
}

// バックエンドでid_token検証。期限切れならfalseを返す
async function verifyIdTokenOnServer(idToken) {
  try {
    const res = await fetch('/api/auth/verify-idtoken', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': getCSRFToken(),
      },
      credentials: 'include',
      body: JSON.stringify({ id_token: idToken })
    });
    if (res.ok) return true;

    const body = await res.json().catch(() => ({}));
    log('verify error', body);
    // LINEの典型エラー："IdToken expired."
    if (body?.reason?.error_description?.includes('expired')) return false;
    return false;
  } catch (e) {
    log('verify exception', e);
    return false;
  }
}

// ハードリフレッシュ：LIFF のキャッシュを削除してから再ログイン
function hardRelogin() {
  try { liff.logout(); } catch (_) {}
  try {
    // LIFF SDK が使う可能性のあるキーを掃除
    const patterns = ['LIFF_STORE', 'LIFF_DATA', String(window.LIFF_ID)];
    Object.keys(localStorage).forEach(k => {
      if (patterns.some(p => k.includes(p))) localStorage.removeItem(k);
    });
    sessionStorage.clear(); // 念のため
  } catch (_) {}
  // 現在URLへ戻す（末尾スラ有無も含め完全一致させると安全）
  liff.login({ redirectUri: location.href.split('#')[0] });
}

// 必要なら再ログインして、使用可能なid_tokenを保証する
async function ensureFreshIdToken() {
  if (!liff.isLoggedIn()) {
    liff.login(); // ログインしてね
    return false; // ここで一旦終了（リダイレクト後に再実行される）
  }
  // 期限が近い/切れているなら強制再ログイン
  if (isIdTokenStale()) {
    hardRelogin();
    return false;
  }
  const token = liff.getIDToken();
  if (!token) {
    hardRelogin();
    return false;
  }

  // ★ デバッグ：検証前にexpと現在時刻の差分を出す
  logIdTokenDebug();

  // サーバ検証。期限切れ等なら再ログイン
  const ok = await verifyIdTokenOnServer(token);
  if (!ok) {
    hardRelogin();
    return false;
  }

  return true; // 新鮮でサーバ検証OK
}


// ==== IDトークン検査ここまで ====

// 読みやすい日付文字列を作るユーティリティ
function isValidDate(d) {
  return d instanceof Date && !Number.isNaN(d.getTime());
}

// "YYYY/MM/DD" を返す（無効なら空文字）
function formatDate(dtStr) {
  try {
    const d = new Date(dtStr);
    if (!isValidDate(d)) return '';
    const pad = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}/${pad(d.getMonth() + 1)}/${pad(d.getDate())}`;
  } catch (_) {
    return '';
  }
}

// 読みやすい時刻文字列（"HH:MM"）を作る
function formatTime(dtStrOrTime) {
  // "14:00" のような時刻だけが来る場合にも対応
  if (/^\d{1,2}:\d{2}$/.test(dtStrOrTime)) return dtStrOrTime;

  // ISO文字列の場合
  try {
    const d = new Date(dtStrOrTime);
    if (!isValidDate(d)) return '';
    const pad = (n) => String(n).padStart(2, '0');
    return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  } catch (e) {
    return dtStrOrTime || '';
  }
}

// 日付＋時刻を結合して "YYYY/MM/DD HH:MM" を返す（片方空ならある方だけ）
function formatDateTime(dateStr, timeStr) {
  const d = formatDate(dateStr);
  const t = formatTime(timeStr);
  if (d && t) return `${d} ${t}`;
  return d || t || '';
}


// アイコン（SVG）— 依存なしで軽量に表示する
const Icon = {
  edit: `
    <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">
      <path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04
               a1.003 1.003 0 0 0 0-1.42l-2.34-2.34a1.003 1.003 0 0 0-1.42
               0l-1.83 1.83 3.75 3.75 1.84-1.82z"></path>
    </svg>`,
  trash: `
    <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">
      <path d="M3 6h18v2H3V6zm2 3h14l-1.2 12.3c-.1 1-1 1.7-2 1.7H8.2c-1 0-1.9-.7-2-1.7L5 9zm5-6h4l1 1h4v2H5V4h4l1-1z"></path>
    </svg>`
};

// イベントカードのDOM文字列を返す
function renderCard(ev) {
  // ev: { id, title, date, start_time, start_datetime など想定 }
  // ※サーバのフィールド名に合わせて必要があればここを調整
  const id = ev.id;
  const title = ev.title || ev.name || '(無題イベント)';
  // 日付と開始時刻の候補を広めに拾う
  // --- 候補の拾い方を強化 ---
  // 1) ISO の start_datetime / start_at があれば、そこから日付と時刻を切り出す
  let iso = ev.start_datetime || ev.start_at || null;
  let date = ev.date || ev.event_date || ev.start_date || null;
  let startTime = ev.start_time || ev.start || null;
  if (iso && (!date || !startTime)) {
    const d = new Date(iso);
    if (isValidDate(d)) {
      const pad = (n) => String(n).padStart(2, '0');
    if (!date)  date  = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
      if (!startTime) startTime = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
    }
  }
  const dateText = formatDateTime(date, startTime); // ← 「2025/09/08 12:30」の形に
  const timeText = formatTime(startTime);           // 既存の「開始」行でも使う（必要なら残す）
 

  return `
  <div class="card" data-id="${id}">
    <div class="card__header">
      <div class="card__title" title="${escapeHtml(title)}">${escapeHtml(title)}</div>
      <div class="card__actions">
        <button class="icon-btn js-edit" title="編集">${Icon.edit}</button>
        <button class="icon-btn js-delete" title="削除">${Icon.trash}</button>
      </div>
    </div>
    <div class="card__meta">
      <div class="meta__row"><span class="meta__label">日付</span><span class="meta__val">${dateText || '未設定'}</span></div> 
</div>
  </div>`;
}

// 文字列をエスケープ（XSS対策）
function escapeHtml(s) {
  return String(s ?? '')
    .replaceAll('&','&amp;')
    .replaceAll('<','&lt;')
    .replaceAll('>','&gt;')
    .replaceAll('"','&quot;')
    .replaceAll("'",'&#39;');
}

// 一覧を取得して描画する
async function fetchAndRenderEvents() {
  const listEl = document.getElementById('event-list');
  const emptyEl = document.getElementById('empty-state');

  listEl.innerHTML = ''; // 一旦クリア
  emptyEl.textContent = '読み込み中…';

  const evRes = await fetch(LIST_ENDPOINT, { credentials: 'include' });
  // ★ユーザー要望により、当面はこのログを残す
  log('events status', evRes.status);

  if (!evRes.ok) {
    emptyEl.textContent = 'イベントの取得に失敗したよ';
    return;
  }

  let data = await evRes.json();
  // DRFのpagination形式 { results: [...] } と素配列の両方に対応
  const items = Array.isArray(data) ? data : (data.results || data.items || []);

  if (!items.length) {
    emptyEl.textContent = 'まだイベントがないよ。「＋作成」から作ってね';
    return;
  }

  const html = items.map(renderCard).join('');
  listEl.innerHTML = html;
  emptyEl.textContent = '';

  // ボタンのイベントを束ねて付与
  listEl.querySelectorAll('.card').forEach(card => {
    const id = card.getAttribute('data-id');
    const editBtn = card.querySelector('.js-edit');
    const delBtn = card.querySelector('.js-delete');

    editBtn?.addEventListener('click', () => {
      // 編集画面へ遷移（LIFF内遷移）
      location.href = EDIT_PAGE(id);
    });

    delBtn?.addEventListener('click', async () => {
      const ok = window.confirm('このイベントを削除するよ。いいかな？');
      if (!ok) return;

      const res = await fetch(DETAIL_ENDPOINT(id), {
        method: 'DELETE',
        headers: { 'X-CSRFToken': getCSRFToken() },
        credentials: 'include'
      });

      if (res.ok || res.status === 204) {
        // 成功時は当該カードをDOMから除去
        card.remove();
        // 空になったらメッセージ表示
        if (!listEl.querySelector('.card')) {
          emptyEl.textContent = 'イベントがなくなったよ';
        }
      } else {
        alert('削除に失敗したよ');
      }
    });
  });
}

// LIFF初期化 → イベント一覧描画
async function init() {
  try {

    // 1) LIFF初期化
    if (!window.liff) {
      log('LIFF SDK not loaded');
    } else {
      await liff.init({ liffId: window.LIFF_ID });
    }

    // 新鮮なid_tokenを保証（必要なら自動再ログイン）
    const ready = await ensureFreshIdToken();
    if (!ready) return;

    // 2) イベント一覧を読んで描画
    await fetchAndRenderEvents();
  } catch (e) {
    console.error(e);
    const emptyEl = document.getElementById('empty-state');
    if (emptyEl) emptyEl.textContent = '読み込みでエラーが起きたよ';
  }
}

// DOMロード後に開始
document.addEventListener('DOMContentLoaded', init);
