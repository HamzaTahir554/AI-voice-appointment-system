/* ==========================================================================
   app.js - starts the dashboard.

   Boot order: theme -> confirm any stored session with the API -> sign-in
   screen, the doctor's dashboard, or the administration area, depending on
   the role the server reports.

   While a doctor has the dashboard open it re-reads today's diary every 30
   seconds, so an appointment booked by the voice assistant appears without anybody
   pressing anything. The same poll is what raises the alerts the doctor
   switched on in Settings: comparing the new diary with the previous one is
   how "a patient just cancelled" can be noticed at all, since the backend
   has no push channel.

   One request per poll: the summary carries today's diary, the unread
   message count and a signature of what is booked from today on, and the
   page it redraws reuses that same answer (store.js keeps it 3 seconds).
   The sign-in answer already contains the doctor's record, so opening the
   dashboard does not fetch the profile first.
   ========================================================================== */
window.App = (function () {
  'use strict';

  const POLL_MS = 30000;
  let shellReady = false;
  let pollTimer = null;
  let session = null;
  let diary = null;          // last seen appointments for today, by id

  function showLogin() {
    stopPolling();
    shellReady = false;
    session = null;
    diary = null;
    Store.reset();
    /* Anything still open belonged to the session that just ended. */
    UI.closeModals();
    document.getElementById('login-screen').hidden = false;
    document.getElementById('app').hidden = true;
    const field = document.getElementById('login-doctor-id');
    if (field) { field.focus(); }
  }

  function showApp(loggedIn) {
    session = loggedIn;
    document.getElementById('login-screen').hidden = true;
    document.getElementById('app').hidden = false;

    if (shellReady) {
      Nav.render();
      startPolling();
      return;
    }

    if (session.role === 'superadmin') {
      Nav.init({ role: 'superadmin', account: session.admin || { name: 'Administrator' } });
      shellReady = true;
      startPolling();
      return;
    }

    const ready = session.doctor
      ? Promise.resolve({ doctor: session.doctor })
      : Store.getProfile();
    ready
      .then(function (data) {
        Store.setDoctor(data.doctor);
        Nav.init({ role: 'doctor', account: data.doctor });
        shellReady = true;
        startPolling();
        rememberDiary();          // also brings the unread count
      })
      .catch(function (error) {
        UI.toast(error.message || 'Could not load your profile.', 'error');
        showLogin();
      });
  }

  /* ----------------------------------------------------------- alerts --- */
  function preferences() {
    const doctor = Store.getCachedDoctor();
    return (doctor && doctor.notification_prefs) || {};
  }

  function index(rows) {
    const map = {};
    (rows || []).forEach(function (row) { map[row.appointment_id] = row; });
    return map;
  }

  function rememberDiary() {
    return Store.getTodayAppointments()
      .then(function (rows) { diary = index(rows); return rows; })
      .catch(function () { return []; });
  }

  /* What changed in today's diary since the last poll, filtered by what the
     doctor asked to hear about. */
  function announce(rows) {
    const now = index(rows);
    if (!diary) { diary = now; return; }
    const prefs = preferences();
    const cancelled = ['cancelled', 'cancelled_by_doctor'];

    Object.keys(now).forEach(function (id) {
      const fresh = now[id];
      const old = diary[id];
      const who = fresh.patient_name || 'A patient';
      const when = UI.formatTime(fresh.time);

      if (!old) {
        if (prefs.new_appointments !== false && Store.isActive(fresh.status)) {
          UI.toast(who + ' booked today at ' + when, 'info');
        }
        return;
      }
      if (old.status !== fresh.status
          && cancelled.indexOf(fresh.status) !== -1
          && cancelled.indexOf(old.status) === -1) {
        if (prefs.cancellations !== false) {
          UI.toast(who + ' cancelled the ' + when + ' appointment', 'info');
        }
        return;
      }
      if (old.time !== fresh.time || old.date !== fresh.date) {
        if (prefs.reschedules !== false) {
          UI.toast(who + ' moved to ' + when, 'info');
        }
      }
    });

    diary = now;
  }

  /* ---------------------------------------------------------- polling --- */
  function startPolling() {
    stopPolling();
    pollTimer = setInterval(function () {
      if (document.visibilityState !== 'visible' || !Api.hasToken()) { return; }
      if (Nav.isAdmin()) {
        Nav.refreshIfRoute(['overview']);
        return;
      }
      Store.getSummary().then(function (summary) {
        announce(summary.today);
        Nav.refreshIfRoute(['dashboard', 'appointments']);
      }).catch(function () { /* offline: try again next time */ });
    }, POLL_MS);
  }

  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  function logout() {
    UI.confirm({
      title: 'Log out?',
      message: 'You will be returned to the sign-in screen.',
      confirmLabel: 'Log out',
      cancelLabel: 'Stay signed in'
    }).then(function (confirmed) {
      if (!confirmed) { return; }
      Auth.signOut().then(function () {
        showLogin();
        UI.toast('You have been signed out', 'info');
      });
    });
  }

  function init() {
    Theme.init();
    Auth.initLoginForm(showApp);

    document.getElementById('logout-btn').addEventListener('click', logout);
    document.getElementById('logout-menu-item').addEventListener('click', logout);

    /* The API says the session is gone (expired, or the service restarted). */
    document.addEventListener('api:unauthorized', function () {
      if (document.getElementById('app').hidden) { return; }
      showLogin();
      UI.toast('Your session has ended. Please sign in again.', 'error');
    });

    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'visible' && Api.hasToken() && shellReady
          && !Nav.isAdmin()) {
        Store.refreshUnreadCount();
      }
    });

    Auth.restore().then(function (restored) {
      if (restored) { showApp(restored); } else { showLogin(); }
    });
  }

  return {
    init: init,
    logout: logout,
    showApp: showApp,
    showLogin: showLogin,
    rememberDiary: rememberDiary
  };
})();

document.addEventListener('DOMContentLoaded', window.App.init);
