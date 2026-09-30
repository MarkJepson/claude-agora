// UI QA of the relay dashboard: drives headless Chromium over the DevTools
// protocol (Node 22's built-in WebSocket, no installs). Reads fixture.env
// written by api_qa.sh. The outage test fails the page's own polls via
// Fetch interception, so the real relay is never stopped.
import { spawn } from 'node:child_process';
import { readFileSync, writeFileSync, mkdtempSync, readdirSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const DIR = dirname(fileURLToPath(import.meta.url));
const FX = Object.fromEntries(readFileSync(join(DIR, 'fixture.env'), 'utf8').trim().split(/\s+/).map((kv) => kv.split('=')));
const BASE = process.env.RELAY_URL || 'http://127.0.0.1:8089';

function findChromium() {
  if (process.env.PLAYWRIGHT_CHROMIUM) return process.env.PLAYWRIGHT_CHROMIUM;
  const cacheDir = join(process.env.HOME || '', '.cache', 'ms-playwright');
  // Don't hardcode a Playwright build number -- it goes stale on every
  // Playwright upgrade. Pick the newest chromium_headless_shell-* found.
  const candidates = existsSync(cacheDir)
    ? readdirSync(cacheDir).filter((d) => d.startsWith('chromium_headless_shell-')).sort().reverse()
    : [];
  if (!candidates.length) {
    console.error('No Playwright chromium_headless_shell found under ' + cacheDir + '.');
    console.error('Install one (npx playwright install chromium --only-shell) or set PLAYWRIGHT_CHROMIUM to a binary path.');
    process.exit(2);
  }
  return join(cacheDir, candidates[0], 'chrome-headless-shell-linux64', 'chrome-headless-shell');
}
// The dashboard needs OPERATOR_TOKEN to post as operator (Mark enters it once;
// it lives in sessionStorage). Same source as api_qa.sh: env, else gitignored .env.
function operatorToken() {
  if (process.env.OPERATOR_TOKEN) return process.env.OPERATOR_TOKEN;
  try {
    const m = readFileSync(join(DIR, '..', '..', '.env'), 'utf8').match(/^OPERATOR_TOKEN=(.*)$/m);
    return m ? m[1].trim().replace(/^["']|["']$/g, '') : '';
  } catch (e) { return ''; }
}
const OPERATOR_TOKEN = operatorToken();
const CH = findChromium();
const PORT = 9333;
let pass = 0, fail = 0;
const ok = (n) => { pass++; console.log('PASS  ' + n); };
const bad = (n, why) => { fail++; console.log('FAIL  ' + n + '  -- ' + why); };
const check = (n, cond, why) => (cond ? ok(n) : bad(n, why));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// --password-store=basic: never touch the desktop keyring (otherwise WSLg
// pops a "Choose password for new keyring" dialog on the operator's screen).
const proc = spawn(CH, ['--no-sandbox', '--hide-scrollbars', '--password-store=basic', '--use-mock-keychain', '--window-size=1440,960', `--remote-debugging-port=${PORT}`,
  '--user-data-dir=' + mkdtempSync(join(tmpdir(), 'qa-chrome-')), 'about:blank'], { stdio: 'ignore' });

let ws, seq = 0;
const waiters = new Map();
const errors = [];
async function connect() {
  for (let i = 0; i < 50; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json`)).json();
      const page = list.find((t) => t.type === 'page');
      if (page) {
        ws = new WebSocket(page.webSocketDebuggerUrl);
        await new Promise((r, j) => { ws.onopen = r; ws.onerror = j; });
        ws.onmessage = (e) => {
          const m = JSON.parse(e.data);
          if (m.id && waiters.has(m.id)) { waiters.get(m.id)(m); waiters.delete(m.id); }
          if (m.method === 'Runtime.exceptionThrown') errors.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
          if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') errors.push(m.params.args.map((a) => a.value ?? a.description).join(' '));
          if (m.method === 'Fetch.requestPaused') send('Fetch.failRequest', { requestId: m.params.requestId, errorReason: 'ConnectionRefused' });
        };
        return;
      }
    } catch (e) { /* not up yet */ }
    await sleep(200);
  }
  throw new Error('chromium did not start');
}
function send(method, params = {}) {
  const id = ++seq;
  ws.send(JSON.stringify({ id, method, params }));
  return new Promise((r) => waiters.set(id, r));
}
async function ev(expr) {
  const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
  if (r.result?.exceptionDetails) throw new Error('eval failed: ' + expr + ' :: ' + JSON.stringify(r.result.exceptionDetails.exception?.description));
  return r.result?.result?.value;
}
async function until(expr, ms = 10000) {
  const end = Date.now() + ms;
  while (Date.now() < end) { if (await ev(expr)) return true; await sleep(200); }
  return false;
}
async function shot(name) {
  const r = await send('Page.captureScreenshot', { format: 'png' });
  writeFileSync(join(DIR, name), Buffer.from(r.result.data, 'base64'));
}
async function go(url) { await send('Page.navigate', { url }); await sleep(300); }
async function mouse(type, x, y, extra = {}) { await send('Input.dispatchMouseEvent', { type, x, y, button: 'left', buttons: type === 'mouseReleased' ? 0 : 1, ...extra }); }

try {
  await connect();
  await send('Runtime.enable');
  await send('Page.enable');
  await send('Browser.grantPermissions', { origin: BASE, permissions: ['clipboardReadWrite', 'clipboardSanitizedWrite'] }).catch(() => {});

  console.log('== load');
  await go(BASE + '/dashboard');
  if (OPERATOR_TOKEN) await ev(`sessionStorage.setItem('relay_operator_token', ${JSON.stringify(OPERATOR_TOKEN)})`);
  check('page reaches Live', await until(`document.querySelector('.conn-strong')?.textContent === 'Live'`), 'never Live');
  check('thread list rendered', await ev(`document.querySelectorAll('.rl-trow').length > 0`), 'few rows');
  check('roles panel has Last seen column', await ev(`document.querySelector('.roles-colhead').textContent.includes('Last seen')`), 'missing');
  const cardSel = `[...document.querySelectorAll('.rl-card, .pop-row')].find((c) => c.textContent.includes('QA UI: approve PR #999'))`;
  check('needs-you card for the QA ask', await until(`!!(${cardSel}) || !!document.querySelector('.attn-more')`), 'no card');
  if (!(await ev(`!!(${cardSel})`))) await ev(`document.querySelector('.attn-more').click()`);
  check('archived-thread ask not in strip', await ev(`![...document.querySelectorAll('.rl-card, .pop-row')].some((c) => c.textContent.includes('QA: approve PR #999 (UI test)'))`), 'archived ask shown');
  check('card shows kind tag', await ev(`(${cardSel}).textContent.includes('Approval')`), 'no Approval tag');
  check('title shows count', await ev(`/^\\(\\d+\\) Agora$/.test(document.title)`), 'title=' + (await ev('document.title')));
  await shot('ui-1-load.png');

  console.log('== open from card');
  await ev(`(${cardSel}).click()`);
  check('URL routes to thread + message', await until(`location.pathname === '/dashboard/threads/${FX.T2}' && location.hash === '#m${FX.UIASK}'`), await ev('location.href'));
  check('reader shows topic', await until(`document.querySelector('.rtitle')?.textContent.startsWith('QA UI run')`), 'wrong title');
  check('focused message highlighted', await ev(`document.getElementById('m${FX.UIASK}')?.classList.contains('rl-msg-focus')`), 'no focus class');
  check('inline code rendered', await ev(`!!document.querySelector('#m${FX.UIASK} code.rl-code')`), 'no code');
  check('PR ref in mono', await ev(`[...document.querySelectorAll('#m${FX.UIASK} .rl-ref')].some((e) => e.textContent === 'PR #999')`), 'no PR ref');
  check('sha in mono', await ev(`[...document.querySelectorAll('#m${FX.UIASK} .rl-sha')].some((e) => e.textContent === '1a2b3c4d')`), 'no sha');
  check('#N links to the message', await ev(`[...document.querySelectorAll('#m${FX.UIASK} .rl-tref')].some((b) => b.textContent === '#${FX.M1}' && b.title.startsWith('Message #${FX.M1}'))`), 'no message link');
  check('"thread N" links to the thread', await ev(`[...document.querySelectorAll('#m${FX.UIASK} .rl-tref')].some((b) => b.textContent === 'thread ${FX.T}')`), 'no thread link');
  check('sender prefix muted', await ev(`document.querySelector('#m${FX.UIASK} .rl-addr')?.textContent.startsWith('coordinator-claude -> operator:')`), 'no addr');
  check('asks-you box with Answer', await ev(`!!document.querySelector('#m${FX.UIASK} .rl-ask .rl-btn-primary')`), 'no Answer');
  console.log('== markdown');
  const MDA = `document.getElementById('m${FX.MD}')`;
  check('md message rendered', await until(`!!${MDA}`), 'missing');
  check('md link: text, href, new tab, noopener', await ev(`(() => { const a = [...${MDA}.querySelectorAll('a.rl-link')].find((x) => x.textContent === 'PR link'); return !!a && a.href === 'https://github.com/example/example-repo/pull/1' && a.target === '_blank' && a.rel.includes('noopener'); })()`), 'bad link');
  check('bare URL linked, trailing dot excluded', await ev(`[...${MDA}.querySelectorAll('a.rl-link')].some((x) => x.getAttribute('href') === 'https://example.com/plan?x=1')`), await ev(`[...${MDA}.querySelectorAll('a')].map((x) => x.getAttribute('href')).join(' | ')`));
  check('bold', await ev(`${MDA}.querySelector('strong')?.textContent === 'Markdown QA'`), 'no strong');
  check('heading', await ev(`${MDA}.querySelector('.rl-mdh2')?.textContent === 'Plan review'`), 'no heading');
  check('bullet list (2 items)', await ev(`${MDA}.querySelectorAll('ul.rl-list > li').length === 2`), 'bad ul');
  check('numbered list (2 items)', await ev(`${MDA}.querySelectorAll('ol.rl-list > li').length === 2`), 'bad ol');
  check('italic + inline code in list', await ev(`${MDA}.querySelector('ul em')?.textContent === 'item' && ${MDA}.querySelector('ul code')?.textContent === 'inline code'`), 'missing');
  check('#N inside list links to message', await ev(`[...${MDA}.querySelectorAll('ul .rl-tref')].some((b) => b.textContent === '#${FX.M1}')`), 'no ref');
  check('blockquote', await ev(`${MDA}.querySelector('blockquote')?.textContent === 'a quote'`), 'no quote');
  check('fenced code block', await ev(`${MDA}.querySelector('pre.rl-pre')?.textContent.startsWith('$ kubectl get pods')`), 'no pre');
  check('javascript: link NOT made a link', await ev(`!${MDA}.querySelector('a[href^="javascript"]') && ${MDA}.textContent.includes('[click](javascript:alert(1))')`), 'unsafe link');
  check('<script>/<b> shown as text, not elements', await ev(`!${MDA}.querySelector('script, b') && ${MDA}.textContent.includes('<script>alert(2)</script>') && ${MDA}.textContent.includes('<b>raw</b>')`), 'raw html rendered');
  check('/\\ and // and encoded schemes are not links', await ev(`(() => { const hrefs = [...${MDA}.querySelectorAll('a')].map((a) => a.getAttribute('href')); return !hrefs.some((h) => /evil|javascript|&#/i.test(h)); })()`), await ev(`[...${MDA}.querySelectorAll('a')].map((a) => a.getAttribute('href')).join(' | ')`));
  check('genuine relative link works', await ev(`[...${MDA}.querySelectorAll('a.rl-link')].some((a) => a.getAttribute('href') === '/dashboard/threads/1' && a.textContent === 'ok')`), 'no relative link');
  check('sender prefix still muted', await ev(`${MDA}.querySelector('.rl-addr')?.textContent.startsWith('coordinator-claude -> operator:')`), 'no addr');
  check('feed excerpt has no markdown symbols', await ev(`(() => { const r = document.querySelector('#feed [data-mid="${FX.MD}"] .rl-ex'); return !!r && !r.textContent.includes('**') && r.textContent.includes('Markdown QA with a PR link'); })()`), await ev(`document.querySelector('#feed [data-mid="${FX.MD}"] .rl-ex')?.textContent`));
  check('pane titles keep 13px', await ev(`getComputedStyle(document.querySelector('.list-head .rl-h2')).fontSize === '13px'`), 'pane heading resized');
  await ev(`${MDA}.scrollIntoView()`);
  await shot('ui-2b-markdown.png');
  await ev(`document.getElementById('m${FX.UIASK}').scrollIntoView({ block: 'center' })`);
  await shot('ui-2-thread.png');

  console.log('== last seen updates in place');
  const rowBefore = await ev(`(() => { const r = document.querySelector('#roles [data-role="coordinator-claude"]'); window.__qaRow = r; return r.getAttribute('data-role'); })()`);
  await fetch(`${BASE}/roles/${rowBefore}/pending`);
  await sleep(5000);
  check('roles row not rebuilt by a last-seen change', await ev(`document.querySelector('#roles [data-role="${rowBefore}"]') === window.__qaRow`), 'row replaced');
  check('watch dot shows running for coordinator-claude', await ev(`!!document.querySelector('#roles [data-role="coordinator-claude"] .role-watch-on')`), await ev(`document.querySelector('#roles [data-role="coordinator-claude"] .role-watch')?.className`));
  check('roles subtitle counts watchers', await ev(`/\\d+ watching/.test(document.getElementById('roles-sub').textContent)`), await ev(`document.getElementById('roles-sub').textContent`));
  check('last-seen cell refreshed', await ev(`/^(now|\\d+m)$/.test(window.__qaRow.querySelector('.role-last').textContent)`), await ev(`window.__qaRow.querySelector('.role-last').textContent`));

  console.log('== answer via composer');
  await ev(`document.querySelector('#m${FX.UIASK} .rl-ask .rl-btn-primary').click()`);
  check('composer shows answering chip', await until(`document.querySelector('.composer-ans')?.textContent.includes('#${FX.UIASK}')`), 'no chip');
  check('draft survives poll re-render', await (async () => {
    await ev(`(() => { const t = document.getElementById('composer-ta'); t.value = 'QA UI answer: approved.'; t.dispatchEvent(new Event('input')); })()`);
    await sleep(4500);
    return ev(`document.getElementById('composer-ta').value === 'QA UI answer: approved.'`);
  })(), 'draft lost');
  await ev(`document.getElementById('composer-post').click()`);
  check('post confirmation shown', await until(`/Posted #\\d+/.test(document.querySelector('.composer-ok')?.textContent || '')`), await ev(`document.querySelector('.composer')?.textContent`));
  const note = await ev(`document.querySelector('.composer-ok')?.textContent`);
  const posted = +(note.match(/Posted #(\d+)/) || [])[1];
  const clip = await ev(`navigator.clipboard.readText().catch(() => 'CLIPBOARD-UNAVAILABLE')`);
  const wantClip = `operator: posted message ${posted} to thread ${FX.T2} (group ${FX.G}), answering #${FX.UIASK}`;
  check('fan-out note copied (or shown)', clip === wantClip || note.includes(wantClip), `clip=[${clip}] note=[${note}]`);
  const hist = await (await fetch(`${BASE}/threads/${FX.T2}/history`)).json();
  const mine = hist.messages.find((m) => m.message_id === posted);
  check('relay has operator reply', mine && mine.sender_role === 'operator' && mine.body === 'QA UI answer: approved.', JSON.stringify(mine));
  const st = await (await fetch(`${BASE}/api/dashboard/state?since_id=999999999`)).json();
  check('ask answered in relay', st.attention.find((a) => a.message_id === +FX.UIASK)?.attn_answered_by === posted, 'not linked');
  check('card disappears after poll', await until(`!(${cardSel})`, 10000), 'still shown');
  check('Answered today lists it', await until(`document.querySelector('.attn-res')?.textContent.includes('answered in #${FX.T2}')`), 'missing');
  check('answered tag on message', await until(`document.getElementById('m${FX.UIASK}')?.textContent.includes('Answered by #${posted}')`), 'missing');
  await shot('ui-3-answered.png');

  console.log('== back to /dashboard');
  await ev(`history.pushState(null, '', '/dashboard/threads/${FX.T2}')`);
  await go(BASE + '/dashboard');
  await until(`document.querySelector('.conn-strong')?.textContent === 'Live'`);
  const firstTid = await ev(`location.pathname`);
  await ev(`document.querySelector('.rl-trow:not(.rl-trow-sel)').click()`);
  await sleep(300);
  const clickedTitle = await ev(`document.querySelector('.rtitle').textContent`);
  await ev(`history.back()`);
  await sleep(600);
  check('Back to /dashboard restores the default thread', await ev(`location.pathname === '/dashboard' && document.querySelector('.rtitle').textContent !== ${JSON.stringify('')}`) && (await ev(`document.querySelector('.rtitle').textContent`)) !== clickedTitle, `path=${await ev('location.pathname')} first=${firstTid}`);

  console.log('== search');
  await ev(`(() => { const q = document.getElementById('q'); q.value = '#${FX.T2}'; q.dispatchEvent(new Event('input')); })()`);
  await sleep(200);
  check('search by #id', await ev(`document.querySelectorAll('.rl-trow').length === 0 || document.querySelector('#list').textContent.includes('No threads match')`)
    ? true : await ev(`[...document.querySelectorAll('.rl-trow')].every((r) => r.textContent.includes('#${FX.T2}'))`), 'unexpected rows');
  await ev(`(() => { const s = document.querySelectorAll('#status-seg button')[2]; s.click(); })()`);
  check('All shows the (active) QA thread', await until(`[...document.querySelectorAll('.rl-trow')].some((r) => r.textContent.includes('#${FX.T2}'))`), 'not found');
  await ev(`(() => { const q = document.getElementById('q'); q.focus(); q.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); })()`);
  check('Esc clears search', await ev(`document.getElementById('q').value === ''`), 'not cleared');

  console.log('== resizable panels');
  const w0 = await ev(`document.querySelector('.list-pane').getBoundingClientRect().width`);
  const r = await ev(`(() => { const b = document.querySelector('[data-split=list]').getBoundingClientRect(); return { x: b.x + b.width / 2, y: b.y + 200 }; })()`);
  await mouse('mousePressed', r.x, r.y, { clickCount: 1 });
  for (let i = 1; i <= 5; i++) await mouse('mouseMoved', r.x + i * 20, r.y);
  await mouse('mouseReleased', r.x + 100, r.y, { clickCount: 1 });
  const w1 = await ev(`document.querySelector('.list-pane').getBoundingClientRect().width`);
  check('drag widens thread list by ~100px', Math.abs(w1 - w0 - 100) <= 2, `${w0} -> ${w1}`);
  check('size saved to localStorage', await ev(`JSON.parse(localStorage.getItem('relay.sizes')).list === ${Math.round(w1)}`), await ev(`localStorage.getItem('relay.sizes')`));
  const rr = await ev(`(() => { const b = document.querySelector('[data-split=rail]').getBoundingClientRect(); return { x: b.x + b.width / 2, y: b.y + 200 }; })()`);
  await mouse('mousePressed', rr.x, rr.y, { clickCount: 1 });
  for (let i = 1; i <= 5; i++) await mouse('mouseMoved', rr.x - i * 10, rr.y);
  await mouse('mouseReleased', rr.x - 50, rr.y, { clickCount: 1 });
  check('drag widens right rail by ~50px', Math.abs((await ev(`document.querySelector('.rail').getBoundingClientRect().width`)) - 430) <= 2, 'rail not 430');
  await ev(`document.querySelector('[data-split=roles]').focus()`);
  const h0 = await ev(`document.querySelector('.roles-pane').getBoundingClientRect().height`);
  await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'ArrowUp', code: 'ArrowUp', windowsVirtualKeyCode: 38 });
  await send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'ArrowUp', code: 'ArrowUp', windowsVirtualKeyCode: 38 });
  check('ArrowUp grows roles pane 16px', Math.abs((await ev(`document.querySelector('.roles-pane').getBoundingClientRect().height`)) - h0 - 16) <= 1, 'no change');
  check('reader keeps >= 560px', await ev(`document.querySelector('.reader').getBoundingClientRect().width >= 560`), 'reader squeezed');
  await shot('ui-4-resized.png');
  await go(BASE + '/dashboard/threads/' + FX.T2);
  check('sizes persist across reload', await until(`Math.abs(document.querySelector('.list-pane').getBoundingClientRect().width - ${w1}) <= 2`), 'reset on reload');
  const d = await ev(`(() => { const b = document.querySelector('[data-split=list]').getBoundingClientRect(); return { x: b.x + b.width / 2, y: b.y + 200 }; })()`);
  await mouse('mousePressed', d.x, d.y, { clickCount: 1 }); await mouse('mouseReleased', d.x, d.y, { clickCount: 1 });
  await mouse('mousePressed', d.x, d.y, { clickCount: 2 }); await mouse('mouseReleased', d.x, d.y, { clickCount: 2 });
  check('double-click resets to 340', await until(`Math.round(document.querySelector('.list-pane').getBoundingClientRect().width) === 340`, 3000), 'not reset');
  await ev(`localStorage.removeItem('relay.sizes')`);

  console.log('== connection lost / recovery (polls failed in-browser only)');
  await send('Fetch.enable', { patterns: [{ urlPattern: '*/api/dashboard/state*' }] });
  const t0 = Date.now();
  check('lost state within ~12s', await until(`document.getElementById('top').classList.contains('top-lost')`, 14000), 'never lost');
  console.log('      (lost after ' + Math.round((Date.now() - t0) / 1000) + 's)');
  check('lost shows Retry button', await ev(`[...document.querySelectorAll('#conn button')].some((b) => b.textContent === 'Retry now')`), 'no retry');
  check('data still readable while lost', await ev(`document.querySelectorAll('.rl-trow').length > 0 && !!document.querySelector('.rtitle')`), 'content gone');
  await shot('ui-5-lost.png');
  await send('Fetch.disable');
  await ev(`[...document.querySelectorAll('#conn button')].find((b) => b.textContent === 'Retry now').click()`);
  check('recovers to Live via Retry', await until(`document.querySelector('.conn-strong')?.textContent === 'Live' && !document.getElementById('top').classList.contains('top-lost')`, 8000), 'no recovery');

  console.log('== narrow layout (1150px: rail tabs)');
  await send('Emulation.setDeviceMetricsOverride', { width: 1150, height: 900, deviceScaleFactor: 1, mobile: false });
  await sleep(400);
  check('rail tabs visible < 1200px', await ev(`getComputedStyle(document.getElementById('rail-tabs')).display !== 'none'`), 'hidden');
  await ev(`document.querySelector('#rail-tabs [data-tab=roles]').click()`);
  check('Roles tab hides feed', await ev(`getComputedStyle(document.getElementById('feed-pane')).display === 'none'`), 'feed visible');
  await shot('ui-6-narrow.png');

  check('no JS errors / console errors', errors.length === 0, errors.join(' | '));
  writeFileSync(join(DIR, 'ui-posted.env'), `UIANSWER=${posted}\n`);
} catch (e) {
  bad('ui run', e.stack || String(e));
} finally {
  console.log(`\nUI QA: ${pass} passed, ${fail} failed`);
  try { ws?.close(); } catch (e) {}
  proc.kill('SIGKILL');
  process.exit(fail ? 1 : 0);
}
