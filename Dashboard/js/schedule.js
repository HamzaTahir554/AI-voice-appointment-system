/* ==========================================================================
   schedule.js - weekly availability and leave.

   The weekly pattern is the `schedules` collection: the same rows the
   booking engine reads when it decides which slots a caller may have.
   Blocking a date writes to `doctor_unavailability` AND runs the backend
   cascade: every live appointment that day becomes cancelled_by_doctor and a
   notification is queued for each patient. Sending those messages (SMS or a
   call) is not connected yet, and the interface says so.
   ========================================================================== */
window.Pages = window.Pages || {};

window.Pages.schedule = (function () {
  'use strict';

  let draft = [];

  function timeInput(value, label, onChange) {
    const input = UI.el('input', { class: 'input', type: 'time', value: value || '', 'aria-label': label });
    input.addEventListener('change', function () { onChange(input.value); });
    return input;
  }

  function dayRow(day, onDirty) {
    const sessionsWrap = UI.el('div', { class: 'day-sessions' });

    function paintSessions() {
      UI.clear(sessionsWrap);
      if (!day.available) {
        sessionsWrap.appendChild(UI.el('span', { class: 'day-closed', text: 'Clinic closed' }));
        return;
      }
      day.sessions.forEach(function (session, index) {
        sessionsWrap.appendChild(UI.el('div', { class: 'session-times' }, [
          timeInput(session.start, day.day + ' session ' + (index + 1) + ' start time', function (value) {
            session.start = value; onDirty();
          }),
          UI.el('span', { class: 'session-sep', text: 'to' }),
          timeInput(session.end, day.day + ' session ' + (index + 1) + ' end time', function (value) {
            session.end = value; onDirty();
          }),
          UI.el('button', {
            class: 'btn btn-ghost btn-icon', type: 'button',
            'aria-label': 'Remove ' + day.day + ' session ' + (index + 1),
            onClick: function () { day.sessions.splice(index, 1); onDirty(); paintSessions(); }
          }, UI.icon('trash', 'icon-sm'))
        ]));
      });
      if (day.sessions.length < 2) {
        sessionsWrap.appendChild(UI.el('button', {
          class: 'btn btn-ghost btn-sm', type: 'button',
          onClick: function () {
            day.sessions.push({ start: '16:00', end: '20:00' });
            onDirty();
            paintSessions();
          }
        }, [UI.icon('plus', 'icon-sm'), 'Add session']));
      }
    }

    const checkbox = UI.el('input', {
      type: 'checkbox', id: 'avail-' + day.day, checked: day.available,
      'aria-label': day.day + ': clinic open'
    });
    checkbox.addEventListener('change', function () {
      day.available = checkbox.checked;
      if (day.available && !day.sessions.length) {
        day.sessions.push({ start: '16:00', end: '20:00' });
      }
      onDirty();
      paintSessions();
    });

    paintSessions();

    return UI.el('div', { class: 'day-row' }, [
      UI.el('div', { class: 'day-name' }, [
        UI.el('label', { class: 'switch switch-inline', for: 'avail-' + day.day }, [
          checkbox, UI.el('span', { class: 'switch-track' })
        ]),
        UI.el('span', { text: day.day })
      ]),
      sessionsWrap
    ]);
  }

  function render(container) {
    const scheduleBody = UI.el('div', { class: 'card-body card-body-flush' }, UI.skeleton(7));
    const leaveBody = UI.el('div', { class: 'card-body card-body-flush' }, UI.skeleton(2));

    const saveButton = UI.el('button', {
      class: 'btn btn-primary', type: 'button', disabled: true,
      onClick: function () { save(saveButton, scheduleBody); }
    }, 'Save schedule');

    UI.mount(container, [
      UI.el('div', { class: 'page-head' }, [
        UI.el('div', {}, [
          UI.el('h2', { text: 'My Schedule' }),
          UI.el('p', { text: 'The working hours the booking system offers to callers.' })
        ])
      ]),
      UI.el('section', { class: 'card', 'aria-labelledby': 'availability-heading' }, [
        UI.el('div', { class: 'card-head' }, [
          UI.el('div', {}, [
            UI.el('h3', { id: 'availability-heading', text: 'Weekly availability' }),
            UI.el('p', { class: 'muted small', text: 'Turning a day off stops new bookings on that weekday.' })
          ]),
          saveButton
        ]),
        scheduleBody
      ]),
      UI.el('section', { class: 'card', 'aria-labelledby': 'leave-heading' }, [
        UI.el('div', { class: 'card-head' }, [
          UI.el('div', {}, [
            UI.el('h3', { id: 'leave-heading', text: 'Leave and unavailable dates' }),
            UI.el('p', {
              class: 'muted small',
              text: 'Blocking a date cancels the appointments on it and queues a message for each patient.'
            })
          ]),
          UI.el('button', {
            class: 'btn btn-primary', type: 'button',
            onClick: function () { openAddLeave(function () { loadLeave(leaveBody); }); }
          }, [UI.icon('plus', 'icon-sm'), 'Add Leave'])
        ]),
        leaveBody
      ])
    ]);

    loadSchedule(scheduleBody, saveButton);
    loadLeave(leaveBody);
  }

  function loadSchedule(scheduleBody, saveButton) {
    UI.mount(scheduleBody, UI.skeleton(7));
    Store.getSchedule().then(function (days) {
      draft = days.map(function (day) {
        return {
          day: day.day,
          available: day.available,
          sessions: (day.sessions || []).map(function (s) { return { start: s.start, end: s.end }; })
        };
      });
      const markDirty = function () { saveButton.disabled = false; };
      UI.mount(scheduleBody, draft.map(function (day) { return dayRow(day, markDirty); }));
    }).catch(function (error) {
      UI.mount(scheduleBody, UI.apiError(error, function () { loadSchedule(scheduleBody, saveButton); }));
    });
  }

  function save(saveButton, scheduleBody) {
    const broken = draft.filter(function (day) {
      return day.available && (!day.sessions.length || day.sessions.some(function (s) {
        return !s.start || !s.end || s.end <= s.start;
      }));
    });
    if (broken.length) {
      UI.toast('Check ' + broken[0].day + ': every open day needs a session, and the end time '
        + 'must be after the start time.', 'error');
      return;
    }
    UI.setButtonLoading(saveButton, true);
    Store.updateSchedule(draft).then(function () {
      UI.setButtonLoading(saveButton, false);
      saveButton.disabled = true;
      UI.toast('Schedule saved. New bookings will use these hours.');
      loadSchedule(scheduleBody, saveButton);
    }).catch(function (error) {
      UI.setButtonLoading(saveButton, false);
      UI.toast(error.message || 'The schedule could not be saved.', 'error');
    });
  }

  /* -------------------------------------------------------- leave ----- */
  /* Consecutive blocked dates with the same reason read better as a range. */
  function group(entries) {
    const groups = [];
    entries.forEach(function (entry) {
      const previous = groups[groups.length - 1];
      const dayAfter = previous
        ? new Date(new Date(previous.end + 'T00:00:00').getTime() + 86400000)
        : null;
      const followsOn = previous && previous.reason === entry.reason
        && dayAfter && entry.date === dayAfter.toISOString().slice(0, 10);
      if (followsOn) {
        previous.end = entry.date;
        previous.dates.push(entry.date);
      } else {
        groups.push({ start: entry.date, end: entry.date, reason: entry.reason, dates: [entry.date] });
      }
    });
    return groups;
  }

  function loadLeave(leaveBody) {
    UI.mount(leaveBody, UI.skeleton(2));
    Store.getLeave().then(function (entries) {
      if (!entries.length) {
        UI.mount(leaveBody, UI.empty({
          icon: 'clock',
          title: 'No upcoming leave scheduled',
          message: 'Block a date when the clinic will be closed.'
        }));
        return;
      }
      UI.mount(leaveBody, group(entries).map(function (entry) {
        const single = entry.start === entry.end;
        return UI.el('div', { class: 'leave-item' }, [
          UI.el('span', { class: 'kpi-icon' }, UI.icon('calendar')),
          UI.el('div', { class: 'grow' }, [
            UI.el('div', {
              class: 'leave-dates',
              text: single ? UI.formatDate(entry.start)
                : UI.formatDateShort(entry.start) + ' - ' + UI.formatDate(entry.end)
            }),
            UI.el('div', { class: 'leave-reason', text: entry.reason })
          ]),
          UI.el('button', {
            class: 'btn btn-ghost btn-sm', type: 'button',
            'aria-label': 'Re-open ' + UI.formatDate(entry.start),
            onClick: function () { removeLeave(entry, leaveBody); }
          }, [UI.icon('trash', 'icon-sm'), 'Re-open'])
        ]);
      }));
    }).catch(function (error) {
      UI.mount(leaveBody, UI.apiError(error, function () { loadLeave(leaveBody); }));
    });
  }

  function removeLeave(entry, leaveBody) {
    UI.confirm({
      title: 'Re-open these dates?',
      message: 'The clinic will accept bookings again from ' + UI.formatDate(entry.start)
        + '. Appointments that were already cancelled stay cancelled, because those patients were told.',
      confirmLabel: 'Re-open',
      danger: false
    }).then(function (confirmed) {
      if (!confirmed) { return; }
      Promise.all(entry.dates.map(function (date) { return Store.removeLeave(date); }))
        .then(function () {
          UI.toast('Dates re-opened for booking');
          loadLeave(leaveBody);
        })
        .catch(function (error) { UI.toast(error.message || 'Could not re-open the date.', 'error'); });
    });
  }

  function openAddLeave(onSaved) {
    const affectedNote = UI.el('p', { class: 'hint' });

    const form = UI.el('form', { class: 'stack', novalidate: true, id: 'leave-form' }, [
      UI.el('div', { class: 'form-grid' }, [
        UI.el('div', { class: 'field' }, [
          UI.el('label', { for: 'lv-start', text: 'First day' }),
          UI.el('input', {
            class: 'input', type: 'date', id: 'lv-start', name: 'startDate',
            min: Store.todayISO(), value: Store.isoFromOffset(1), 'data-autofocus': ''
          }),
          UI.el('p', { class: 'error-text' })
        ]),
        UI.el('div', { class: 'field' }, [
          UI.el('label', { for: 'lv-end', text: 'Last day' }),
          UI.el('input', {
            class: 'input', type: 'date', id: 'lv-end', name: 'endDate',
            min: Store.todayISO(), value: Store.isoFromOffset(1)
          }),
          UI.el('p', { class: 'error-text' })
        ])
      ]),
      UI.el('div', { class: 'field' }, [
        UI.el('label', { for: 'lv-reason', text: 'Reason' }),
        UI.el('input', {
          class: 'input', type: 'text', id: 'lv-reason', name: 'reason',
          placeholder: 'Medical leave'
        }),
        UI.el('p', { class: 'error-text' })
      ]),
      affectedNote
    ]);

    function updateAffected() {
      const start = form.elements.startDate.value;
      const end = form.elements.endDate.value || start;
      if (!start || end < start) { affectedNote.textContent = ''; return; }
      affectedNote.textContent = 'Checking affected appointments...';
      Store.getAppointmentsInRange(start, end).then(function (rows) {
        affectedNote.textContent = rows.length
          ? rows.length + ' appointment' + (rows.length === 1 ? '' : 's')
            + ' will be cancelled and a message queued for each patient. '
            + 'Message delivery (SMS or call) is not connected yet, so tell them yourself if it is urgent.'
          : 'No appointments are booked in this period.';
      }).catch(function () {
        affectedNote.textContent = 'Could not check the affected appointments.';
      });
    }
    form.addEventListener('change', updateAffected);
    updateAffected();

    const save = UI.el('button', { class: 'btn btn-danger', type: 'submit', form: 'leave-form' },
      'Block dates');

    const instance = UI.modal({
      title: 'Add leave',
      subtitle: 'Blocks booking and cancels what is already booked.',
      body: [form],
      footer: [
        UI.el('button', {
          class: 'btn btn-secondary', type: 'button',
          onClick: function () { instance.close(); }
        }, 'Cancel'),
        save
      ]
    });

    form.addEventListener('submit', function (event) {
      event.preventDefault();
      const valid = UI.validate(form, {
        startDate: [
          UI.rule.required('First day is required.'),
          UI.rule.date('Enter a valid date.'),
          { test: function (value) { return value >= Store.todayISO(); },
            message: 'Leave cannot start in the past.' }
        ],
        endDate: [
          UI.rule.required('Last day is required.'),
          UI.rule.date('Enter a valid date.'),
          { test: function (value, f) { return value >= f.elements.startDate.value; },
            message: 'The last day must be on or after the first day.' }
        ],
        reason: [UI.rule.required('Reason is required.')]
      });
      if (!valid) { return; }

      UI.setButtonLoading(save, true);
      Store.addLeave({
        startDate: form.elements.startDate.value,
        endDate: form.elements.endDate.value,
        reason: form.elements.reason.value.trim()
      }).then(function (result) {
        UI.setButtonLoading(save, false);
        instance.close();
        const cancelled = result.cancelled_appointments || 0;
        UI.toast(cancelled
          ? 'Dates blocked. ' + cancelled + ' appointment' + (cancelled === 1 ? '' : 's')
            + ' cancelled and ' + (result.notifications_queued || 0) + ' message(s) queued.'
          : 'Dates blocked. No appointments were affected.');
        onSaved();
      }).catch(function (error) {
        UI.setButtonLoading(save, false);
        UI.toast(error.message || 'Could not block those dates.', 'error');
      });
    });
  }

  return { render: render };
})();
