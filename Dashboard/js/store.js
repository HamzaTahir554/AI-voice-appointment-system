/* ==========================================================================
   store.js - the only place the interface gets data from.

   Every function calls the Appointment Backend over HTTP (js/api.js). There
   is no mock data left: what the dashboard shows is what is in Firebase, and
   an appointment booked by the voice assistant appears here as soon as the page
   asks for it.

       Dashboard -> store.js -> /dashboard/* (FastAPI) -> Appointment Backend -> Firebase

   Field names are the backend's own (appointment_id, patient_id, date, time,
   status), so nothing has to be translated back when writing.
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

  const listeners = new Set();
  let doctor = null;
  let clinic = null;

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
    return Api.get('/dashboard/profile').then(function (data) {
      doctor = data.doctor;
      clinic = data.clinic;
      return data;
    });
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
  function getSummary() {
    return Api.get('/dashboard/summary');
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
    return Api.get('/dashboard/statistics?' + params.join('&'));
  }

  function getAppointments(filters) {
    const f = filters || {};
    const params = [];
    if (f.scope && f.scope !== 'all') { params.push('scope=' + encodeURIComponent(f.scope)); }
    if (f.date) { params.push('date=' + encodeURIComponent(f.date)); }
    if (f.status) { params.push('status=' + encodeURIComponent(f.status)); }
    if (f.query) { params.push('query=' + encodeURIComponent(f.query)); }
    const suffix = params.length ? '?' + params.join('&') : '';
    return Api.get('/dashboard/appointments' + suffix).then(function (data) {
      return data.appointments;
    });
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
  function getPatients(query) {
    const suffix = query ? '?query=' + encodeURIComponent(query) : '';
    return Api.get('/dashboard/patients' + suffix).then(function (data) {
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
    return Api.get('/dashboard/schedule').then(function (data) { return data.days; });
  }

  function updateSchedule(days) {
    return Api.patch('/dashboard/schedule', { days: days }).then(function (data) {
      changed();
      return data.days;
    });
  }

  function getLeave() {
    return Api.get('/dashboard/leave').then(function (data) { return data.leave; });
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
    return getAppointments({ scope: 'upcoming' }).then(function (rows) {
      return rows.filter(function (a) {
        return a.date >= startDate && a.date <= endDate;
      });
    });
  }

  /* ---------------------------------------------------- notifications --- */
  let unread = 0;

  function getNotifications() {
    return Api.get('/dashboard/notifications').then(function (data) {
      unread = data.unread;
      return data.notifications;
    });
  }

  function getUnreadCount() { return unread; }

  function refreshUnreadCount() {
    return Api.get('/dashboard/notifications').then(function (data) {
      const before = unread;
      unread = data.unread;
      if (before !== unread) { changed(); }
      return unread;
    }).catch(function () { return unread; });
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
      getPatients(needle).catch(function () { return []; }),
      getAppointments({ query: needle }).catch(function () { return []; })
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
  }

  return {
    STATUS: STATUS,
    ACTIVE_STATUSES: ACTIVE,
    CANCELLED_STATUSES: CANCELLED_ANY,
    label: function (status) { return LABELS[status] || status || 'Unknown'; },
    isActive: function (status) { return ACTIVE.indexOf(status) !== -1; },

    subscribe: subscribe,
    reset: reset,
    todayISO: todayISO,
    isoFromOffset: isoFromOffset,

    getDoctor: getDoctor,
    getProfile: getProfile,
    getCachedDoctor: getCachedDoctor,
    changePassword: changePassword,
    updateDoctorProfile: updateDoctorProfile,

    getSummary: getSummary,
    getStats: getStats,
    getStatistics: getStatistics,
    getAppointments: getAppointments,
    getTodayAppointments: getTodayAppointments,
    getQueue: getQueue,
    getAppointment: getAppointment,
    createAppointment: createAppointment,
    cancelAppointment: cancelAppointment,
    rescheduleAppointment: rescheduleAppointment,
    completeAppointment: completeAppointment,
    getAvailability: getAvailability,

    getPatients: getPatients,
    getPatient: getPatient,
    createPatient: createPatient,

    getSchedule: getSchedule,
    updateSchedule: updateSchedule,
    getLeave: getLeave,
    addLeave: addLeave,
    removeLeave: removeLeave,
    getAppointmentsInRange: getAppointmentsInRange,

    getNotifications: getNotifications,
    getUnreadCount: getUnreadCount,
    refreshUnreadCount: refreshUnreadCount,
    markNotificationRead: markNotificationRead,
    markAllNotificationsRead: markAllNotificationsRead,

    search: search
  };
})();
