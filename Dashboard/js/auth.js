/* ==========================================================================
   auth.js - signing in.

   One form, two kinds of account. POST /auth/login answers with the role the
   id belongs to:

       doctor      -> the doctor's own dashboard
       superadmin  -> the administration area

   The password is checked on the server; nothing about the server, its
   address or its configuration is shown here, and no credential is stored,
   defaulted or hinted at anywhere in this folder.
   ========================================================================== */
window.Auth = (function () {
  'use strict';

  const ROLE_KEY = 'avas.dashboard.role';

  function isSignedIn() {
    return Api.hasToken();
  }

  function rememberRole(role) {
    try { sessionStorage.setItem(ROLE_KEY, role || ''); } catch (error) { /* private mode */ }
  }

  function role() {
    try { return sessionStorage.getItem(ROLE_KEY) || ''; } catch (error) { return ''; }
  }

  function isAdmin() { return role() === 'superadmin'; }

  /* The token may be stale (the service restarts and forgets its sessions),
     so a stored token is confirmed with the server before anything opens. */
  function restore() {
    if (!Api.hasToken()) { return Promise.resolve(null); }
    return Api.get('/auth/session')
      .then(function (session) {
        rememberRole(session.role);
        return session;
      })
      .catch(function () { Api.setToken(''); rememberRole(''); return null; });
  }

  function signIn(userId, password) {
    return Api.post('/auth/login', {
      user_id: String(userId).trim().toUpperCase(),
      password: password
    }, { anonymous: true }).then(function (data) {
      Api.setToken(data.token);
      rememberRole(data.role);
      return data;
    });
  }

  function signOut() {
    const finish = function () {
      Api.setToken('');
      rememberRole('');
      Store.reset();
    };
    return Api.post('/auth/logout').then(finish).catch(finish);
  }

  /* ---------------------------------------------------------- the form -- */
  function initLoginForm(onSuccess) {
    const form = document.getElementById('login-form');
    if (!form) { return; }

    const alertBox = document.getElementById('login-alert');
    const submit = document.getElementById('login-submit');
    const passwordInput = form.elements.password;
    const toggle = document.getElementById('password-toggle');

    toggle.addEventListener('click', function () {
      const showing = passwordInput.type === 'text';
      passwordInput.type = showing ? 'password' : 'text';
      toggle.setAttribute('aria-label', showing ? 'Show password' : 'Hide password');
      toggle.setAttribute('aria-pressed', showing ? 'false' : 'true');
      UI.mount(toggle, UI.icon(showing ? 'eye' : 'eye-off'));
      passwordInput.focus();
    });

    document.getElementById('forgot-link').addEventListener('click', function (event) {
      event.preventDefault();
      const instance = UI.modal({
        size: 'sm',
        title: 'Forgotten password',
        body: [
          UI.el('p', {
            class: 'muted',
            text: 'Passwords for this system are issued by the clinic '
              + 'administrator. Ask them to set a new one for you - there is '
              + 'no self-service reset.'
          })
        ],
        footer: [
          UI.el('button', {
            class: 'btn btn-primary', type: 'button', 'data-autofocus': '',
            onClick: function () { instance.close(); }
          }, 'Got it')
        ]
      });
    });

    form.addEventListener('submit', function (event) {
      event.preventDefault();
      alertBox.hidden = true;

      const valid = UI.validate(form, {
        user_id: [UI.rule.required('Enter your user ID, for example D001.')],
        password: [UI.rule.required('Password is required.')]
      });
      if (!valid) { return; }

      UI.setButtonLoading(submit, true);
      signIn(form.elements.user_id.value, form.elements.password.value)
        .then(function (session) {
          UI.setButtonLoading(submit, false);
          form.reset();
          onSuccess(session);
        })
        .catch(function (error) {
          UI.setButtonLoading(submit, false);
          showError(alertBox, error);
          passwordInput.focus();
          passwordInput.select();
        });
    });
  }

  function showError(alertBox, error) {
    /* Wrong credentials stay deliberately vague; a connection problem says so
       plainly, without naming any address or internal detail. */
    let message = 'Invalid user ID or password.';
    if (error && (error.code === 'offline' || error.code === 'timeout')) {
      message = 'Cannot reach the appointment system. Please try again in a '
        + 'moment, or ask your administrator to check it.';
    } else if (error && error.code === 'account_inactive') {
      message = error.message;
    } else if (error && error.status >= 500) {
      message = 'The appointment system reported an error. Please try again.';
    }
    UI.mount(alertBox, [UI.icon('alert', 'icon-sm'), UI.el('span', { text: message })]);
    alertBox.hidden = false;
  }

  return {
    isSignedIn: isSignedIn,
    restore: restore,
    signIn: signIn,
    signOut: signOut,
    role: role,
    isAdmin: isAdmin,
    initLoginForm: initLoginForm
  };
})();
