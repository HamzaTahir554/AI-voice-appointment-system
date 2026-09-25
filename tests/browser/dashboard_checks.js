/* Drive the SuperAdmin interface in a real browser against a running API.

   Every check is a real interaction: click what a person clicks, wait for the
   network, then assert on what is actually in the DOM. Anything unexpected on
   the console is reported as a failure, because a silent JS error is exactly
   the bug this harness exists to find.

   node admin-ui.js <baseUrl> <adminId> <adminPassword> [doctorId doctorPassword]
*/
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const http = require('http');

const CHROME = process.env.CHROME_PATH
  || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const BASE = process.argv[2];
const ADMIN = process.argv[3];
const ADMIN_PASSWORD = process.argv[4];
const DOCTOR = process.argv[5] || 'D001';
const DOCTOR_PASSWORD = process.argv[6] || ADMIN_PASSWORD;
const PORT = 9561;
const NAME = 'Dr Rabia Noor ' + String(Date.now()).slice(-4);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const problems = [];
const results = [];

function check(label, ok, detail) {
  results.push({ label, ok, detail });
  if (!ok) { problems.push(label + (detail ? ' :: ' + detail : '')); }
  console.log((ok ? '  ok   ' : '  FAIL ') + label + (detail && !ok ? '  -> ' + detail : ''));
}

function getJSON(url) {
  return new Promise((resolve, reject) => {
    http.get(url, (res) => {
      let data = '';
      res.on('data', (c) => { data += c; });
      res.on('end', () => { try { resolve(JSON.parse(data)); } catch (e) { reject(e); } });
    }).on('error', reject);
  });
}

