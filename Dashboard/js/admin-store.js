/* ==========================================================================
   admin-store.js - the only place the administration screens get data from.

   One function per backend call, exactly as store.js does for the doctor
   side. Every call goes to /admin/* on the Appointment Backend, which is the
   only thing that touches Firebase.

       Admin screens -> admin-store.js -> /admin/* (FastAPI) -> Firebase

   Reuse (api.js remembers; any change made here, and signing out, forgets
   everything at once): the overview 3 seconds, so its 30-second refresh
   shares one request with the page it redraws; statistics 25 seconds; the
   doctor register 20 seconds; the clinic a minute. Appointment lists are
   never kept - they are read a page at a time.
   ========================================================================== */
window.AdminStore = (function () {
  'use strict';

  const KEEP = { summary: 3000, statistics: 25000, doctors: 20000, clinic: 60000 };

  function query(params) {
    const parts = [];
    Object.keys(params || {}).forEach(function (key) {
      const value = params[key];
      if (value === undefined || value === null || value === '') { return; }
      parts.push(encodeURIComponent(key) + '=' + encodeURIComponent(value));
    });
    return parts.length ? '?' + parts.join('&') : '';
  }

  /* ---------------------------------------------------------- overview -- */
  function getSummary() {
    return Api.get('/admin/summary', { cache: KEEP.summary });
  }

  /* ----------------------------------------------------------- doctors -- */
  function getDoctors(filters) {
    return Api.get('/admin/doctors' + query(filters), { cache: KEEP.doctors });
  }

  function getDoctor(doctorId) {
    return Api.get('/admin/doctors/' + encodeURIComponent(doctorId));
  }

  function createDoctor(payload) {
    return Api.post('/admin/doctors', payload);
  }

  function updateDoctor(doctorId, payload) {
    return Api.patch('/admin/doctors/' + encodeURIComponent(doctorId), payload);
  }

  function setActive(doctorId, active) {
    return Api.post('/admin/doctors/' + encodeURIComponent(doctorId) + '/status',
                    { active: !!active });
  }

  function removeDoctor(doctorId) {
    return Api.del('/admin/doctors/' + encodeURIComponent(doctorId));
  }

  function restoreDoctor(doctorId) {
    return Api.post('/admin/doctors/' + encodeURIComponent(doctorId) + '/restore');
  }

  /* ------------------------------------------------------- credentials -- */
  function getCredentials(doctorId) {
    return Api.get('/admin/doctors/' + encodeURIComponent(doctorId) + '/credentials');
  }

  /* Only ever sends what the administrator typed; the server hashes it and
     nothing about a password ever comes back. */
  function setCredentials(doctorId, fields) {
    return Api.patch('/admin/doctors/' + encodeURIComponent(doctorId) + '/credentials',
                     fields);
  }

  /* -------------------------------------------------------- statistics -- */
  /* The clinic's counts and the same breakdown per doctor. `doctor_id`
     narrows the headline figures to one doctor. */
  function getStatistics(filters) {
    const f = filters || {};
    const params = { period: f.period || 'month', doctor_id: f.doctorId || '' };
    if (f.period === 'custom') {
      params.start = f.start || '';
      params.end = f.end || '';
    }
    return Api.get('/admin/statistics' + query(params), { cache: KEEP.statistics });
  }

  /* ------------------------------------------------------------- clinic -- */
  /* One clinic, one record. Its name and address are read from here by every
     screen, and by the assistant when it talks to a caller. */
  function getClinic() {
    return Api.get('/admin/clinic', { cache: KEEP.clinic });
  }

  function updateClinic(payload) {
    return Api.patch('/admin/clinic', payload);
  }

  /* For a database left over from an earlier version with more than one
     clinic record: move every doctor onto the one clinic. */
  function consolidateClinic() {
    return Api.post('/admin/clinic/consolidate');
  }

  /* ------------------------------------------- every doctor's appointments -- */
  function getAllAppointments(filters) {
    return Api.get('/admin/appointments' + query(filters));
  }

  function cancelAppointment(appointmentId, reason) {
    return Api.post('/admin/appointments/' + encodeURIComponent(appointmentId)
                    + '/cancel', { reason: reason || 'Cancelled by the clinic' });
  }

  function rescheduleAppointment(appointmentId, changes) {
    return Api.post('/admin/appointments/' + encodeURIComponent(appointmentId)
                    + '/reschedule', { date: changes.date, time: changes.time });
  }

  function completeAppointment(appointmentId) {
    return Api.post('/admin/appointments/' + encodeURIComponent(appointmentId)
                    + '/complete');
  }

  /* Free slots the booking engine would accept, for moving an appointment. */
  function getAvailability(doctorId, date) {
    return Api.get('/admin/doctors/' + encodeURIComponent(doctorId)
                   + '/availability?date=' + encodeURIComponent(date));
  }

  /* ------------------------------------------- one doctor's working week -- */
  function getSchedule(doctorId) {
    return Api.get('/admin/doctors/' + encodeURIComponent(doctorId) + '/schedule')
      .then(function (data) { return data.days; });
  }

  function setSchedule(doctorId, days, slotDuration) {
    return Api.patch('/admin/doctors/' + encodeURIComponent(doctorId) + '/schedule',
                     { days: days, slot_duration: slotDuration })
      .then(function (data) { return data.days; });
  }

  function getLeave(doctorId) {
    return Api.get('/admin/doctors/' + encodeURIComponent(doctorId) + '/leave')
      .then(function (data) { return data.leave; });
  }

  /* What blocking these dates would cancel - read only, nothing is written. */
  function previewLeave(doctorId, entry) {
    return Api.get('/admin/doctors/' + encodeURIComponent(doctorId) + '/leave/preview'
                   + query({ start_date: entry.startDate,
                             end_date: entry.endDate || entry.startDate }));
  }

  function addLeave(doctorId, entry) {
    return Api.post('/admin/doctors/' + encodeURIComponent(doctorId) + '/leave', {
      start_date: entry.startDate,
      end_date: entry.endDate || entry.startDate,
      reason: entry.reason || 'Doctor unavailable'
    });
  }

  function removeLeave(doctorId, date) {
    return Api.del('/admin/doctors/' + encodeURIComponent(doctorId)
                   + '/leave/' + encodeURIComponent(date));
  }

  /* ------------------------------------------------- one doctor's diary -- */
  function getAppointments(doctorId, filters) {
    return Api.get('/admin/doctors/' + encodeURIComponent(doctorId)
                   + '/appointments' + query(filters))
      .then(function (data) { return data.appointments; });
  }

  return {
    getSummary: getSummary,
    getStatistics: getStatistics,
    getDoctors: getDoctors,
    getCredentials: getCredentials,
    setCredentials: setCredentials,
    getClinic: getClinic,
    updateClinic: updateClinic,
    consolidateClinic: consolidateClinic,
    getAllAppointments: getAllAppointments,
    cancelAppointment: cancelAppointment,
    rescheduleAppointment: rescheduleAppointment,
    completeAppointment: completeAppointment,
    getAvailability: getAvailability,
    previewLeave: previewLeave,
    getDoctor: getDoctor,
    createDoctor: createDoctor,
    updateDoctor: updateDoctor,
    setActive: setActive,
    removeDoctor: removeDoctor,
    restoreDoctor: restoreDoctor,
    getSchedule: getSchedule,
    setSchedule: setSchedule,
    getLeave: getLeave,
    addLeave: addLeave,
    removeLeave: removeLeave,
    getAppointments: getAppointments
  };
})();
