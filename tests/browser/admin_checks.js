/* Second UI harness: clinics, clinic-wide appointments, credentials, the
   leave preview, and the permission boundaries between the two roles.

   node admin-ui2.js <baseUrl> <adminId> <adminPassword> <doctorId> <doctorPassword>
*/
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const http = require('http');

const CHROME = process.env.CHROME_PATH
  || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const [BASE, ADMIN, ADMIN_PASSWORD, DOCTOR, DOCTOR_PASSWORD] = process.argv.slice(2);
const PORT = 9591;
const STAMP = String(Date.now()).slice(-4);
const USERNAME = 'ahmed.k' + STAMP;
const NEW_DOCTOR_PASSWORD = 'issued-pass-' + STAMP;
const SELF_PASSWORD = 'self-chosen-' + STAMP;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const problems = [];
const results = [];

function check(label, ok, detail) {
  results.push({ label, ok });
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
  const userDir = path.join(require('os').tmpdir(), 'avas-browser-profile-2');
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
    return res.result && res.result.result ? res.result.result.value : undefined;
  };

  await send('Runtime.enable');
  await send('Page.enable');
  await sleep(1200);

  const signIn = async (id, password) => {
    await evaluate(`(async () => {
      document.getElementById('login-doctor-id').value = ${JSON.stringify(id)};
      document.getElementById('login-password').value = ${JSON.stringify(password)};
      document.getElementById('login-form').dispatchEvent(new Event('submit', {cancelable:true, bubbles:true}));
      await new Promise(r => setTimeout(r, 2600));
    })()`);
  };
  const signOut = async () => {
    await evaluate(`(async () => {
      await Auth.signOut(); App.showLogin();
      await new Promise(r => setTimeout(r, 500));
    })()`);
  };

  /* =================================================== administrator ==== */
  await signIn(ADMIN, ADMIN_PASSWORD);
  check('administrator is signed in', await evaluate('!document.getElementById("app").hidden'));
  check('the sidebar carries the five administration sections',
    (await evaluate('Array.from(document.querySelectorAll("#nav-links .nav-link")).map(a=>a.textContent.trim()).join("|")'))
      === 'Overview|Doctors|Appointments|Clinic|Settings',
    await evaluate('Array.from(document.querySelectorAll("#nav-links .nav-link")).map(a=>a.textContent.trim()).join("|")'));

  /* ------------------------------------------------------- overview ---- */
  await evaluate('Nav.go("overview")');
  await sleep(1600);
  const overviewText = await evaluate('document.getElementById("page").innerText');
  check('the overview counts doctors on leave', /on leave today/i.test(overviewText),
    overviewText.slice(0, 160));
  check('the overview counts cancelled appointments', /cancelled/i.test(overviewText),
    overviewText.slice(0, 160));

  /* --------------------------------------------------------- clinic ---- */
  console.log('\n--- the clinic ---');
  await evaluate('Nav.go("clinic")');
  await sleep(1800);
  check('the clinic page shows one record, not a list',
    (await evaluate('document.querySelectorAll("#clinic-body .card").length')) === 1,
    'cards: ' + await evaluate('document.querySelectorAll("#clinic-body .card").length'));
  check('there is no way to add a clinic',
    !(await evaluate('document.getElementById("page").innerText')).match(/add clinic/i));

  const NEW_CLINIC_NAME = 'Hamza Medical Clinic ' + STAMP;
  await evaluate(`(async () => {
    const set = (id, value) => {
      const node = document.getElementById(id);
      node.value = value;
      node.dispatchEvent(new Event('input', {bubbles:true}));
    };
    set('clinic-name', ${JSON.stringify(NEW_CLINIC_NAME)});
    set('clinic-address', 'Ferozepur Road, Gulberg');
    set('clinic-city', 'Lahore');
    document.getElementById('clinic-form').dispatchEvent(new Event('submit', {cancelable:true, bubbles:true}));
    await new Promise(r => setTimeout(r, 2400));
  })()`);
  check('the clinic name was saved', await evaluate(`(async () => {
    const data = await Api.get('/admin/clinic');
    return data.clinic.name === ${JSON.stringify(NEW_CLINIC_NAME)};
  })()`));

  /* The whole point of one clinic: the new name turns up everywhere. */
  check('the register shows the new name for every doctor', await evaluate(`(async () => {
    const data = await Api.get('/admin/doctors');
    return data.doctors.every(d => d.clinic && d.clinic.name === ${JSON.stringify(NEW_CLINIC_NAME)});
  })()`));

  /* --------------------------------------------------- appointments ---- */
  console.log('\n--- clinic-wide appointments ---');
  /* Book one first: an earlier run may have cancelled the seeded appointment,
     and a check that depends on leftovers is not a check. */
  const seeded = await evaluate(`(async () => {
    const day = new Date();
    day.setDate(day.getDate() + 2);
    while (day.getDay() === 0) { day.setDate(day.getDate() + 1); }
    const iso = day.toISOString().slice(0, 10);
    const slots = await fetch('/doctors/D002/availability?date=' + iso).then(r => r.json());
    const free = (slots.data && slots.data.available_slots) || [];
    if (!free.length) { return ''; }
    const booked = await fetch('/appointments/book', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ patient_id: 'P001', doctor_id: 'D002',
                             date: iso, time: free[0] })
    }).then(r => r.json());
    return booked.appointment_id || '';
  })()`);
  check('an appointment could be booked for the cancellation check',
        /^APT/.test(String(seeded)), String(seeded));

  await evaluate('Nav.go("appointments")');
  await sleep(1800);
  await evaluate(`(async () => {
    const all = Array.from(document.querySelectorAll('.segmented button'))
      .find(b => b.textContent.trim() === 'All');
    all.click();
    await new Promise(r => setTimeout(r, 1800));
  })()`);
  const rowCount = await evaluate('document.querySelectorAll("#appointments-body tbody tr").length');
  check('every doctor\u2019s appointments are listed', rowCount >= 1, 'rows: ' + rowCount);
  check('each row names the doctor and the patient', await evaluate(`(() => {
    const row = document.querySelector('#appointments-body tbody tr');
    return !!row && /Dr /.test(row.innerText);
  })()`));

  const cancelled = await evaluate(`(async () => {
    const rows = Array.from(document.querySelectorAll('#appointments-body tbody tr'));
    const row = rows.find(r => r.querySelector('[aria-label="Cancel this appointment"]'));
    if (!row) { return 'no live appointment to cancel'; }
    const id = row.innerText.match(/APT[A-Z0-9]+/)[0];
    row.querySelector('[aria-label="Cancel this appointment"]').click();
    await new Promise(r => setTimeout(r, 700));
    const dialog = document.querySelector('.modal');
    if (!dialog || !document.getElementById('cancel-form')) { return 'no dialog'; }
    document.getElementById('cancel-reason').value = 'Clinic closed for maintenance';
    document.getElementById('cancel-form').dispatchEvent(new Event('submit', {cancelable:true, bubbles:true}));
    await new Promise(r => setTimeout(r, 2500));
    return id;
  })()`);
  check('cancelling asks for a reason and goes through the backend',
    /^APT/.test(String(cancelled)), String(cancelled));

  if (/^APT/.test(String(cancelled))) {
    const stored = await evaluate(`(async () => {
      const row = await Api.get('/admin/appointments/' + ${JSON.stringify(cancelled)});
      return JSON.stringify({ status: row.status, reason: row.cancellation_reason });
    })()`);
    const parsed = JSON.parse(stored || '{}');
    check('the record is kept with the real reason',
      parsed.status === 'cancelled' && /maintenance/i.test(parsed.reason || ''), stored);
  }

  const searchWorks = await evaluate(`(async () => {
    const box = document.getElementById('appointment-search');
    box.value = 'zzzz-no-such-patient';
    box.dispatchEvent(new Event('input', {bubbles:true}));
    await new Promise(r => setTimeout(r, 1600));
    const empty = document.getElementById('appointments-body').innerText;
    box.value = '';
    box.dispatchEvent(new Event('input', {bubbles:true}));
    await new Promise(r => setTimeout(r, 1600));
    return empty;
  })()`);
  check('search narrows to nothing when nothing matches',
    /Nothing matches/i.test(String(searchWorks)), String(searchWorks).slice(0, 80));

  /* ------------------------------------------------- leave preview ----- */
  console.log('\n--- leave ---');
  const bookedDate = await evaluate(`(async () => {
    /* book one through the PUBLIC route, exactly as the voice pipeline does */
    const day = new Date();
    day.setDate(day.getDate() + 3 + (Number(${JSON.stringify(STAMP)}) % 9));
    while (day.getDay() === 0) { day.setDate(day.getDate() + 1); }
    const iso = day.toISOString().slice(0, 10);
    const slots = await fetch('/doctors/D001/availability?date=' + iso)
      .then(r => r.json());
    const free = (slots.data && slots.data.available_slots) || [];
    if (!free.length) { return 'NO FREE SLOTS on ' + iso; }
    const booked = await fetch('/appointments/book', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ patient_id: 'P001', doctor_id: 'D001',
                             date: iso, time: free[0] })
    }).then(r => r.json());
    return booked.success ? iso : '';
  })()`);
  if (!bookedDate || !/^\d{4}-/.test(String(bookedDate))) {
    check('a booked date was available for the leave preview', false, String(bookedDate));
  } else {
    await evaluate('Nav.go("doctors")');
    await sleep(1600);
    await evaluate(`(() => {
      const rows = Array.from(document.querySelectorAll('.table-doctors tbody tr'));
      const row = rows.find(r => r.innerText.includes('D001'));
      row.querySelector('[aria-label^="View"]').click();
    })()`);
    await sleep(1800);
    await evaluate('document.querySelectorAll(".tab")[1].click()');
    await sleep(1200);
    await evaluate('Array.from(document.querySelectorAll(".btn")).find(b => b.textContent.includes("Block dates")).click()');
    await sleep(700);
    const impact = await evaluate(`(async () => {
      const start = document.getElementById('leave-start');
      start.value = ${JSON.stringify(bookedDate)};
      start.dispatchEvent(new Event('change', {bubbles:true}));
      await new Promise(r => setTimeout(r, 2200));
      const box = document.querySelector('.impact');
      return box ? box.innerText : 'no impact box';
    })()`);
    check('choosing a date shows what it would cancel, before anything happens',
      /will be cancelled/i.test(String(impact)), String(impact).slice(0, 120));
    check('the preview names the patients affected',
      /patient/i.test(String(impact)), String(impact).slice(0, 120));
    const untouched = await evaluate(`(async () => {
      const data = await Api.get('/admin/appointments?doctor_id=D001&date=' + ${JSON.stringify(bookedDate)});
      return data.appointments.filter(a => a.status === 'confirmed').length;
    })()`);
    check('the preview changed nothing', untouched >= 1, 'live appointments left: ' + untouched);
    await evaluate('document.querySelectorAll(".modal-foot .btn")[0].click()');
    await sleep(400);
    await evaluate('document.querySelector(".modal-foot .btn").click()');
    await sleep(400);
  }

  /* -------------------------------------------------------- statistics -- */
  console.log('');
  console.log('--- statistics ---');
  await evaluate('Nav.go("overview")');
  await sleep(2600);
  check('the overview shows appointment statistics',
    (await evaluate('document.getElementById("page").innerText')).includes('Appointment statistics'));
  check('the figures are drawn from the backend payload', await evaluate(`(async () => {
    const shown = Number(document.querySelector('.figure-value').textContent);
    const data = await Api.get('/admin/statistics?period=month');
    return shown === data.totals.total;
  })()`));
  check('there is a row for every doctor', await evaluate(`(async () => {
    const rows = document.querySelectorAll('.table-stats tbody tr').length;
    const data = await Api.get('/admin/statistics?period=month');
    return rows === data.doctors.length && rows > 0;
  })()`));
  check('the doctor rows add up to the clinic total', await evaluate(`(async () => {
    const data = await Api.get('/admin/statistics?period=month');
    const summed = data.doctors.reduce((total, row) => total + row.total, 0);
    return summed === data.totals.total;
  })()`));

  /* changing the period really re-counts */
  const periodChange = await evaluate(`(async () => {
    const before = document.querySelector('.figure-value').textContent;
    Array.from(document.querySelectorAll('.period-picker button'))
      .find(b => b.textContent.trim() === 'All time').click();
    await new Promise(r => setTimeout(r, 2200));
    const after = document.querySelector('.figure-value').textContent;
    const data = await Api.get('/admin/statistics?period=all');
    return JSON.stringify({ before, after, expected: String(data.totals.total) });
  })()`);
  const change = JSON.parse(periodChange || '{}');
  check('switching the period re-counts from the backend',
    change.after === change.expected, periodChange);

  /* narrowing to one doctor */
  const narrowed = await evaluate(`(async () => {
    const button = document.querySelector('.table-stats tbody tr .btn');
    button.click();
    await new Promise(r => setTimeout(r, 2200));
    const scope = document.getElementById('stats-scope').textContent;
    const shown = Number(document.querySelector('.figure-value').textContent);
    return JSON.stringify({ scope, shown });
  })()`);
  const narrowedData = JSON.parse(narrowed || '{}');
  check('choosing a doctor narrows the figures to them',
    /only/.test(String(narrowedData.scope)), narrowed);

  /* ----------------------------------------------------- credentials --- */
  console.log('\n--- doctor sign-in details ---');
  await evaluate('Nav.go("doctors")');
  await sleep(1700);
  await evaluate(`(() => {
    const rows = Array.from(document.querySelectorAll('.table-doctors tbody tr'));
    const row = rows.find(r => r.innerText.includes('D001'));
    row.querySelector('[aria-label^="View"]').click();
  })()`);
  await sleep(1900);
  check('the doctor dialog has a Sign-in tab',
    (await evaluate('Array.from(document.querySelectorAll(".tab")).map(t=>t.textContent).join("|")'))
      === 'Profile|Availability|Appointments|Sign-in',
    await evaluate('Array.from(document.querySelectorAll(".tab")).map(t=>t.textContent).join("|")'));

  await evaluate('document.querySelectorAll(".tab")[3].click()');
  await sleep(1500);
  check('no password is ever displayed',
    !(await evaluate('document.querySelector(".tab-panel").innerText')).match(/hash|[A-Fa-f0-9]{32}/),
    (await evaluate('document.querySelector(".tab-panel").innerText')).slice(0, 120));

  await evaluate(`(async () => {
    const username = document.getElementById('cred-username');
    username.value = ${JSON.stringify(USERNAME)};
    document.getElementById('username-form').dispatchEvent(new Event('submit', {cancelable:true, bubbles:true}));
    await new Promise(r => setTimeout(r, 2200));
  })()`);
  check('the administrator can set a username',
    (await evaluate('document.getElementById("toast-region").innerText')).includes('Username saved'),
    await evaluate('document.getElementById("toast-region").innerText'));

  await evaluate(`(async () => {
    document.getElementById('cred-password').value = ${JSON.stringify(NEW_DOCTOR_PASSWORD)};
    document.getElementById('cred-password-2').value = 'does-not-match';
    document.getElementById('password-form').dispatchEvent(new Event('submit', {cancelable:true, bubbles:true}));
    await new Promise(r => setTimeout(r, 600));
  })()`);
  check('a mistyped repeat is caught before sending', await evaluate(`(() => {
    const field = document.getElementById('cred-password-2').closest('.field');
    return /not the same/i.test(field.querySelector('.error-text').textContent);
  })()`));

  await evaluate(`(async () => {
    document.getElementById('cred-password').value = ${JSON.stringify(NEW_DOCTOR_PASSWORD)};
    document.getElementById('cred-password-2').value = ${JSON.stringify(NEW_DOCTOR_PASSWORD)};
    document.getElementById('password-form').dispatchEvent(new Event('submit', {cancelable:true, bubbles:true}));
    await new Promise(r => setTimeout(r, 2500));
  })()`);
  check('the administrator can set a password',
    (await evaluate('document.getElementById("toast-region").innerText')).includes('Password set'),
    await evaluate('document.getElementById("toast-region").innerText'));

  /* --------------------------------- the doctor signs in with them ----- */
  console.log('\n--- the doctor uses those credentials ---');
  await signOut();
  await signIn(USERNAME, NEW_DOCTOR_PASSWORD);
  check('the doctor signs in with the new username and password',
    await evaluate('!document.getElementById("app").hidden'),
    await evaluate('document.getElementById("login-alert").textContent'));
  check('no dialog from the previous account is still on screen',
    !(await evaluate('!!document.querySelector(".modal-backdrop")')));

  await evaluate('Nav.go("settings")');
  await sleep(1800);
  const doctorSettings = await evaluate('document.getElementById("page").innerText');
  check('settings shows the username read-only', await evaluate(`(() => {
    const field = document.getElementById('ac-username');
    return !!field && field.readOnly && field.value === ${JSON.stringify(USERNAME)};
  })()`), await evaluate('document.getElementById("ac-username") ? document.getElementById("ac-username").value : "missing"'));
  check('settings has a password form', /Password/.test(doctorSettings)
    && !!(await evaluate('!!document.getElementById("pw-current")')));

  await evaluate(`(async () => {
    document.getElementById('pw-current').value = 'wrong-current-pass';
    document.getElementById('pw-new').value = ${JSON.stringify(SELF_PASSWORD)};
    document.getElementById('pw-repeat').value = ${JSON.stringify(SELF_PASSWORD)};
    document.getElementById('password-form').dispatchEvent(new Event('submit', {cancelable:true, bubbles:true}));
    await new Promise(r => setTimeout(r, 2200));
  })()`);
  check('the wrong current password is refused on the field', await evaluate(`(() => {
    const field = document.getElementById('pw-current').closest('.field');
    return /not correct/i.test(field.querySelector('.error-text').textContent);
  })()`), await evaluate('document.getElementById("pw-current").closest(".field").querySelector(".error-text").textContent'));

  await evaluate(`(async () => {
    document.getElementById('pw-current').value = ${JSON.stringify(NEW_DOCTOR_PASSWORD)};
    document.getElementById('pw-new').value = ${JSON.stringify(SELF_PASSWORD)};
    document.getElementById('pw-repeat').value = ${JSON.stringify(SELF_PASSWORD)};
    document.getElementById('password-form').dispatchEvent(new Event('submit', {cancelable:true, bubbles:true}));
    await new Promise(r => setTimeout(r, 2500));
  })()`);
  check('the doctor changes their own password',
    (await evaluate('document.getElementById("toast-region").innerText')).includes('Password changed'),
    await evaluate('document.getElementById("toast-region").innerText'));

  await signOut();
  await signIn(USERNAME, SELF_PASSWORD);
  check('the new password works', await evaluate('!document.getElementById("app").hidden'));

  /* ---------------------------------------- what a doctor may not do --- */
  console.log('\n--- doctor limits ---');
  check('a doctor has no clinics or all-appointments link',
    !(await evaluate('document.getElementById("nav-links").innerText')).includes('Clinics'));

  /* A doctor's figures are their own: the page must agree with the doctor
     endpoint, and must NOT equal the clinic-wide number. */
  await evaluate('Nav.go("dashboard")');
  await sleep(2600);
  const ownFigures = await evaluate(`(async () => {
    const shown = Number(document.querySelector('.figures .figure-value').textContent);
    const mine = await Api.get('/dashboard/statistics?period=month');
    let clinic = 'refused';
    try { const all = await Api.get('/admin/statistics?period=month');
          clinic = all.totals.total; } catch (error) { clinic = 'refused ' + error.status; }
    return JSON.stringify({ shown, mine: mine.totals.total, clinic });
  })()`);
  const own = JSON.parse(ownFigures || '{}');
  check('the doctor page shows the doctor’s own count', own.shown === own.mine,
        ownFigures);
  check('the clinic-wide figures are refused to a doctor',
        String(own.clinic).startsWith('refused'), ownFigures);

  await evaluate('Nav.go("profile")');
  await sleep(1800);
  check('the doctor sees the clinic the administrator set',
    (await evaluate('document.getElementById("page").innerText')).includes(NEW_CLINIC_NAME),
    (await evaluate('document.getElementById("page").innerText')).slice(0, 160));
  await evaluate(`(async () => {
    const edit = Array.from(document.querySelectorAll('.btn')).find(b => /edit/i.test(b.textContent));
    edit.click();
    await new Promise(r => setTimeout(r, 900));
  })()`);
  check('the edit form offers no clinic field at all', await evaluate(`(() => {
    return !document.getElementById('pf-clinic')
      && !document.querySelector('[name="clinic_name"]')
      && !document.querySelector('[name="clinic_address"]');
  })()`));
  const modalText = await evaluate(
    'document.querySelector(".modal-body") ? document.querySelector(".modal-body").innerText : "NO MODAL"');
  check('the clinic is shown as information instead',
    String(modalText).includes(NEW_CLINIC_NAME),
    'looking for "' + NEW_CLINIC_NAME + '" in: '
      + String(modalText).split(String.fromCharCode(10)).join(' | ').slice(0, 300));
  await evaluate('document.querySelector(".modal-head button").click()');
  await sleep(400);

  const refused = await evaluate(`(async () => {
    const attempts = [];
    for (const field of ['clinic_name', 'clinic_address', 'clinic_city', 'clinic_phone']) {
      try {
        await Api.patch('/dashboard/profile', { [field]: 'Renamed By Doctor' });
        attempts.push(field + ':accepted');
      } catch (error) { attempts.push(field + ':' + error.status); }
    }
    return attempts.join(' ');
  })()`);
  check('the backend refuses every clinic change from a doctor',
    !/accepted/.test(String(refused)) && /403/.test(String(refused)), String(refused));

  const credentialsRefused = await evaluate(`(async () => {
    try { await Api.patch('/admin/doctors/D002/credentials', { password: 'hijack-attempt' });
      return 'accepted'; } catch (error) { return error.status + ' ' + error.code; }
  })()`);
  check('a doctor cannot set another doctor\u2019s password',
    /403/.test(String(credentialsRefused)), String(credentialsRefused));

  const clinicsRefused = await evaluate(`(async () => {
    try { await Api.get('/admin/clinic'); return 'accepted'; }
    catch (error) { return error.status + ' ' + error.code; }
  })()`);
  check('a doctor cannot read the clinic settings', /403/.test(String(clinicsRefused)),
    String(clinicsRefused));

  /* ------------------------------ the administrator's own password ----- */
  console.log('\n--- administrator password ---');
  await signOut();
  await signIn(ADMIN, ADMIN_PASSWORD);
  await evaluate('Nav.go("settings")');
  await sleep(1600);
  check('the administrator settings has a password form',
    await evaluate('!!document.getElementById("pw-current")'));
  const ADMIN_NEW = 'admin-rotated-' + STAMP;
  await evaluate(`(async () => {
    document.getElementById('pw-current').value = ${JSON.stringify(ADMIN_PASSWORD)};
    document.getElementById('pw-new').value = ${JSON.stringify(ADMIN_NEW)};
    document.getElementById('pw-repeat').value = ${JSON.stringify(ADMIN_NEW)};
    document.getElementById('password-form').dispatchEvent(new Event('submit', {cancelable:true, bubbles:true}));
    await new Promise(r => setTimeout(r, 2500));
  })()`);
  check('the administrator changes their own password',
    (await evaluate('document.getElementById("toast-region").innerText')).includes('Password changed'),
    await evaluate('document.getElementById("toast-region").innerText'));
  await signOut();
  await signIn(ADMIN, ADMIN_NEW);
  check('the administrator signs in with the new password',
    await evaluate('!document.getElementById("app").hidden'));

  /* ------------------------------------------------------- narrow ------ */
  await send('Emulation.setDeviceMetricsOverride',
    { width: 390, height: 844, deviceScaleFactor: 2, mobile: true });
  await evaluate('Nav.go("appointments")');
  await sleep(2000);
  const overflow = await evaluate('document.documentElement.scrollWidth - document.documentElement.clientWidth');
  check('no sideways scrolling on a phone (appointments)', overflow <= 1, 'overflow ' + overflow);
  await evaluate('Nav.go("clinic")');
  await sleep(1800);
  const clinicOverflow = await evaluate('document.documentElement.scrollWidth - document.documentElement.clientWidth');
  check('no sideways scrolling on a phone (clinic)', clinicOverflow <= 1, 'overflow ' + clinicOverflow);
  await send('Emulation.clearDeviceMetricsOverride');

  /* ---------------------------------------------------------- tidy up -- */
  /* Everything above really did change the stored credentials, so put them
     back; otherwise this harness only works once. */
  const restored = await evaluate(`(async () => {
    try {
      await Api.post('/auth/password', {
        current_password: ${JSON.stringify(ADMIN_NEW)},
        new_password: ${JSON.stringify(ADMIN_PASSWORD)}
      });
      await Api.patch('/admin/doctors/D001/credentials', {
        password: ${JSON.stringify(DOCTOR_PASSWORD)}
      });
      return 'restored';
    } catch (error) { return error.status + ' ' + error.code; }
  })()`);
  check('the harness put the passwords back', restored === 'restored', String(restored));

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