async function main() {
  const userDir = path.join(require('os').tmpdir(), 'avas-browser-profile-1');
  fs.rmSync(userDir, { recursive: true, force: true });
  const chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-first-run',
    '--remote-debugging-port=' + PORT, '--user-data-dir=' + userDir,
    '--window-size=1440,960', BASE], { stdio: 'ignore' });

  let targets = null;
  for (let i = 0; i < 60 && !targets; i += 1) {
    await sleep(250);
    try {
      const list = await getJSON('http://127.0.0.1:' + PORT + '/json/list');
      targets = list.filter((t) => t.type === 'page' && t.webSocketDebuggerUrl);
      if (!targets.length) { targets = null; }
    } catch (e) { /* waiting */ }
  }
  if (!targets) { throw new Error('Chrome did not start'); }

  const ws = new WebSocket(targets[0].webSocketDebuggerUrl);
  let nextId = 1;
  const pending = new Map();
  const consoleErrors = [];

  ws.addEventListener('message', (event) => {
    const msg = JSON.parse(event.data);
    if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); return; }
    if (msg.method === 'Runtime.consoleAPICalled' && msg.params.type === 'error') {
      consoleErrors.push((msg.params.args || []).map((a) => a.value || a.description || '').join(' '));
    }
    if (msg.method === 'Runtime.exceptionThrown') {
      const d = msg.params.exceptionDetails;
      consoleErrors.push('EXCEPTION ' + ((d.exception && d.exception.description) || d.text));
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
    const res = await send('Runtime.evaluate',
      { expression, awaitPromise: true, returnByValue: true });
    if (res.result && res.result.exceptionDetails) {
      return { __error: res.result.exceptionDetails.text
        + ' ' + ((res.result.exceptionDetails.exception || {}).description || '') };
    }
    return res.result && res.result.result ? res.result.result.value : undefined;
  };

  await send('Runtime.enable');
  await send('Page.enable');
  await sleep(1200);

  /* ----------------------------------------------------------- sign in -- */
  console.log('\n--- sign-in screen ---');
  check('no backend address is shown on the sign-in screen',
    !(await evaluate('document.body.innerText')).match(/127\.0\.0\.1|localhost|:8\d{3}/),
    await evaluate('document.body.innerText.slice(0,200)'));
  check('no "Change connection" control exists',
    await evaluate('!document.getElementById("api-target-btn") && !document.getElementById("api-target")'));
  check('no environment variable names on the sign-in screen',
    !(await evaluate('document.body.innerText')).includes('DASHBOARD_PASSWORD'));
  check('forgotten-password help mentions no server configuration', await (async () => {
    await evaluate('document.getElementById("forgot-link").click()');
    await sleep(300);
    const text = await evaluate('document.querySelector(".modal-body").innerText');
    await evaluate('document.querySelector(".modal-head button").click()');
    await sleep(200);
    return !/DASHBOARD_PASSWORD|environment|variable/i.test(text);
  })());

  const signIn = async (id, password) => {
    await evaluate(`(async () => {
      document.getElementById('login-doctor-id').value = ${JSON.stringify(id)};
      document.getElementById('login-password').value = ${JSON.stringify(password)};
      document.getElementById('login-form').dispatchEvent(new Event('submit', {cancelable:true, bubbles:true}));
      await new Promise(r => setTimeout(r, 2500));
    })()`);
  };

  await signIn(ADMIN, ADMIN_PASSWORD);
  check('administrator is signed in', await evaluate('!document.getElementById("app").hidden'),
    await evaluate('document.getElementById("login-alert").textContent'));
  check('the shell knows the role is superadmin',
    (await evaluate('document.getElementById("app").dataset.role')) === 'superadmin');
  check('sidebar shows administration sections only',
    (await evaluate('Array.from(document.querySelectorAll("#nav-links .nav-link")).map(a=>a.textContent.trim()).join("|")'))
      === 'Overview|Doctors|Appointments|Clinic|Settings',
    await evaluate('Array.from(document.querySelectorAll("#nav-links .nav-link")).map(a=>a.textContent.trim()).join("|")'));
  check('patient search is hidden from the administrator',
    await evaluate('document.querySelector(".global-search").hidden'));
  check('the notification bell is hidden from the administrator',
    await evaluate('document.getElementById("bell-btn").hidden'));

  /* ---------------------------------------------------------- overview -- */
  console.log('\n--- overview ---');
  await sleep(1200);
  check('counters are rendered',
    (await evaluate('document.querySelectorAll(".stat-value").length')) >= 5,
    'found ' + await evaluate('document.querySelectorAll(".stat-value").length'));
  const doctorCount = await evaluate('document.querySelector(".stat-value").textContent');
  check('doctor count is a number', /^\d+$/.test(doctorCount), doctorCount);
  check('today card is present',
    (await evaluate('document.body.innerText')).includes("Today's appointments"));

  /* ----------------------------------------------------------- doctors -- */
  console.log('\n--- doctor register ---');
  await evaluate('Nav.go("doctors")');
  await sleep(1500);
  check('the register table is rendered',
    (await evaluate('document.querySelectorAll(".table-doctors tbody tr").length')) >= 4,
    'rows: ' + await evaluate('document.querySelectorAll(".table-doctors tbody tr").length'));
  check('the table has the specified columns',
    (await evaluate('Array.from(document.querySelectorAll(".table-doctors thead th")).map(t=>t.textContent.trim()).join("|")'))
      === 'Doctor|Specialization|Contact|Status|Availability|Actions',
    await evaluate('Array.from(document.querySelectorAll(".table-doctors thead th")).map(t=>t.textContent.trim()).join("|")'));

  /* search */
  await evaluate(`(async () => {
    const box = document.getElementById('doctor-search');
    box.value = 'ahmed';
    box.dispatchEvent(new Event('input', {bubbles:true}));
    await new Promise(r => setTimeout(r, 1500));
  })()`);
  /* "ahmed" legitimately matches Dr Ahmed Khan AND the colleague who works at
     Ahmed Medical Clinic, so the assertion is that every row matches. */
  check('search narrows the register', await evaluate(`(() => {
    const rows = Array.from(document.querySelectorAll('.table-doctors tbody tr'));
    return rows.length > 0 && rows.length < 4
      && rows.every(r => r.innerText.toLowerCase().includes('ahmed'));
  })()`), 'rows: ' + await evaluate('document.querySelectorAll(".table-doctors tbody tr").length'));
  await evaluate(`(async () => {
    const box = document.getElementById('doctor-search');
    box.value = '';
    box.dispatchEvent(new Event('input', {bubbles:true}));
    await new Promise(r => setTimeout(r, 1500));
  })()`);

  /* add a doctor */
  await evaluate('document.querySelector(".page-head .btn-primary").click()');
  await sleep(600);
  check('the add-doctor dialog opens', await evaluate('!!document.getElementById("doctor-form")'));

  await evaluate(`(async () => {
    const set = (id, value) => {
      const node = document.getElementById(id);
      node.value = value;
      node.dispatchEvent(new Event('input', {bubbles:true}));
      node.dispatchEvent(new Event('change', {bubbles:true}));
    };
    set('doctor-name', ${JSON.stringify(NAME)});
    set('doctor-specialization', 'Eye Specialist');
    set('doctor-qualification', 'MBBS, FCPS (Ophthalmology)');
    set('doctor-experience', '7');
    set('doctor-phone', '+923451112233');
    set('doctor-email', 'rabia@clinic.pk');
    set('doctor-fee', '1700');
    set('doctor-duration', '25');
  })()`);

  /* validation first: clear a required field and try to save */
  await evaluate(`(async () => {
    const name = document.getElementById('doctor-name');
    const keep = name.value;
    name.value = '';
    document.getElementById('doctor-form').dispatchEvent(new Event('submit', {cancelable:true, bubbles:true}));
    await new Promise(r => setTimeout(r, 400));
    window.__nameError = document.querySelector('#doctor-name').closest('.field').querySelector('.error-text').textContent;
    name.value = keep;
  })()`);
  check('an empty name is refused before anything is sent',
    !!(await evaluate('window.__nameError')), await evaluate('window.__nameError'));

  await evaluate(`(async () => {
    document.getElementById('doctor-form').dispatchEvent(new Event('submit', {cancelable:true, bubbles:true}));
    await new Promise(r => setTimeout(r, 2500));
  })()`);
  check('the dialog closes after saving', !(await evaluate('!!document.getElementById("doctor-form")')));
  check('a confirmation toast is shown',
    (await evaluate('document.getElementById("toast-region").innerText')).includes(NAME),
    await evaluate('document.getElementById("toast-region").innerText'));
  await sleep(1200);
  check('the new doctor is in the register',
    (await evaluate('document.querySelector(".table-doctors").innerText')).includes('Dr Rabia Noor'));
  check('the new doctor joined the one clinic', await evaluate(`(async () => {
    const data = await Api.get('/admin/doctors?q=' + encodeURIComponent(${JSON.stringify(NAME)}));
    const row = data.doctors[0];
    return !!row && !!row.clinic && row.clinic_ids.length === 1;
  })()`));

  const newId = await evaluate(`(() => {
    const rows = Array.from(document.querySelectorAll('.table-doctors tbody tr'));
    const row = rows.find(r => r.innerText.includes(${JSON.stringify(NAME)}));
    return row ? row.innerText.match(/D\\d{3}/)[0] : '';
  })()`);
  check('the new doctor was given an id', /^D\d{3}$/.test(newId || ''), String(newId));

  /* view dialog */
  await evaluate(`(() => {
    const rows = Array.from(document.querySelectorAll('.table-doctors tbody tr'));
    const row = rows.find(r => r.innerText.includes(${JSON.stringify(NAME)}));
    row.querySelector('.row-actions .btn').click();
  })()`);
  await sleep(1800);
  check('the doctor dialog opens with its four tabs',
    (await evaluate('Array.from(document.querySelectorAll(".tab")).map(t=>t.textContent).join("|")'))
      === 'Profile|Availability|Appointments|Sign-in',
    await evaluate('Array.from(document.querySelectorAll(".tab")).map(t=>t.textContent).join("|")'));
  /* The clinic is central now, so the profile tab shows the doctor's own
     details and the one clinic everybody shares. */
  check('the profile tab shows the stored details', await evaluate(`(async () => {
    const text = document.querySelector('.tab-panel').innerText;
    const central = await Api.get('/admin/clinic');
    return text.includes('Eye Specialist')
      && text.includes('+923451112233')
      && (!central.clinic.name || text.includes(central.clinic.name));
  })()`), await evaluate('document.querySelector(".tab-panel").innerText.slice(0, 160)'));
  await evaluate('document.querySelectorAll(".tab")[1].click()');
  await sleep(1200);
  check('the availability tab lists the working week',
    (await evaluate('document.querySelector(".tab-panel").innerText')).includes('Monday'));
  check('the availability tab can block dates',
    (await evaluate('document.querySelector(".tab-panel").innerText')).includes('Block dates'));
  await evaluate('document.querySelectorAll(".tab")[2].click()');
  await sleep(800);
  check('the appointments tab shows counters',
    (await evaluate('document.querySelectorAll(".mini-stat").length')) === 3);
  await evaluate('document.querySelector(".modal-foot .btn").click()');
  await sleep(400);

  /* deactivate with confirmation */
  await evaluate(`(() => {
    const rows = Array.from(document.querySelectorAll('.table-doctors tbody tr'));
    const row = rows.find(r => r.innerText.includes(${JSON.stringify(NAME)}));
    const buttons = Array.from(row.querySelectorAll('.row-actions .btn'));
    buttons.find(b => (b.getAttribute('aria-label') || '').startsWith('Deactivate')).click();
  })()`);
  await sleep(500);
  check('deactivating asks first',
    (await evaluate('document.querySelector(".modal") ? document.querySelector(".modal").innerText : ""')).includes('Deactivate'),
    await evaluate('document.querySelector(".modal") ? document.querySelector(".modal").innerText.slice(0,120) : "no dialog"'));
  await evaluate('document.querySelector(".modal-foot .btn-danger, .modal-foot .btn-primary").click()');
  await sleep(2000);
  check('the doctor now shows as deactivated', await evaluate(`(() => {
    const rows = Array.from(document.querySelectorAll('.table-doctors tbody tr'));
    const row = rows.find(r => r.innerText.includes(${JSON.stringify(NAME)}));
    return row ? row.innerText.includes('Deactivated') : false;
  })()`));

  /* the deactivated filter */
  await evaluate(`(async () => {
    const select = document.getElementById('doctor-status');
    select.value = 'inactive';
    select.dispatchEvent(new Event('change', {bubbles:true}));
    await new Promise(r => setTimeout(r, 1500));
  })()`);
  check('the Deactivated filter shows deactivated doctors only', await evaluate(`(() => {
    const rows = Array.from(document.querySelectorAll('.table-doctors tbody tr'));
    return rows.length > 0
      && rows.every(r => r.innerText.includes('Deactivated'))
      && rows.some(r => r.innerText.includes(${JSON.stringify(NAME)}));
  })()`));

  /* reactivate */
  await evaluate(`(() => {
    const rows = Array.from(document.querySelectorAll('.table-doctors tbody tr'));
    const row = rows.find(r => r.innerText.includes(${JSON.stringify(NAME)}));
    Array.from(row.querySelectorAll('.row-actions .btn'))
      .find(b => (b.getAttribute('aria-label') || '').startsWith('Activate')).click();
  })()`);
  await sleep(500);
  await evaluate('document.querySelector(".modal-foot .btn-primary, .modal-foot .btn-danger").click()');
  await sleep(2000);
  await evaluate(`(async () => {
    const select = document.getElementById('doctor-status');
    select.value = 'listed';
    select.dispatchEvent(new Event('change', {bubbles:true}));
    await new Promise(r => setTimeout(r, 1500));
  })()`);
  check('the doctor is active again', await evaluate(`(() => {
    const rows = Array.from(document.querySelectorAll('.table-doctors tbody tr'));
    const row = rows.find(r => r.innerText.includes(${JSON.stringify(NAME)}));
    return row ? row.innerText.includes('Active') : false;
  })()`));

  /* remove and restore */
  await evaluate(`(() => {
    const rows = Array.from(document.querySelectorAll('.table-doctors tbody tr'));
    const row = rows.find(r => r.innerText.includes(${JSON.stringify(NAME)}));
    Array.from(row.querySelectorAll('.row-actions .btn'))
      .find(b => (b.getAttribute('aria-label') || '').startsWith('Remove')).click();
  })()`);
  await sleep(500);
  check('removing asks first and says records are kept',
    (await evaluate('document.querySelector(".modal").innerText')).includes('kept'));
  await evaluate('document.querySelector(".modal-foot .btn-danger").click()');
  await sleep(2000);
  check('the removed doctor leaves the default list',
    !(await evaluate('document.querySelector(".table-doctors").innerText')).includes(NAME));
  await evaluate(`(async () => {
    const select = document.getElementById('doctor-status');
    select.value = 'archived';
    select.dispatchEvent(new Event('change', {bubbles:true}));
    await new Promise(r => setTimeout(r, 1500));
  })()`);
  check('the removed doctor is still there under Removed',
    (await evaluate('document.querySelector(".table-doctors").innerText')).includes(NAME));
  await evaluate(`(() => {
    const rows = Array.from(document.querySelectorAll('.table-doctors tbody tr'));
    const row = rows.find(r => r.innerText.includes(${JSON.stringify(NAME)}));
    Array.from(row.querySelectorAll('.row-actions .btn'))
      .find(b => (b.getAttribute('aria-label') || '').startsWith('Restore')).click();
  })()`);
  await sleep(500);
  await evaluate('document.querySelector(".modal-foot .btn-primary").click()');
  await sleep(2000);
  check('restoring works', await evaluate(`(async () => {
    const select = document.getElementById('doctor-status');
    select.value = 'listed';
    select.dispatchEvent(new Event('change', {bubbles:true}));
    await new Promise(r => setTimeout(r, 1500));
    return document.querySelector('.table-doctors').innerText.includes(${JSON.stringify(NAME)});
  })()`));

  /* ---------------------------------------------------------- settings -- */
  console.log('\n--- administrator settings ---');
  await evaluate('Nav.go("settings")');
  await sleep(900);
  const settingsText = await evaluate('document.getElementById("page").innerText');
  check('no connection card', !/Connection and security/i.test(settingsText));
  check('no backend address', !/127\.0\.0\.1|localhost|:8\d{3}/.test(settingsText), settingsText.slice(0, 200));
  check('no environment variable names', !/DASHBOARD_PASSWORD|NOTIFICATION_PROVIDER|environment variable/i.test(settingsText));
  check('appearance offers light, dark and system',
    /Light/.test(settingsText) && /Dark/.test(settingsText) && /System/.test(settingsText));
  check('security section exists with a log out button', /Security/.test(settingsText) && /Log out/.test(settingsText));

  /* ------------------------------------------------- role separation --- */
  console.log('\n--- role separation ---');
  const doctorReach = await evaluate(`(async () => {
    try {
      await Api.get('/dashboard/summary');
      return 'reachable';
    } catch (error) { return error.status + ' ' + error.code; }
  })()`);
  check('the administrator cannot read a doctor dashboard', /403/.test(String(doctorReach)), String(doctorReach));

  /* --------------------------------------------------- the doctor side -- */
  console.log('\n--- doctor side ---');
  await evaluate(`(async () => {
    await Auth.signOut();
    App.showLogin();
    await new Promise(r => setTimeout(r, 400));
  })()`);
  await signIn(DOCTOR, DOCTOR_PASSWORD);
  await sleep(2500);
  check('the doctor is signed in', await evaluate('!document.getElementById("app").hidden'),
    await evaluate('document.getElementById("login-alert").textContent'));
  check('the doctor sees their own sections',
    (await evaluate('Array.from(document.querySelectorAll("#nav-links .nav-link")).map(a=>a.textContent.trim()).join("|")'))
      .startsWith('Dashboard|Appointments|Patients'),
    await evaluate('Array.from(document.querySelectorAll("#nav-links .nav-link")).map(a=>a.textContent.trim()).join("|")'));
  check('a doctor has no administration link',
    !(await evaluate('document.getElementById("nav-links").innerText')).includes('Doctors'));
  const adminReach = await evaluate(`(async () => {
    try { await Api.get('/admin/doctors'); return 'reachable'; }
    catch (error) { return error.status + ' ' + error.code; }
  })()`);
  check('a doctor cannot reach the administration API', /403/.test(String(adminReach)), String(adminReach));

  await evaluate('Nav.go("settings")');
  await sleep(1500);
  const doctorSettings = await evaluate('document.getElementById("page").innerText');
  check('doctor settings: no connection card', !/Connection and security/i.test(doctorSettings));
  check('doctor settings: no address or variables',
    !/127\.0\.0\.1|localhost|:8\d{3}|DASHBOARD_PASSWORD/.test(doctorSettings), doctorSettings.slice(0, 200));
  check('doctor settings: account, alerts, appearance, security',
    /Account/.test(doctorSettings) && /Alerts/.test(doctorSettings)
      && /Appearance/.test(doctorSettings) && /Security/.test(doctorSettings));

  /* alert preference round trip */
  const prefResult = await evaluate(`(async () => {
    const box = document.getElementById('pref-cancellations');
    const before = box.checked;
    box.checked = !before;
    box.dispatchEvent(new Event('change', {bubbles:true}));
    await new Promise(r => setTimeout(r, 2000));
    const stored = await Api.get('/dashboard/profile');
    return JSON.stringify({ before, after: stored.doctor.notification_prefs.cancellations });
  })()`);
  const prefs = JSON.parse(prefResult || '{}');
  check('an alert switch is saved on the server', prefs.before !== prefs.after, prefResult);

  /* the doctor's own pages still work */
  for (const [route, marker] of [['dashboard', 'Today'], ['appointments', 'Appointments'],
                                 ['patients', 'Patients'], ['schedule', 'Weekly'],
                                 ['profile', 'Profile'], ['notifications', 'Notification']]) {
    await evaluate('Nav.go("' + route + '")');
    await sleep(1400);
    const text = await evaluate('document.getElementById("page").innerText');
    check('doctor page still works: ' + route, text.length > 40 && !/Page not available/.test(text),
      text.slice(0, 80));
  }

  /* ------------------------------------------------------- responsive -- */
  console.log('\n--- narrow screen ---');
  await send('Emulation.setDeviceMetricsOverride',
    { width: 390, height: 844, deviceScaleFactor: 2, mobile: true });
  await evaluate('Nav.go("appointments")');
  await sleep(1500);
  const overflow = await evaluate('document.documentElement.scrollWidth - document.documentElement.clientWidth');
  check('no sideways scrolling on a phone (doctor)', overflow <= 1, 'overflow ' + overflow + 'px');

  await evaluate(`(async () => {
    await Auth.signOut();
    App.showLogin();
    await new Promise(r => setTimeout(r, 400));
  })()`);
  await signIn(ADMIN, ADMIN_PASSWORD);
  await sleep(2000);
  await evaluate('Nav.go("doctors")');
  await sleep(1800);
  const adminOverflow = await evaluate('document.documentElement.scrollWidth - document.documentElement.clientWidth');
  check('no sideways scrolling on a phone (register)', adminOverflow <= 1, 'overflow ' + adminOverflow + 'px');
  /* The shared pattern hides the header off-screen and lays each cell out as
     a labelled row, rather than display:none. */
  check('the register collapses into readable blocks', await evaluate(`(() => {
    const head = document.querySelector('.table-doctors thead');
    const cell = document.querySelector('.table-doctors tbody td');
    return getComputedStyle(cell).display === 'flex'
      && getComputedStyle(head).position === 'absolute'
      && !!cell.getAttribute('data-label');
  })()`));
  await send('Emulation.clearDeviceMetricsOverride');

  /* ------------------------------------------------------------ console */
  console.log('\n--- console ---');
  const realErrors = consoleErrors.filter((line) => !/favicon|Failed to load resource/i.test(line));
  check('no console errors during the whole run', realErrors.length === 0,
    realErrors.slice(0, 3).join(' | '));

  console.log('\n=====================================================');
  console.log(results.filter((r) => r.ok).length + ' of ' + results.length + ' checks passed');
  if (problems.length) {
    console.log('\nPROBLEMS:');
    problems.forEach((p) => console.log('  - ' + p));
  }
  ws.close();
  chrome.kill();
  process.exit(problems.length ? 1 : 0);
}

main().catch((e) => { console.error('harness failed:', e); process.exit(2); });
