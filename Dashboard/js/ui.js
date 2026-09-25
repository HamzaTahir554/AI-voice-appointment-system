/* ==========================================================================
   ui.js - shared interface pieces: element building, formatting, badges,
   empty and loading states, toasts, modals, confirmation and validation.

   Everything that renders user data goes through el()/text nodes rather than
   innerHTML, so a patient name can never be interpreted as markup.
   ========================================================================== */
window.UI = (function () {
  'use strict';

  /* ------------------------------------------------------------- DOM ---- */
  function el(tag, props, children) {
    const node = document.createElement(tag);
    Object.keys(props || {}).forEach(function (key) {
      const value = props[key];
      if (value === null || value === undefined || value === false) { return; }
      if (key === 'class') { node.className = value; }
      else if (key === 'text') { node.textContent = value; }
      else if (key === 'html') { node.innerHTML = value; }
      else if (key === 'dataset') { Object.assign(node.dataset, value); }
      else if (key.slice(0, 2) === 'on' && typeof value === 'function') {
        node.addEventListener(key.slice(2).toLowerCase(), value);
      } else { node.setAttribute(key, value === true ? '' : value); }
    });
    (Array.isArray(children) ? children : children ? [children] : [])
      .forEach(function (child) {
        if (child === null || child === undefined || child === false) { return; }
        node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
      });
    return node;
  }

  function icon(name, className) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'icon ' + (className || ''));
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '#i-' + name);
    svg.appendChild(use);
    return svg;
  }

  function clear(node) {
    while (node && node.firstChild) { node.removeChild(node.firstChild); }
    return node;
  }

  function mount(node, children) {
    clear(node);
    (Array.isArray(children) ? children : [children]).forEach(function (child) {
      if (child) { node.appendChild(child); }
    });
    return node;
  }

  /* -------------------------------------------------------- formatting -- */
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

  function parseISO(iso) {
    return new Date(iso + 'T00:00:00');
  }

  function formatDate(iso) {
    if (!iso) { return '-'; }
    const d = parseISO(iso);
    return DAYS[d.getDay()] + ', ' + d.getDate() + ' ' + MONTHS[d.getMonth()] + ' ' + d.getFullYear();
  }

  function formatDateShort(iso) {
    if (!iso) { return '-'; }
    const d = parseISO(iso);
    return d.getDate() + ' ' + MONTHS[d.getMonth()];
  }

  /* 24-hour "16:20" as the doctor reads it: "04:20 PM" */
  function formatTime(time) {
    if (!time) { return '-'; }
    const parts = time.split(':');
    let hour = parseInt(parts[0], 10);
    const minute = parts[1] || '00';
    const suffix = hour >= 12 ? 'PM' : 'AM';
    hour = hour % 12 || 12;
    return String(hour).padStart(2, '0') + ':' + minute + ' ' + suffix;
  }

  function relativeDay(iso) {
    const today = new Date();
    const target = parseISO(iso);
    const diff = Math.round((target - new Date(today.getFullYear(), today.getMonth(), today.getDate())) / 86400000);
    if (diff === 0) { return 'Today'; }
    if (diff === 1) { return 'Tomorrow'; }
    if (diff === -1) { return 'Yesterday'; }
    return formatDateShort(iso);
  }

  /* Accepts a millisecond number or an ISO timestamp, which is what the
     backend stores ("2026-09-25T04:12:07+05:00"). */
  function timeAgo(timestamp) {
    const at = typeof timestamp === 'number' ? timestamp : Date.parse(timestamp);
    if (!at || isNaN(at)) { return ''; }
    const minutes = Math.round((Date.now() - at) / 60000);
    if (minutes < 0) { return 'Just now'; }
    if (minutes < 1) { return 'Just now'; }
    if (minutes < 60) { return minutes + ' min ago'; }
    const hours = Math.round(minutes / 60);
    if (hours < 24) { return hours + (hours === 1 ? ' hour ago' : ' hours ago'); }
    const days = Math.round(hours / 24);
    if (days === 1) { return 'Yesterday'; }
    if (days < 7) { return days + ' days ago'; }
    return Math.round(days / 7) + ' weeks ago';
  }

  function money(amount, currency) {
    return (currency || 'Rs.') + ' ' + Number(amount || 0).toLocaleString('en-PK');
  }

  function initials(name) {
    return String(name || '').replace(/^Dr\.?\s*/i, '').split(/\s+/).slice(0, 2)
      .map(function (word) { return word[0] || ''; }).join('').toUpperCase();
  }

  /* ------------------------------------------------------------ badges -- */
  /* Statuses are the backend's own strings (config.py Status); the label is
     the only thing this layer changes. */
  function statusClass(status) {
    return 'badge badge-' + String(status || 'unknown').toLowerCase().replace(/[\s_]+/g, '-');
  }

  function statusLabel(status) {
    return (window.Store && Store.label) ? Store.label(status) : String(status || '');
  }

  function badge(status) {
    return el('span', { class: statusClass(status), text: statusLabel(status) });
  }

  function avatar(name, large, photo) {
    const node = el('span', {
      class: 'avatar' + (large ? ' avatar-lg' : '') + (photo ? ' avatar-photo' : ''),
      'aria-hidden': 'true'
    });
    if (photo) { node.appendChild(el('img', { src: photo, alt: '' })); }
    else { node.textContent = initials(name); }
    return node;
  }

  /* A profile picture is stored inside the doctor's record, so it has to be
     small: the file the user picked is drawn onto a square canvas at most
     `size` pixels wide and handed back as a JPEG data URL. Nothing is
     uploaded anywhere else - this project has no file storage service. */
  function readImage(file, size) {
    const maxSide = size || 256;
    return new Promise(function (resolve, reject) {
      if (!file || !/^image\//.test(file.type)) {
        reject(new Error('Choose an image file (PNG, JPEG or WebP).'));
        return;
      }
      if (file.size > 8 * 1024 * 1024) {
        reject(new Error('That image is very large. Choose one under 8 MB.'));
        return;
      }
      const reader = new FileReader();
      reader.onerror = function () { reject(new Error('That image could not be read.')); };
      reader.onload = function () {
        const image = new Image();
        image.onerror = function () { reject(new Error('That image could not be read.')); };
        image.onload = function () {
          const side = Math.min(image.width, image.height);
          const canvas = document.createElement('canvas');
          canvas.width = maxSide;
          canvas.height = maxSide;
          const context = canvas.getContext('2d');
          context.drawImage(image,
            (image.width - side) / 2, (image.height - side) / 2, side, side,
            0, 0, maxSide, maxSide);
          resolve(canvas.toDataURL('image/jpeg', 0.82));
        };
        image.src = reader.result;
      };
      reader.readAsDataURL(file);
    });
  }

  /* ------------------------------------------- empty and loading states -- */
  function empty(options) {
    const config = options || {};
    return el('div', { class: 'empty' }, [
      el('span', { class: 'empty-icon' }, icon(config.icon || 'inbox')),
      el('strong', { text: config.title || 'Nothing to show' }),
      config.message ? el('p', { text: config.message }) : null,
      config.action || null
    ]);
  }

  /* Shown in place of a list when the API call failed. Offline gets its own
     wording because that one the doctor can act on. */
  function apiError(error, retry) {
    const offline = error && (error.code === 'offline' || error.code === 'timeout');
    return el('div', { class: 'empty' }, [
      el('span', { class: 'empty-icon' }, icon('alert')),
      el('strong', {
        text: offline ? 'Cannot reach the appointment system' : 'Something went wrong'
      }),
      el('p', { text: (error && error.message) || 'The request could not be completed.' }),
      retry ? el('button', {
        class: 'btn btn-secondary mt-16', type: 'button', onClick: retry
      }, [icon('refresh', 'icon-sm'), 'Try again']) : null
    ]);
  }

  function skeleton(rows) {
    const list = el('div', { class: 'skeleton-list', 'aria-hidden': 'true' });
    for (let i = 0; i < (rows || 4); i += 1) {
      list.appendChild(el('div', { class: 'skeleton skeleton-row' }));
    }
    return list;
  }

  function loading(label) {
    return el('div', { class: 'skeleton-list' }, [
      el('span', { class: 'loading-inline', role: 'status', text: label || 'Loading...' })
    ]);
  }

  function setButtonLoading(button, isLoading) {
    if (!button) { return; }
    button.dataset.loading = isLoading ? 'true' : 'false';
    button.disabled = !!isLoading;
  }

  /* ------------------------------------------------------------ toasts -- */
  function toast(message, type) {
    const region = document.getElementById('toast-region');
    if (!region) { return; }
    const kind = type || 'success';
    const iconName = kind === 'success' ? 'check-circle' : kind === 'error' ? 'alert' : 'info';

    const node = el('div', { class: 'toast toast-' + kind, role: 'status' }, [
      icon(iconName, 'toast-icon'),
      el('span', { class: 'toast-msg', text: message }),
      el('button', {
        class: 'toast-close', type: 'button', 'aria-label': 'Dismiss notification',
        onClick: function () { remove(); }
      }, icon('close', 'icon-sm'))
    ]);

    let removed = false;
    function remove() {
      if (removed) { return; }
      removed = true;
      clearTimeout(timer);
      node.remove();
    }
    const timer = setTimeout(remove, 4000);

    region.appendChild(node);
    return remove;
  }

  /* ------------------------------------------------------------- modal -- */
  const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
  let openModals = 0;

  function modal(options) {
    const config = options || {};
    const lastFocused = document.activeElement;
    const titleId = 'modal-title-' + Math.random().toString(36).slice(2, 8);

    const dialog = el('div', {
      class: 'modal ' + (config.size ? 'modal-' + config.size : ''),
      role: 'dialog',
      'aria-modal': 'true',
      'aria-labelledby': titleId
    }, [
      el('div', { class: 'modal-head' }, [
        el('div', {}, [
          el('h2', { id: titleId, text: config.title || '' }),
          config.subtitle ? el('p', { text: config.subtitle }) : null
        ]),
        el('button', {
          class: 'btn-ghost btn-icon', type: 'button', 'aria-label': 'Close dialog',
          onClick: function () { close(); }
        }, icon('close'))
      ]),
      el('div', { class: 'modal-body' }, config.body || []),
      config.footer ? el('div', { class: 'modal-foot' }, config.footer) : null
    ]);

    const backdrop = el('div', {
      class: 'modal-backdrop',
      onMousedown: function (event) {
        if (event.target === backdrop && config.dismissable !== false) { close(); }
      }
    }, dialog);

    function onKeydown(event) {
      if (event.key === 'Escape' && config.dismissable !== false) {
        event.stopPropagation();
        close();
        return;
      }
      if (event.key !== 'Tab') { return; }
      const items = Array.prototype.filter.call(dialog.querySelectorAll(FOCUSABLE), function (node) {
        return node.offsetParent !== null;
      });
      if (!items.length) { return; }
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    function close() {
      backdrop.remove();
      document.removeEventListener('keydown', onKeydown, true);
      openModals = Math.max(0, openModals - 1);
      if (!openModals) { document.body.style.removeProperty('overflow'); }
      if (lastFocused && lastFocused.focus) { lastFocused.focus(); }
      if (typeof config.onClose === 'function') { config.onClose(); }
    }

    document.body.appendChild(backdrop);
    document.addEventListener('keydown', onKeydown, true);
    openModals += 1;
    document.body.style.overflow = 'hidden';

    const initial = dialog.querySelector('[data-autofocus]')
      || dialog.querySelector(FOCUSABLE);
    if (initial) { initial.focus(); }

    return { close: close, dialog: dialog };
  }

  /* Promise-based confirmation used for every destructive action. */
  /* Shut every dialog. Used when a session ends: a dialog left open by one
     account must not still be on screen for the next one. */
  function closeModals() {
    document.querySelectorAll('.modal-backdrop').forEach(function (node) {
      node.remove();
    });
    openModals = 0;
    document.body.style.removeProperty('overflow');
  }

  function confirm(options) {
    const config = options || {};
    return new Promise(function (resolve) {
      let settled = false;
      const finish = function (value) {
        if (settled) { return; }
        settled = true;
        resolve(value);
      };

      const instance = modal({
        size: 'sm',
        title: config.title || 'Are you sure?',
        body: [el('p', { class: 'muted', text: config.message || '' })],
        footer: [
          el('button', {
            class: 'btn btn-secondary', type: 'button',
            onClick: function () { finish(false); instance.close(); }
          }, config.cancelLabel || 'Cancel'),
          el('button', {
            class: 'btn ' + (config.danger ? 'btn-danger' : 'btn-primary'),
            type: 'button', 'data-autofocus': '',
            onClick: function () { finish(true); instance.close(); }
          }, config.confirmLabel || 'Confirm')
        ],
        onClose: function () { finish(false); }
      });
    });
  }

  /* -------------------------------------------------------- validation -- */
  function fieldError(input, message) {
    const field = input.closest('.field') || input.parentElement;
    const holder = field ? field.querySelector('.error-text') : null;
    input.setAttribute('aria-invalid', message ? 'true' : 'false');
    if (holder) { holder.textContent = message || ''; }
  }

  function clearErrors(form) {
    form.querySelectorAll('.error-text').forEach(function (node) { node.textContent = ''; });
    form.querySelectorAll('[aria-invalid="true"]').forEach(function (node) {
      node.setAttribute('aria-invalid', 'false');
    });
  }

  /* rules: { fieldName: [ {test(value, form), message} ] } */
  function validate(form, rules) {
    clearErrors(form);
    let firstInvalid = null;
    Object.keys(rules).forEach(function (name) {
      const input = form.elements[name];
      if (!input) { return; }
      const value = (input.value || '').trim();
      for (let i = 0; i < rules[name].length; i += 1) {
        const rule = rules[name][i];
        if (!rule.test(value, form)) {
          fieldError(input, rule.message);
          if (!firstInvalid) { firstInvalid = input; }
          break;
        }
      }
    });
    if (firstInvalid) { firstInvalid.focus(); }
    return !firstInvalid;
  }

  const rule = {
    required: function (message) {
      return { test: function (value) { return value.length > 0; }, message: message };
    },
    email: function (message) {
      return {
        test: function (value) { return !value || /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value); },
        message: message
      };
    },
    number: function (message) {
      return {
        test: function (value) { return !value || (!isNaN(Number(value)) && Number(value) >= 0); },
        message: message
      };
    },
    date: function (message) {
      return {
        test: function (value) { return !value || !isNaN(new Date(value + 'T00:00:00').getTime()); },
        message: message
      };
    },
    minLength: function (length, message) {
      return { test: function (value) { return !value || value.length >= length; }, message: message };
    },
    match: function (otherName, message) {
      return {
        test: function (value, form) { return value === (form.elements[otherName].value || '').trim(); },
        message: message
      };
    }
  };

  return {
    el: el, icon: icon, clear: clear, mount: mount,
    formatDate: formatDate, formatDateShort: formatDateShort, formatTime: formatTime,
    relativeDay: relativeDay, timeAgo: timeAgo, money: money, initials: initials,
    badge: badge, statusClass: statusClass, statusLabel: statusLabel, avatar: avatar,
    readImage: readImage,
    empty: empty, apiError: apiError, skeleton: skeleton, loading: loading,
    setButtonLoading: setButtonLoading,
    toast: toast, modal: modal, confirm: confirm, closeModals: closeModals,
    fieldError: fieldError, clearErrors: clearErrors, validate: validate, rule: rule
  };
})();
