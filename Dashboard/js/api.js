/* ==========================================================================
   api.js - the HTTP layer between the dashboard and the Appointment Backend.

   Every request goes to the FastAPI service (api/main.py); the dashboard
   never talks to Firebase directly and holds no Firebase credentials. The
   session token from POST /auth/login travels in an Authorization header and
   lives in sessionStorage, so it disappears when the tab closes.

   Where it sends requests is worked out once and never shown in the
   interface: the same origin when the API serves the dashboard at /ui, and
   the local development service when the page is opened straight from disk.
   A deployment that needs a different address sets `avas.api` in
   localStorage; nothing in the interface reads or prints it.

   Reads are shared and, where a caller asks, briefly remembered:
     - two identical GETs in flight at once become one request;
     - `Api.get(path, { cache: ms })` answers again from memory for `ms`;
     - ANY change sent to the server (POST, PATCH, DELETE), signing out, or a
       new session forgets everything remembered, so an edit is never
       followed by an old answer. Appointments are only ever remembered for
       seconds; see store.js for what each screen uses.
   ========================================================================== */
window.Api = (function () {
  'use strict';

  const TOKEN_KEY = 'avas.dashboard.token';
  const BASE_KEY = 'avas.api';
  const TIMEOUT_MS = 15000;

  function storedBase() {
    try { return localStorage.getItem(BASE_KEY); } catch (error) { return null; }
  }

  function detectBase() {
    const override = storedBase();
    if (override) { return override.replace(/\/+$/, ''); }
    if (window.location.protocol === 'file:') { return 'http://127.0.0.1:8000'; }
    // Served from the API at /ui/ - same origin.
    return window.location.origin;
  }

  let base = detectBase();


  /* ------------------------------------------------ shared reads --- */
  const inFlight = new Map();     // path -> promise of the parsed answer
  const remembered = new Map();   // path -> { until, value }
  let generation = 0;             // bumped by every change: late answers are not kept

  function copy(value) {
    if (value === null || typeof value !== 'object') { return value; }
    try { return structuredClone(value); } catch (error) { return JSON.parse(JSON.stringify(value)); }
  }

  function forget(prefix) {
    generation += 1;
    if (!prefix) { remembered.clear(); return; }
    Array.from(remembered.keys()).forEach(function (key) {
      if (key.indexOf(prefix) === 0) { remembered.delete(key); }
    });
  }

  function token() {
    try { return sessionStorage.getItem(TOKEN_KEY) || ''; } catch (error) { return ''; }
  }

  function setToken(value) {
    forget();
    inFlight.clear();
    try {
      if (value) { sessionStorage.setItem(TOKEN_KEY, value); }
      else { sessionStorage.removeItem(TOKEN_KEY); }
    } catch (error) { /* private browsing */ }
  }

  /* An error the interface can show without exposing internals. */
  function ApiError(code, message, status) {
    const error = new Error(message || 'The request failed.');
    error.name = 'ApiError';
    error.code = code || 'error';
    error.status = status || 0;
    return error;
  }

  function describe(status, payload) {
    const detail = payload && payload.detail ? payload.detail : payload;
    const inner = detail && detail.error ? detail.error : null;
    if (inner && inner.message) { return ApiError(inner.code, inner.message, status); }
    if (payload && payload.error && payload.error.message) {
      return ApiError(payload.error.code, payload.error.message, status);
    }
    if (status === 404) { return ApiError('not_found', 'That record no longer exists.', status); }
    if (status === 403) { return ApiError('forbidden', 'You do not have access to that record.', status); }
    if (status === 409) { return ApiError('conflict', 'That change conflicts with an existing appointment.', status); }
    if (status >= 500) { return ApiError('server_error', 'The appointment system reported an error.', status); }
    return ApiError('error', 'The request could not be completed.', status);
  }

  async function request(path, options) {
    const config = options || {};
    const controller = new AbortController();
    const timer = setTimeout(function () { controller.abort(); }, config.timeout || TIMEOUT_MS);

    const headers = { 'Accept': 'application/json' };
    if (config.body !== undefined) { headers['Content-Type'] = 'application/json'; }
    const current = token();
    if (current && !config.anonymous) { headers.Authorization = 'Bearer ' + current; }

    let response;
    try {
      response = await fetch(base + path, {
        method: config.method || 'GET',
        headers: headers,
        body: config.body === undefined ? undefined : JSON.stringify(config.body),
        signal: controller.signal
      });
    } catch (error) {
      clearTimeout(timer);
      if (error.name === 'AbortError') {
        throw ApiError('timeout', 'The appointment system did not answer in time.', 0);
      }
      throw ApiError('offline', 'Cannot reach the appointment system. Is the API running?', 0);
    }
    clearTimeout(timer);

    let payload = null;
    try { payload = await response.json(); } catch (error) { payload = null; }

    if (response.status === 401) {
      setToken('');
      document.dispatchEvent(new CustomEvent('api:unauthorized'));
      throw ApiError('not_signed_in', 'Your session has ended. Please sign in again.', 401);
    }
    if (!response.ok) { throw describe(response.status, payload); }

    // Backend operations answer with the OperationResult envelope; a failure
    // can arrive with HTTP 200 only if success is false, so check it too.
    if (payload && payload.success === false) { throw describe(response.status, payload); }
    if (payload && payload.operation !== undefined) { return payload; }
    return payload && payload.data !== undefined ? payload.data : payload;
  }

  /* A GET: shared with an identical one already on its way, and answered
     from memory when the caller allowed it and the copy is young enough.
     Every caller gets its own copy, so one screen editing what it received
     cannot change what another is shown. */
  function get(path, options) {
    const config = Object.assign({}, options);
    const now = Date.now();
    const kept = remembered.get(path);
    if (config.cache && kept && kept.until > now) {
      return Promise.resolve(copy(kept.value));
    }
    let pending = inFlight.get(path);
    if (!pending) {
      const startedIn = generation;
      pending = request(path, config).then(function (value) {
        if (config.cache && startedIn === generation) {
          remembered.set(path, { until: Date.now() + config.cache, value: value });
        }
        return value;
      });
      const done = function () { if (inFlight.get(path) === pending) { inFlight.delete(path); } };
      pending.then(done, done);
      inFlight.set(path, pending);
    }
    return pending.then(copy);
  }

  /* Anything that changes data: whatever was remembered may now be wrong. */
  function change(path, options) {
    forget();
    return request(path, options).finally(function () { forget(); });
  }

  return {
    token: token,
    setToken: setToken,
    hasToken: function () { return !!token(); },
    get: get,
    forget: forget,
    post: function (path, body, options) {
      return change(path, Object.assign({ method: 'POST', body: body }, options));
    },
    patch: function (path, body, options) {
      return change(path, Object.assign({ method: 'PATCH', body: body }, options));
    },
    del: function (path, options) {
      return change(path, Object.assign({ method: 'DELETE' }, options));
    }
  };
})();
