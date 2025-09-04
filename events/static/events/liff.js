// events/static/events/liff.js
// LIFF 初期化 → ログイン → IDトークン検証 → イベント取得
// 画面内ログ(#devlog)とステータス(#status)に出力する。

(async function () {
  const statusEl = document.getElementById('status');
  const eventsEl = document.getElementById('events');
  const devlogEl = document.getElementById('devlog');

  const say = (t) => { if (statusEl) statusEl.textContent = t; };
  const log = (...args) => {
    try { console.log('[LIFF]', ...args); } catch (_) {}
    if (devlogEl) {
      const line = args.map(a => {
        try { return typeof a === 'string' ? a : JSON.stringify(a); }
        catch { return String(a); }
      }).join(' ');
      devlogEl.textContent += `[LIFF] ${line}\n`;
    }
  };

  // 想定外エラーも拾う
  window.addEventListener('error', (e) => log('window.error', e.message));
  window.addEventListener('unhandledrejection', (e) => log('unhandledrejection', String(e.reason)));

  try {
    // 1) LIFF_ID チェック
    if (!window.LIFF_ID) {
      say('設定が足りないよ（LIFF_ID）');
      log('LIFF_ID is empty; check settings.py/.env');
      return;
    }

    // 2) 初期化
    say('LIFFを初期化するよ…');
    log('init start', window.LIFF_ID);
    await liff.init({ liffId: window.LIFF_ID });
    log('init done', { isLoggedIn: liff.isLoggedIn(), inClient: liff.isInClient() });

    // 3) ログインしてなければログインへ
    if (!liff.isLoggedIn()) {
      say('ログインしてね');
      log('calling liff.login()');
      liff.login();
      return;
    }

    // 4) IDトークン取得
    say('ログイン中だよ…');
    const idToken = liff.getIDToken();
    log('idToken', idToken ? '(received)' : '(empty)');
    if (!idToken) {
      say('IDトークンが取れないよ');
      return;
    }

    // 5) IDトークン検証
    say('トークン検証に行くよ…');
    const verifyRes = await fetch('/api/auth/verify-idtoken', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id_token: idToken }),
    });
    log('verify status', verifyRes.status);
    const verifyJson = await verifyRes.json().catch(() => ({}));
    if (!verifyRes.ok || !verifyJson.ok) {
      say('トークン検証に失敗したよ');
      log('verify error', verifyJson);
      return;
    }

    // 6) イベント一覧
    say('イベントを読み込むよ…');
    const evRes = await fetch('/api/events');
    log('events status', evRes.status);
    const evJson = await evRes.json().catch(() => ({}));
    if (!evRes.ok || !evJson.ok) {
      say('イベント取得に失敗したよ');
      log('events error', evJson);
      return;
    }

    say('読み込み完了だよ');
    if (!Array.isArray(evJson.items) || evJson.items.length === 0) {
      if (eventsEl) eventsEl.textContent = 'イベントはまだないよ';
      return;
    }
    const ul = document.createElement('ul');
    evJson.items.forEach(e => {
      const li = document.createElement('li');
      const date = e.date || '';
      const name = e.name || '';
      li.textContent = `${date} ${name}`.trim();
      ul.appendChild(li);
    });
    if (eventsEl) eventsEl.appendChild(ul);

  } catch (e) {
    try { console.error('[LIFF] fatal', e); } catch (_) {}
    say('エラーが発生したよ');
    log('fatal', String(e && e.message ? e.message : e));
  }
})();
