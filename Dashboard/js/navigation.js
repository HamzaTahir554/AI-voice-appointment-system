/* ==========================================================================
   navigation.js - the application shell: sidebar, routing, header, global
   search, theme and the account menu.

   The shell serves two kinds of account. The signed-in role decides which
   sections exist, so a doctor never sees an administration link and the
   administrator never sees a doctor's patients:

       doctor      Dashboard, Appointments, Patients, Schedule, Profile,
                   Notifications, Settings
       superadmin  Overview, Doctors, Settings

   Routing is hash based (#/doctors) so the browser back button works and a
   page can be linked to directly, without any server.
   ========================================================================== */
window.Theme = (function () {
  'use strict';
  const KEY = 'avas.dashboard.theme';

  function stored() {
    try { return localStorage.getItem(KEY); } catch (error) { return null; }
  }

  function systemTheme() {
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches
      ? 'dark' : 'light';
  }

  function current() {
    return document.documentElement.getAttribute('data-theme') || 'light';
  }

  /* 'system' follows the operating system and keeps following it. */
  function preference() {
    return stored() || 'system';
  }

  function apply(choice) {
    const theme = choice === 'system' ? systemTheme() : choice;
    document.documentElement.setAttribute('data-theme', theme);
    try {
      if (choice === 'system') { localStorage.removeItem(KEY); }
      else { localStorage.setItem(KEY, choice); }
    } catch (error) { /* storage blocked */ }
    document.dispatchEvent(new CustomEvent('themechange', { detail: theme }));
  }

  function init() {
    document.documentElement.setAttribute('data-theme', stored() || systemTheme());
    if (window.matchMedia) {
      window.matchMedia('(prefers-color-scheme: dark)')
        .addEventListener('change', function () {
          if (!stored()) { apply('system'); }
        });
    }
  }

  return { init: init, apply: apply, current: current, preference: preference };
})();

