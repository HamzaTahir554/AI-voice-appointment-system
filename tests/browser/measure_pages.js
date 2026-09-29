/* Measure how the dashboard really loads, page by page.

   For each page it records, from the moment of the click until the network
   goes quiet:
     - elapsed time
     - how many API requests the page made (duplicates included)
     - the database work behind them: round trips and documents read, summed
       from each response's Server-Timing header

   The API must run with PERF_DEBUG=1, or there is no Server-Timing header to
   read. Nothing here writes to the database: it signs in and opens pages.

   The dashboard's 30-second background refresh is not allowed to fire on
   its own schedule, or it could land inside some page's timing (it once
   did, and cancelled a request mid-flight on the next page load). The
   harness keeps the refresh callback instead and runs it once, on purpose,
   30 seconds after the page it refreshes - as it would really run - as its
   own measured step ("30-second refresh"). The app is not changed.

   node tests/browser/measure_pages.js <baseUrl> <doctorId> <doctorPw> <adminId> <adminPw> [out.json]
*/
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const http = require('http');

const CHROME = process.env.CHROME_PATH
  || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const [BASE, DOCTOR, DOCTOR_PW, ADMIN, ADMIN_PW, OUT] = process.argv.slice(2);
const PORT = 9631;
const QUIET_MS = 400;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function getJSON(url) {
  return new Promise((resolve, reject) => {
    http.get(url, (res) => {
      let data = '';
      res.on('data', (c) => { data += c; });
      res.on('end', () => { try { resolve(JSON.parse(data)); } catch (e) { reject(e); } });
    }).on('error', reject);
  });
}

function parseServerTiming(header) {
  const out = { calls: 0, reads: 0, dbMs: 0, appMs: 0 };
  if (!header) { return out; }
  const app = /app;dur=([\d.]+)/.exec(header);
  const db = /db;dur=([\d.]+);desc="calls=(\d+) reads=(\d+)/.exec(header);
  if (app) { out.appMs = Number(app[1]); }
  if (db) { out.dbMs = Number(db[1]); out.calls = Number(db[2]); out.reads = Number(db[3]); }
  return out;
}

