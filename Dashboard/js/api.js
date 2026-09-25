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


  function token() {
    try { return sessionStorage.getItem(TOKEN_KEY) || ''; } catch (error) { return ''; }
  }

  function setToken(value) {
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

  return {
    token: token,
    setToken: setToken,
    hasToken: function () { return !!token(); },
    get: function (path, options) { return request(path, Object.assign({}, options)); },
    post: function (path, body, options) {
      return request(path, Object.assign({ method: 'POST', body: body }, options));
    },
    patch: function (path, body, options) {
      return request(path, Object.assign({ method: 'PATCH', body: body }, options));
    },
    del: function (path, options) {
      return request(path, Object.assign({ method: 'DELETE' }, options));
    }
  };
})();