window.Nav = (function () {
  'use strict';

  const DOCTOR_ROUTES = [
    { id: 'dashboard', label: 'Dashboard', icon: 'home', title: 'Dashboard' },
    { id: 'appointments', label: 'Appointments', icon: 'calendar', title: 'Appointments' },
    { id: 'patients', label: 'Patients', icon: 'users', title: 'Patients' },
    { id: 'schedule', label: 'Schedule', icon: 'clock', title: 'My Schedule' },
    { id: 'profile', label: 'Doctor Profile', icon: 'user', title: 'Doctor Profile' },
    { id: 'notifications', label: 'Notifications', icon: 'bell', title: 'Notifications' },
    { id: 'settings', label: 'Settings', icon: 'settings', title: 'Settings' }
  ];

  const ADMIN_ROUTES = [
    { id: 'overview', label: 'Overview', icon: 'home', title: 'Clinic overview',
      page: 'adminOverview' },
    { id: 'doctors', label: 'Doctors', icon: 'users', title: 'Doctors',
      page: 'adminDoctors' },
    { id: 'appointments', label: 'Appointments', icon: 'calendar',
      title: 'Appointments', page: 'adminAppointments' },
    { id: 'clinic', label: 'Clinic', icon: 'building', title: 'Clinic',
      page: 'adminClinic' },
    { id: 'settings', label: 'Settings', icon: 'settings', title: 'Settings' }
  ];

  let role = 'doctor';
  let account = null;
  let currentRoute = null;

  function routes() {
    return role === 'superadmin' ? ADMIN_ROUTES : DOCTOR_ROUTES;
  }

  function isAdmin() { return role === 'superadmin'; }

  function greeting() {
    const hour = new Date().getHours();
    if (hour < 12) { return 'Good morning'; }
    if (hour < 17) { return 'Good afternoon'; }
    return 'Good evening';
  }

  /* ---------------------------------------------------------- sidebar --- */
  function buildSidebar() {
    const nav = document.getElementById('nav-links');
    UI.mount(nav, routes().map(function (route) {
      const link = UI.el('a', {
        class: 'nav-link',
        href: '#/' + route.id,
        'data-route': route.id
      }, [UI.icon(route.icon), UI.el('span', { text: route.label })]);
      if (route.id === 'notifications') {
        link.appendChild(UI.el('span', { class: 'nav-badge', id: 'nav-unread', hidden: true }));
      }
      return link;
    }));
  }

  function updateUnreadBadges() {
    if (isAdmin()) { return; }
    const count = Store.getUnreadCount();
    const navBadge = document.getElementById('nav-unread');
    const bellBadge = document.getElementById('bell-count');
    [navBadge, bellBadge].forEach(function (node) {
      if (!node) { return; }
      node.textContent = count > 9 ? '9+' : String(count);
      node.hidden = count === 0;
    });
    const bell = document.getElementById('bell-btn');
    if (bell) {
      bell.setAttribute('aria-label', count
        ? 'Notifications, ' + count + ' unread'
        : 'Notifications, none unread');
    }
  }

  /* ----------------------------------------------------------- drawer --- */
  function setDrawer(open) {
    const app = document.getElementById('app');
    const overlay = document.getElementById('sidebar-overlay');
    const toggle = document.getElementById('menu-toggle');
    app.classList.toggle('nav-open', open);
    overlay.hidden = !open;
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) {
      const first = document.querySelector('#nav-links .nav-link');
      if (first) { first.focus(); }
    }
  }

  function isDrawerLayout() {
    return window.matchMedia('(max-width: 1024px)').matches;
  }

  /* ---------------------------------------------------------- routing --- */
  function routeFromHash() {
    const id = (window.location.hash || '').replace(/^#\/?/, '').split('?')[0];
    const known = routes().some(function (route) { return route.id === id; });
    return known ? id : routes()[0].id;
  }

  function go(routeId) {
    if (window.location.hash === '#/' + routeId) { render(); return; }
    window.location.hash = '#/' + routeId;
  }

  function render() {
    const routeId = routeFromHash();
    currentRoute = routeId;
    const route = routes().find(function (item) { return item.id === routeId; });

    document.querySelectorAll('#nav-links .nav-link').forEach(function (link) {
      const active = link.dataset.route === routeId;
      if (active) { link.setAttribute('aria-current', 'page'); }
      else { link.removeAttribute('aria-current'); }
    });

    document.getElementById('page-title').textContent = route.title;
    document.getElementById('page-subtitle').textContent = subtitleFor(routeId);
    document.title = route.title + ' - AI Voice Appointment System';

    const page = document.getElementById('page');
    UI.mount(page, UI.loading('Loading ' + route.label.toLowerCase() + '...'));
    page.focus({ preventScroll: true });
    window.scrollTo({ top: 0 });

    const module = window.Pages && window.Pages[route.page || routeId];
    if (module) {
      module.render(page);
    } else {
      UI.mount(page, UI.empty({
        title: 'Page not available',
        message: 'This section could not be loaded. Please reload the page.'
      }));
    }

    if (isDrawerLayout()) { setDrawer(false); }
  }

  function subtitleFor(routeId) {
    if (isAdmin()) {
      return routeId === 'overview'
        ? greeting() + '. Here is how the clinic looks today.'
        : 'Clinic administration';
    }
    if (routeId === 'dashboard') {
      return greeting() + ', ' + (account ? account.name : 'Doctor');
    }
    return 'AI Voice Appointment System';
  }

  /* ----------------------------------------------------- global search --- */
  function initSearch() {
    const wrapper = document.querySelector('.global-search');
    const input = document.getElementById('global-search');
    const results = document.getElementById('search-results');

    /* The administrator has no patient or appointment data of their own;
       the doctor list has its own search box on its page. */
    if (isAdmin()) {
      if (wrapper) { wrapper.hidden = true; }
      return;
    }
    if (wrapper) { wrapper.hidden = false; }
    let timer = null;

    function hide() {
      results.hidden = true;
      UI.clear(results);
      input.setAttribute('aria-expanded', 'false');
    }

    function show(data, query) {
      const groups = [];
      if (data.pages.length) {
        groups.push(UI.el('p', { class: 'search-group-label', text: 'Pages' }));
        data.pages.forEach(function (page) {
          groups.push(UI.el('button', {
            class: 'search-item', type: 'button',
            onClick: function () { hide(); input.value = ''; go(page.id); }
          }, [UI.icon('arrow-right', 'icon-sm'), UI.el('span', { text: page.label, class: 'grow' })]));
        });
      }
      if (data.patients.length) {
        groups.push(UI.el('p', { class: 'search-group-label', text: 'Patients' }));
        data.patients.forEach(function (patient) {
          groups.push(UI.el('button', {
            class: 'search-item', type: 'button',
            onClick: function () {
              hide(); input.value = '';
              go('patients');
              setTimeout(function () { window.PatientActions.openDetails(patient.patient_id); }, 300);
            }
          }, [UI.icon('user', 'icon-sm'), UI.el('span', { text: patient.name }),
            UI.el('span', { text: patient.patient_id })]));
        });
      }
      if (data.appointments.length) {
        groups.push(UI.el('p', { class: 'search-group-label', text: 'Appointments' }));
        data.appointments.forEach(function (appointment) {
          groups.push(UI.el('button', {
            class: 'search-item', type: 'button',
            onClick: function () {
              hide(); input.value = '';
              window.AppointmentActions.openDetails(appointment.appointment_id);
            }
          }, [
            UI.icon('calendar', 'icon-sm'),
            UI.el('span', { text: appointment.patient_name + ' - ' + UI.formatTime(appointment.time) }),
            UI.el('span', { text: UI.relativeDay(appointment.date) })
          ]));
        });
      }
      if (!groups.length) {
        groups.push(UI.el('p', { class: 'search-empty', text: 'No matches for "' + query + '".' }));
      }
      UI.mount(results, groups);
      results.hidden = false;
      input.setAttribute('aria-expanded', 'true');
    }

    input.addEventListener('input', function () {
      const query = input.value.trim();
      clearTimeout(timer);
      if (query.length < 2) { hide(); return; }
      timer = setTimeout(function () {
        Store.search(query).then(function (data) { show(data, query); });
      }, 160);
    });

    input.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') { hide(); input.blur(); }
      if (event.key === 'ArrowDown' && !results.hidden) {
        const first = results.querySelector('.search-item');
        if (first) { event.preventDefault(); first.focus(); }
      }
    });

    document.addEventListener('click', function (event) {
      if (!results.hidden && !event.target.closest('.global-search')) { hide(); }
    });
  }

  /* --------------------------------------------------- account menu ---- */
  function initProfileMenu() {
    const button = document.getElementById('profile-btn');
    const menu = document.getElementById('profile-dropdown');

    function close() {
      menu.hidden = true;
      button.setAttribute('aria-expanded', 'false');
    }

    button.addEventListener('click', function () {
      const open = menu.hidden;
      menu.hidden = !open;
      button.setAttribute('aria-expanded', open ? 'true' : 'false');
      if (open) {
        const first = menu.querySelector('.dropdown-item');
        if (first) { first.focus(); }
      }
    });

    document.addEventListener('click', function (event) {
      if (!menu.hidden && !event.target.closest('.profile-menu')) { close(); }
    });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && !menu.hidden) { close(); button.focus(); }
    });
    menu.addEventListener('click', function (event) {
      if (event.target.closest('.dropdown-item')) { close(); }
    });
  }

  /* ------------------------------------------------------------- theme -- */
  function initThemeToggle() {
    const button = document.getElementById('theme-btn');
    function paint() {
      const dark = Theme.current() === 'dark';
      UI.mount(button, UI.icon(dark ? 'sun' : 'moon'));
      button.setAttribute('aria-label', dark ? 'Switch to light mode' : 'Switch to dark mode');
    }
    button.addEventListener('click', function () {
      Theme.apply(Theme.current() === 'dark' ? 'light' : 'dark');
    });
    document.addEventListener('themechange', paint);
    paint();
  }

  /* -------------------------------------------------------- identity --- */
  function setDoctor(next) {
    account = next || account;
    if (!account) { return; }
    document.querySelectorAll('[data-doctor-name]').forEach(function (node) {
      node.textContent = account.name || 'Account';
    });
    document.querySelectorAll('[data-doctor-role]').forEach(function (node) {
      node.textContent = isAdmin() ? 'Administrator' : (account.specialization || 'Doctor');
    });
    document.querySelectorAll('[data-doctor-initials]').forEach(function (node) {
      UI.clear(node);
      node.classList.toggle('avatar-photo', !!account.photo);
      if (account.photo) { node.appendChild(UI.el('img', { src: account.photo, alt: '' })); }
      else { node.textContent = UI.initials(account.name); }
    });
    if (currentRoute) {
      document.getElementById('page-subtitle').textContent = subtitleFor(currentRoute);
    }
  }

  /* -------------------------------------------------------------- init -- */
  function init(session) {
    role = session.role || 'doctor';
    account = session.account;
    document.getElementById('app').dataset.role = role;

    buildSidebar();
    initSearch();
    initProfileMenu();
    initThemeToggle();
    setDoctor(account);

    /* Doctor-only header tools. */
    const bell = document.getElementById('bell-btn');
    const profileLink = document.getElementById('menu-profile-link');
    if (isAdmin()) {
      if (bell) { bell.hidden = true; }
      if (profileLink) { profileLink.hidden = true; }
    } else {
      if (bell) {
        bell.hidden = false;
        bell.addEventListener('click', function () { go('notifications'); });
      }
      if (profileLink) { profileLink.hidden = false; }
      updateUnreadBadges();
      Store.subscribe(updateUnreadBadges);
    }

    document.getElementById('menu-toggle').addEventListener('click', function () {
      setDrawer(!document.getElementById('app').classList.contains('nav-open'));
    });
    document.getElementById('sidebar-overlay').addEventListener('click', function () { setDrawer(false); });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && document.getElementById('app').classList.contains('nav-open')) {
        setDrawer(false);
        document.getElementById('menu-toggle').focus();
      }
    });

    window.addEventListener('hashchange', render);
    render();
  }

  /* Used by the polling loop: re-render only when the user is looking at a
     page whose data may have changed, so open forms are never wiped. */
  function refreshIfRoute(routeIds) {
    if (routeIds.indexOf(currentRoute) === -1) { return; }
    if (document.querySelector('.modal-backdrop')) { return; }
    render();
  }

  return {
    init: init,
    go: go,
    render: render,
    refreshIfRoute: refreshIfRoute,
    current: function () { return currentRoute; },
    role: function () { return role; },
    isAdmin: isAdmin,
    account: function () { return account; },
    setDoctor: setDoctor,
    updateUnreadBadges: updateUnreadBadges,
    routes: routes
  };
})();