async function main() {
  const userDir = path.join(os.tmpdir(), 'avas-measure-profile');
  fs.rmSync(userDir, { recursive: true, force: true });
  const chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-first-run',
    '--remote-debugging-port=' + PORT, '--user-data-dir=' + userDir,
    '--window-size=1440,960', 'about:blank'], { stdio: 'ignore' });

  let targets = null;
  for (let i = 0; i < 60 && !targets; i += 1) {
    await sleep(250);
    try {
      const list = await getJSON('http://127.0.0.1:' + PORT + '/json/list');
      targets = list.filter((t) => t.type === 'page' && t.webSocketDebuggerUrl);
      if (!targets.length) { targets = null; }
    } catch (e) { /* waiting */ }
  }
  const ws = new WebSocket(targets[0].webSocketDebuggerUrl);
  let nextId = 1;
  const pending = new Map();

  /* every API request in flight, and a log of finished ones */
  const inFlight = new Map();
  let finished = [];
  let lastActivity = Date.now();
  const apiOrigin = new URL(BASE).origin;
  const isApi = (url) => url.startsWith(apiOrigin) && !url.includes('/ui/');

  ws.addEventListener('message', (event) => {
    const msg = JSON.parse(event.data);
    if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); return; }
    const p = msg.params || {};
    if (msg.method === 'Network.requestWillBeSent' && isApi(p.request.url)
        && p.request.method !== 'OPTIONS') {
      inFlight.set(p.requestId, { url: p.request.url, method: p.request.method, timing: null });
      lastActivity = Date.now();
    }
    if (msg.method === 'Network.responseReceived' && inFlight.has(p.requestId)) {
      const headers = p.response.headers || {};
      const key = Object.keys(headers).find((h) => h.toLowerCase() === 'server-timing');
      inFlight.get(p.requestId).timing = parseServerTiming(key ? headers[key] : '');
      inFlight.get(p.requestId).status = p.response.status;
    }
    if ((msg.method === 'Network.loadingFinished' || msg.method === 'Network.loadingFailed')
        && inFlight.has(p.requestId)) {
      finished.push(inFlight.get(p.requestId));
      inFlight.delete(p.requestId);
      lastActivity = Date.now();
    }
  });

  await new Promise((resolve, reject) => {
    ws.addEventListener('open', resolve);
    ws.addEventListener('error', reject);
  });
  const send = (method, params) => new Promise((resolve) => {
    const id = nextId++;
    pending.set(id, resolve);
    ws.send(JSON.stringify({ id, method, params: params || {} }));
  });
  const evaluate = async (expression) => {
    const res = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
    return res.result && res.result.result ? res.result.result.value : undefined;
  };

  await send('Runtime.enable');
  await send('Network.enable');
  await send('Network.setCacheDisabled', { cacheDisabled: false });
  await send('Page.enable');
  await send('Page.addScriptToEvaluateOnNewDocument', { source: `
    (function () {
      const schedule = window.setInterval;
      window.setInterval = function (fn, ms) {
        if (ms === 30000) { window.__avasRefresh = fn; return 0; }
        return schedule.apply(window, arguments);
      };
    })();` });

  /* Run an action, then wait for the network to go quiet. */
  async function measure(label, action) {
    finished = [];
    inFlight.clear();            // nothing from an earlier page counts here
    const started = Date.now();
    lastActivity = started;
    await evaluate(action);
    const deadline = started + 60000;
    while (Date.now() < deadline) {
      await sleep(50);
      if (inFlight.size === 0 && Date.now() - lastActivity >= QUIET_MS) { break; }
    }
    const elapsed = Math.max(0, lastActivity - started);
    const requests = finished.slice();
    const sum = requests.reduce((acc, r) => {
      const t = r.timing || {};
      acc.calls += t.calls || 0; acc.reads += t.reads || 0; acc.dbMs += t.dbMs || 0;
      return acc;
    }, { calls: 0, reads: 0, dbMs: 0 });
    const paths = requests.map((r) => {
      const url = new URL(r.url);
      return r.method + ' ' + url.pathname + url.search;
    });
    const row = { page: label, ms: elapsed, requests: requests.length,
                  dbCalls: sum.calls, reads: sum.reads, paths };
    console.log(label.padEnd(30) + String(elapsed).padStart(7) + ' ms'
      + String(requests.length).padStart(5) + ' req'
      + String(sum.calls).padStart(6) + ' db calls'
      + String(sum.reads).padStart(7) + ' reads');
    return row;
  }

  const results = [];
  async function openApp() {
    await send('Page.navigate', { url: BASE });
    await sleep(2500);
    inFlight.clear();            // requests cut off by the navigation never finish
  }
  const refresh = '(() => { if (window.__avasRefresh) { window.__avasRefresh(); } })()';
  const REFRESH_EVERY = 30000;
  const signIn = (id, pw) => `(async () => {
      document.getElementById('login-doctor-id').value = ${JSON.stringify(id)};
      document.getElementById('login-password').value = ${JSON.stringify(pw)};
      document.getElementById('login-form').dispatchEvent(new Event('submit', {cancelable:true, bubbles:true}));
    })()`;
  const go = (route) => `(() => { Nav.go(${JSON.stringify(route)}); })()`;

  /* ---------------------------------------------------------- doctor --- */
  if (DOCTOR && DOCTOR_PW) {
    console.log('\n--- doctor ' + DOCTOR + ' ---');
    await openApp();
    results.push(await measure('doctor: sign in -> dashboard', signIn(DOCTOR, DOCTOR_PW)));
    const signedIn = await evaluate('!document.getElementById("app").hidden');
    if (!signedIn) {
      console.log('   (doctor sign-in failed - skipping doctor pages)');
    } else {
      for (const route of ['appointments', 'patients', 'schedule', 'profile',
                           'notifications', 'settings', 'dashboard']) {
        results.push(await measure('doctor: ' + route, go(route)));
      }
      await sleep(REFRESH_EVERY);
      results.push(await measure('doctor: 30-second refresh', refresh));
      await evaluate('(async () => { await Auth.signOut(); App.showLogin(); })()');
      await sleep(800);
    }
  }

  /* ----------------------------------------------------------- admin --- */
  if (ADMIN && ADMIN_PW) {
    console.log('\n--- administrator ---');
    await openApp();
    results.push(await measure('admin: sign in -> overview', signIn(ADMIN, ADMIN_PW)));
    const signedIn = await evaluate('!document.getElementById("app").hidden');
    if (!signedIn) {
      console.log('   (administrator sign-in failed - skipping admin pages)');
    } else {
      for (const route of ['doctors', 'appointments', 'clinic', 'settings', 'overview']) {
        results.push(await measure('admin: ' + route, go(route)));
      }
      await sleep(REFRESH_EVERY);
      results.push(await measure('admin: 30-second refresh', refresh));
      await evaluate('(async () => { await Auth.signOut(); App.showLogin(); })()');
    }
  }

  if (OUT) { fs.writeFileSync(OUT, JSON.stringify(results, null, 2)); }
  ws.close();
  chrome.kill();
  process.exit(0);
}

main().catch((e) => { console.error('measurement failed:', e); process.exit(2); });
