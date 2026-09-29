/* ==========================================================================
   store.js - the only place the interface gets data from.

   Every function calls the Appointment Backend over HTTP (js/api.js). There
   is no mock data left: what the dashboard shows is what is in Firebase, and
   an appointment booked by the voice assistant appears here as soon as the page
   asks for it.

       Dashboard -> store.js -> /dashboard/* (FastAPI) -> Appointment Backend -> Firebase

   Field names are the backend's own (appointment_id, patient_id, date, time,
   status), so nothing has to be translated back when writing.

   Reuse, and its limits (api.js does the remembering; any change the doctor
   makes, and signing out, forgets all of it at once):
     - the summary: 3 seconds - long enough that the 30-second refresh and
       the page it redraws share one request, never longer
     - statistics: 60 seconds, and forgotten sooner the moment the summary
       shows the diary has changed (its `signature`)
     - profile and working week: 2 minutes / 1 minute; they change only
       when somebody edits them, and an edit made here forgets them at once
     - appointment lists, patients and messages: never kept - each opening
       asks again, one page at a time
   ========================================================================== */
window.Store = (function () {
  'use strict';

  /* Backend statuses (config.py Status). The dashboard never invents one. */
  const STATUS = {
    PENDING: 'pending',
    CONFIRMED: 'confirmed',
    RESCHEDULED: 'rescheduled',
    COMPLETED: 'completed',
    CANCELLED: 'cancelled',
    CANCELLED_BY_DOCTOR: 'cancelled_by_doctor'
  };

  const ACTIVE = [STATUS.PENDING, STATUS.CONFIRMED, STATUS.RESCHEDULED];
  const CANCELLED_ANY = [STATUS.CANCELLED, STATUS.CANCELLED_BY_DOCTOR];

  const LABELS = {
    pending: 'Pending',
    confirmed: 'Confirmed',
    rescheduled: 'Rescheduled',
    completed: 'Completed',
    cancelled: 'Cancelled',
    cancelled_by_doctor: 'Cancelled by you'
  };

  const KEEP = { summary: 3000, statistics: 60000, profile: 120000, schedule: 60000 };
  const PAGE = 20;

  const listeners = new Set();
  let doctor = null;
  let clinic = null;
  let signature = null;      // the diary as the last summary saw it

  function subscribe(fn) {
    listeners.add(fn);
    return function () { listeners.delete(fn); };
  }

  function changed() {
    listeners.forEach(function (fn) { fn(); });
  }

  /* ------------------------------------------------------------ dates --- */
  function toISO(date) {
    return date.getFullYear() + '-'
      + String(date.getMonth() + 1).padStart(2, '0') + '-'
      + String(date.getDate()).padStart(2, '0');
  }

  function todayISO() { return toISO(new Date()); }

  function isoFromOffset(days) {
    const date = new Date();
    date.setDate(date.getDate() + days);
    return toISO(date);
  }

  /* ----------------------------------------------------------- doctor --- */
  function loadProfile() {
    return Api.get('/dashboard/profile', { cache: KEEP.profile }).then(function (data) {
      doctor = data.doctor;
      clinic = data.clinic;
      return data;
    });
  }

  /* The sign-in answer already carries the doctor's record, so the
     dashboard can open without asking for it again. */
  function setDoctor(record) {
    doctor = record || null;
  }

  function getDoctor() {
    if (doctor) { return Promise.resolve(doctor); }
    return loadProfile().then(function (data) { return data.doctor; });
  }

  function getProfile() { return loadProfile(); }

  /* The record already in memory, for code that must not trigger a request
     (the polling loop reads the alert preferences from here). */
  function getCachedDoctor() { return doctor; }

  /* The only credential call a doctor can make: their own password. The
     current one is required, and neither ever reaches any other screen. */
  function changePassword(currentPassword, newPassword) {
    return Api.post('/auth/password', {
      current_password: currentPassword,
      new_password: newPassword
    });
  }

  function updateDoctorProfile(patch) {
    return Api.patch('/dashboard/profile', patch).then(function (data) {
      doctor = data.doctor;
      clinic = data.clinic;
      changed();
      return data;
    });
  }

  /* ------------------------------------------------------ appointments --- */
  /* The summary also brings the unread-message count, and a signature of
     the diary from today on: when that changes, the statistics are asked for
     again instead of waiting out their minute. */
  function getSummary() {
    return Api.get('/dashboard/summary', { cache: KEEP.summary }).then(function (data) {
      if (data.signature && signature && data.signature !== signature) {
        Api.forget('/dashboard/statistics');
      }
      signature = data.signature || signature;
      if (typeof data.unread_notifications === 'number' && data.unread_notifications !== unread) {
        unread = data.unread_notifications;
        changed();
      }
      return data;
    });
  }

  function getStats() {
    return getSummary().then(function (data) { return data.stats; });
  }

  /* Counts of this doctor's own appointments, by status, for a period.
     The backend decides whose records those are - the id comes from the
     session, never from here. */
  function getStatistics(filters) {
    const f = filters || {};
    const params = ['period=' + encodeURIComponent(f.period || 'month')];
    if (f.period === 'custom') {
      if (f.start) { params.push('start=' + encodeURIComponent(f.start)); }
      if (f.end) { params.push('end=' + encodeURIComponent(f.end)); }
    }
    return Api.get('/dashboard/statistics?' + params.join('&'), { cache: KEEP.statistics });
  }

  /* One page of appointments: { appointments, total, has_more, offset }.
     `total` is null when counting would mean reading every record. */
  function getAppointmentsPage(filters) {
    const f = filters || {};
    const params = [];
    if (f.scope && f.scope !== 'all') { params.push('scope=' + encodeURIComponent(f.scope)); }
    if (f.date) { params.push('date=' + encodeURIComponent(f.date)); }
    if (f.start) { params.push('start=' + encodeURIComponent(f.start)); }
    if (f.end) { params.push('end=' + encodeURIComponent(f.end)); }
    if (f.status) { params.push('status=' + encodeURIComponent(f.status)); }
    if (f.query) { params.push('query=' + encodeURIComponent(f.query)); }
    params.push('offset=' + (f.offset || 0));
    params.push('limit=' + (f.limit || PAGE));
    return Api.get('/dashboard/appointments?' + params.join('&'));
  }

  function getAppointments(filters) {
    return getAppointmentsPage(filters).then(function (data) { return data.appointments; });
  }

  function getTodayAppointments() {
    return getSummary().then(function (data) { return data.today; });
  }

  function getQueue() {
    return getSummary().then(function (data) { return data.queue; });
  }

  function getAppointment(id) {
    return Api.get('/dashboard/appointments/' + encodeURIComponent(id));
  }

  /* No clinic is sent: there is one, and the backend attaches it. */
  function createAppointment(data) {
    return Api.post('/dashboard/appointments', {
      patient_id: data.patient_id,
      date: data.date,
      time: data.time
    }).then(function (result) { changed(); return result; });
  }

  function cancelAppointment(id) {
    return Api.post('/dashboard/appointments/' + encodeURIComponent(id) + '/cancel')
      .then(function (result) { changed(); return result; });
  }

  function rescheduleAppointment(id, changes) {
    return Api.post('/dashboard/appointments/' + encodeURIComponent(id) + '/reschedule', {
      date: changes.date, time: changes.time
    }).then(function (result) { changed(); return result; });
  }

  function completeAppointment(id) {
    return Api.post('/dashboard/appointments/' + encodeURIComponent(id) + '/complete')
      .then(function (result) { changed(); return result; });
  }

  function getAvailability(date) {
    return Api.get('/dashboard/availability?date=' + encodeURIComponent(date));
  }

  /* --------------------------------------------------------- patients --- */
  function getPatientsPage(filters) {
    const f = filters || {};
    const params = ['offset=' + (f.offset || 0), 'limit=' + (f.limit || PAGE)];
    if (f.query) { params.push('query=' + encodeURIComponent(f.query)); }
    return Api.get('/dashboard/patients?' + params.join('&'));
  }

  /* Every patient of this doctor (up to 500), for the booking form's list. */
  function getPatients(query, limit) {
    return getPatientsPage({ query: query, limit: limit || 500 }).then(function (data) {
      return data.patients;
    });
  }

  function getPatient(id) {
    return Api.get('/dashboard/patients/' + encodeURIComponent(id));
  }

  function createPatient(data) {
    return Api.post('/dashboard/patients', { name: data.name, phone: data.phone })
      .then(function (patient) { changed(); return patient; });
  }

  /* --------------------------------------------------------- schedule --- */
  function getSchedule() {
    return Api.get('/dashboard/schedule', { cache: KEEP.schedule })
      .then(function (data) { return data.days; });
  }

  function updateSchedule(days) {
    return Api.patch('/dashboard/schedule', { days: days }).then(function (data) {
      changed();
      return data.days;
    });
  }

  function getLeave() {
    return Api.get('/dashboard/leave', { cache: KEEP.schedule })
      .then(function (data) { return data.leave; });
  }

  /* Blocking a date runs the backend cascade: appointments on it become
     cancelled_by_doctor and a notification is queued for each patient. */
  function addLeave(entry) {
    return Api.post('/dashboard/leave', {
      start_date: entry.startDate,
      end_date: entry.endDate || entry.startDate,
      reason: entry.reason
    }).then(function (result) { changed(); return result; });
  }

  function removeLeave(date) {
    return Api.del('/dashboard/leave/' + encodeURIComponent(date))
      .then(function (result) { changed(); return result; });
  }

  /* How many appointments a date range would affect, for the warning shown
     before the doctor confirms. Nothing is cancelled by this call. */
  function getAppointmentsInRange(startDate, endDate) {
    // The server reads only those dates; nothing outside them is sent.
    return getAppointments({ scope: 'upcoming', start: startDate, end: endDate, limit: 500 });
  }

  /* ---------------------------------------------------- notifications --- */
  let unread = 0;

  /* One page of messages, newest first: { notifications, unread, total, has_more }. */
  function getNotificationsPage(offset, limit) {
    return Api.get('/dashboard/notifications?offset=' + (offset || 0)
                   + '&limit=' + (limit || PAGE)).then(function (data) {
      if (data.unread !== unread) { unread = data.unread; changed(); }
      return data;
    });
  }

  function getNotifications() {
    return getNotificationsPage(0, 50).then(function (data) { return data.notifications; });
  }

  function getUnreadCount() { return unread; }

  /* The unread count comes with the summary, which the dashboard asks for
     anyway - there is no separate request for it. */
  function refreshUnreadCount() {
    return getSummary().then(function () { return unread; })
      .catch(function () { return unread; });
  }

  function markNotificationRead(id) {
    return Api.post('/dashboard/notifications/' + encodeURIComponent(id) + '/read')
      .then(function (result) {
        unread = Math.max(0, unread - 1);
        changed();
        return result;
      });
  }

  function markAllNotificationsRead() {
    return Api.post('/dashboard/notifications/read-all').then(function (result) {
      unread = 0;
      changed();
      return result;
    });
  }

  /* ----------------------------------------------------------- search --- */
  const PAGES = [
    { id: 'dashboard', label: 'Dashboard' },
    { id: 'appointments', label: 'Appointments' },
    { id: 'patients', label: 'Patients' },
    { id: 'schedule', label: 'Schedule' },
    { id: 'profile', label: 'Doctor Profile' },
    { id: 'notifications', label: 'Notifications' },
    { id: 'settings', label: 'Settings' }
  ];

  function search(query) {
    const needle = (query || '').trim();
    if (needle.length < 2) {
      return Promise.resolve({ patients: [], appointments: [], pages: [] });
    }
    const lower = needle.toLowerCase();
    return Promise.all([
      getPatients(needle, 4).catch(function () { return []; }),
      getAppointments({ query: needle, limit: 4 }).catch(function () { return []; })
    ]).then(function (results) {
      return {
        patients: results[0].slice(0, 4),
        appointments: results[1].slice(0, 4),
        pages: PAGES.filter(function (p) { return p.label.toLowerCase().indexOf(lower) !== -1; })
      };
    });
  }

  /* Cleared on sign-out so the next doctor never sees cached values. */
  function reset() {
    doctor = null;
    clinic = null;
    unread = 0;
    signature = null;
    Api.forget();
  }

  return {
    STATUS: STATUS,
    PAGE_SIZE: PAGE,
    ACTIVE_STATUSES: ACTIVE,
    CANCELLED_STATUSES: CANCELLED_ANY,
    label: function (status) { return LABELS[status] || status || 'Unknown'; },
    isActive: function (status) { return ACTIVE.indexOf(status) !== -1; },

    subscribe: subscribe,
    reset: reset,
    todayISO: todayISO,
    isoFromOffset: isoFromOffset,

    getDoctor: getDoctor,
    setDoctor: setDoctor,
    getProfile: getProfile,
    getCachedDoctor: getCachedDoctor,
    changePassword: changePassword,
    updateDoctorProfile: updateDoctorProfile,

    getSummary: getSummary,
    getStats: getStats,
    getStatistics: getStatistics,
    getAppointments: getAppointments,
    getAppointmentsPage: getAppointmentsPage,
    getTodayAppointments: getTodayAppointments,
    getQueue: getQueue,
    getAppointment: getAppointment,
    createAppointment: createAppointment,
    cancelAppointment: cancelAppointment,
    rescheduleAppointment: rescheduleAppointment,
    completeAppointment: completeAppointment,
    getAvailability: getAvailability,

    getPatients: getPatients,
    getPatientsPage: getPatientsPage,
    getPatient: getPatient,
    createPatient: createPatient,

    getSchedule: getSchedule,
    updateSchedule: updateSchedule,
    getLeave: getLeave,
    addLeave: addLeave,
    removeLeave: removeLeave,
    getAppointmentsInRange: getAppointmentsInRange,

    getNotifications: getNotifications,
    getNotificationsPage: getNotificationsPage,
    getUnreadCount: getUnreadCount,
    refreshUnreadCount: refreshUnreadCount,
    markNotificationRead: markNotificationRead,
    markAllNotificationsRead: markAllNotificationsRead,

    search: search
  };
})();
