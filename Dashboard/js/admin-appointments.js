/* ==========================================================================
   admin-appointments.js - every doctor's diary in one table.

   Read through /admin/appointments, and every action goes back through the
   Appointment Backend: cancelling here is the backend's own cancellation
   (the record is kept, the status changes, the patient is added to the
   message queue), and moving one is the same slot check a caller gets.
   ========================================================================== */
window.Pages = window.Pages || {};

window.Pages.adminAppointments = (function () {
  'use strict';

  const SCOPES = [
    { id: 'upcoming', label: 'Upcoming' },
    { id: 'today', label: 'Today' },
    { id: 'past', label: 'Past' },
    { id: 'cancelled', label: 'Cancelled' },
    { id: 'all', label: 'All' }
  ];

  let filters = { scope: 'upcoming', doctor_id: '', status: '', date: '', q: '' };
  let doctors = [];
  let shown = 20;

  /* ---------------------------------------------------------- actions --- */
  function cancel(appointment) {
    const reason = UI.el('input', {
      class: 'input', type: 'text', id: 'cancel-reason', name: 'reason',
      value: 'Cancelled by the clinic', 'data-autofocus': ''
    });
    const form = UI.el('form', { id: 'cancel-form', novalidate: true, class: 'stack' }, [
      UI.el('p', {
        class: 'muted',
        text: appointment.patient_name + '’s appointment with '
          + appointment.doctor_name + ' on ' + UI.formatDate(appointment.date)
          + ' at ' + UI.formatTime(appointment.time) + ' will be cancelled.'
      }),
      UI.el('div', { class: 'field' }, [
        UI.el('label', { for: 'cancel-reason', text: 'Reason (kept with the record)' }),
        reason,
        UI.el('p', { class: 'error-text' })
      ]),
      UI.el('p', {
        class: 'muted small',
        text: 'The appointment is kept in the history, the slot is freed, and '
          + 'a message for the patient is added to the queue.'
      })
    ]);

    const submit = UI.el('button', { class: 'btn btn-danger', type: 'submit',
                                     form: 'cancel-form' }, 'Cancel appointment');
    const dialog = UI.modal({
      size: 'sm',
      title: 'Cancel this appointment?',
      body: [form],
      footer: [
        UI.el('button', {
          class: 'btn btn-secondary', type: 'button',
          onClick: function () { dialog.close(); }
        }, 'Keep it'),
        submit
      ]
    });

    form.addEventListener('submit', function (event) {
      event.preventDefault();
      UI.setButtonLoading(submit, true);
      AdminStore.cancelAppointment(appointment.appointment_id, reason.value.trim())
        .then(function () {
          UI.setButtonLoading(submit, false);
          dialog.close();
          UI.toast('Appointment cancelled and the patient was queued for a message');
          load();
        })
        .catch(function (error) {
          UI.setButtonLoading(submit, false);
          UI.toast(error.message || 'That appointment could not be cancelled.', 'error');
        });
    });
  }

  function reschedule(appointment) {
    const dateInput = UI.el('input', {
      class: 'input', type: 'date', id: 'move-date', name: 'date',
      value: appointment.date, 'data-autofocus': ''
    });
    const slotWrap = UI.el('div', { class: 'slot-wrap' },
                           UI.el('p', { class: 'muted small',
                                        text: 'Choose a date to see free times.' }));
    let chosen = null;

    function loadSlots() {
      UI.mount(slotWrap, UI.loading('Checking free times...'));
      chosen = null;
      AdminStore.getAvailability(appointment.doctor_id, dateInput.value)
        .then(function (data) {
          const slots = data.available_slots || [];
          if (!slots.length) {
            UI.mount(slotWrap, UI.el('p', {
              class: 'muted small',
              text: 'No free times that day - the doctor may not work then, or '
                + 'the day is fully booked.'
            }));
            return;
          }
          UI.mount(slotWrap, UI.el('div', { class: 'slot-grid' },
            slots.map(function (slot) {
              const button = UI.el('button', {
                class: 'slot', type: 'button', onClick: function () {
                  chosen = slot;
                  slotWrap.querySelectorAll('.slot').forEach(function (node) {
                    node.classList.toggle('is-chosen', node === button);
                    node.setAttribute('aria-pressed', node === button ? 'true' : 'false');
                  });
                }
              }, UI.formatTime(slot));
              button.setAttribute('aria-pressed', 'false');
              return button;
            })));
        })
        .catch(function (error) {
          UI.mount(slotWrap, UI.apiError(error, loadSlots));
        });
    }

    dateInput.addEventListener('change', loadSlots);

    const form = UI.el('form', { id: 'move-form', novalidate: true, class: 'stack' }, [
      UI.el('p', {
        class: 'muted',
        text: appointment.patient_name + ' with ' + appointment.doctor_name
          + ', currently ' + UI.formatDate(appointment.date) + ' at '
          + UI.formatTime(appointment.time) + '.'
      }),
      UI.el('div', { class: 'field' }, [
        UI.el('label', { for: 'move-date', text: 'New date' }),
        dateInput,
        UI.el('p', { class: 'error-text' })
      ]),
      slotWrap
    ]);

    const submit = UI.el('button', { class: 'btn btn-primary', type: 'submit',
                                     form: 'move-form' }, 'Move appointment');
    const dialog = UI.modal({
      size: 'sm',
      title: 'Move this appointment',
      body: [form],
      footer: [
        UI.el('button', {
          class: 'btn btn-secondary', type: 'button',
          onClick: function () { dialog.close(); }
        }, 'Cancel'),
        submit
      ]
    });

    loadSlots();

    form.addEventListener('submit', function (event) {
      event.preventDefault();
      if (!chosen) {
        UI.toast('Choose a new time first.', 'error');
        return;
      }
      UI.setButtonLoading(submit, true);
      AdminStore.rescheduleAppointment(appointment.appointment_id,
                                       { date: dateInput.value, time: chosen })
        .then(function () {
          UI.setButtonLoading(submit, false);
          dialog.close();
          UI.toast('Appointment moved to ' + UI.formatDateShort(dateInput.value)
                   + ' at ' + UI.formatTime(chosen));
          load();
        })
        .catch(function (error) {
          UI.setButtonLoading(submit, false);
          UI.toast(error.message || 'That appointment could not be moved.', 'error');
        });
    });
  }

  function complete(appointment) {
    UI.confirm({
      title: 'Mark as completed?',
      message: appointment.patient_name + '’s appointment with '
        + appointment.doctor_name + ' will be recorded as completed.',
      confirmLabel: 'Mark completed'
    }).then(function (confirmed) {
      if (!confirmed) { return; }
      AdminStore.completeAppointment(appointment.appointment_id)
        .then(function () { UI.toast('Marked as completed'); load(); })
        .catch(function (error) {
          UI.toast(error.message || 'That could not be recorded.', 'error');
        });
    });
  }

  /* ------------------------------------------------------------ table --- */
  function actions(appointment) {
    const live = Store.isActive(appointment.status);
    const buttons = [];
    if (live) {
      buttons.push(UI.el('button', {
        class: 'btn btn-ghost btn-icon btn-sm', type: 'button',
        title: 'Move this appointment', 'aria-label': 'Move this appointment',
        onClick: function () { reschedule(appointment); }
      }, UI.icon('calendar', 'icon-sm')));
      buttons.push(UI.el('button', {
        class: 'btn btn-ghost btn-icon btn-sm', type: 'button',
        title: 'Mark completed', 'aria-label': 'Mark completed',
        onClick: function () { complete(appointment); }
      }, UI.icon('check-circle', 'icon-sm')));
      buttons.push(UI.el('button', {
        class: 'btn btn-ghost btn-icon btn-sm is-danger', type: 'button',
        title: 'Cancel this appointment', 'aria-label': 'Cancel this appointment',
        onClick: function () { cancel(appointment); }
      }, UI.icon('ban', 'icon-sm')));
    }
    return UI.el('td', { class: 'cell-right', 'data-label': 'Actions' },
                 buttons.length
                   ? UI.el('div', { class: 'row-actions' }, buttons)
                   : UI.el('span', { class: 'muted small', text: '—' }));
  }

  function table(rows) {
    return UI.el('div', { class: 'table-wrap' }, UI.el('table', { class: 'data' }, [
      UI.el('thead', {}, UI.el('tr', {}, [
        UI.el('th', { text: 'When' }),
        UI.el('th', { text: 'Patient' }),
        UI.el('th', { text: 'Doctor' }),
        UI.el('th', { text: 'Status' }),
        UI.el('th', { class: 'cell-right', text: 'Actions' })
      ])),
      UI.el('tbody', {}, rows.map(function (appointment) {
        return UI.el('tr', {}, [
          UI.el('td', { 'data-label': 'When' }, [
            UI.el('strong', { text: UI.formatDateShort(appointment.date) }),
            UI.el('span', { class: 'muted small',
                            text: ' ' + UI.formatTime(appointment.time) })
          ]),
          UI.el('td', { 'data-label': 'Patient' }, [
            UI.el('div', { text: appointment.patient_name }),
            UI.el('span', { class: 'muted small',
                            text: appointment.patient_phone || '' })
          ]),
          UI.el('td', { 'data-label': 'Doctor' }, [
            UI.el('div', { text: appointment.doctor_name || appointment.doctor_id }),
            UI.el('span', { class: 'muted small', text: appointment.appointment_id })
          ]),
          UI.el('td', { 'data-label': 'Status' }, UI.badge(appointment.status)),
          actions(appointment)
        ]);
      }))
    ]));
  }

  /* ---------------------------------------------------------- toolbar --- */
  function toolbar() {
    const search = UI.el('input', {
      class: 'input', type: 'search', id: 'appointment-search',
      placeholder: 'Search patient, phone, doctor or ID',
      'aria-label': 'Search appointments', value: filters.q, autocomplete: 'off'
    });
    let timer = null;
    search.addEventListener('input', function () {
      clearTimeout(timer);
      timer = setTimeout(function () {
        filters.q = search.value.trim();
        shown = 20;
        load({ keepFocus: true });
      }, 220);
    });

    const doctorSelect = UI.el('select', {
      class: 'input', id: 'appointment-doctor', 'aria-label': 'Filter by doctor'
    }, [UI.el('option', { value: '', text: 'All doctors' })].concat(
      doctors.map(function (doctor) {
        return UI.el('option', { value: doctor.doctor_id,
                                 text: doctor.name || doctor.doctor_id,
                                 selected: filters.doctor_id === doctor.doctor_id });
      })));
    doctorSelect.addEventListener('change', function () {
      filters.doctor_id = doctorSelect.value;
      shown = 20;
      load();
    });

    const dateInput = UI.el('input', {
      class: 'input', type: 'date', id: 'appointment-date',
      'aria-label': 'Filter by date', value: filters.date
    });
    dateInput.addEventListener('change', function () {
      filters.date = dateInput.value;
      shown = 20;
      load();
    });

    const scopes = UI.el('div', { class: 'segmented', role: 'group',
                                  'aria-label': 'Which appointments' },
      SCOPES.map(function (scope) {
        return UI.el('button', {
          type: 'button',
          'aria-pressed': filters.scope === scope.id ? 'true' : 'false',
          onClick: function () {
            filters.scope = scope.id;
            shown = 20;
            load();
          }
        }, scope.label);
      }));

    return UI.el('div', { class: 'stack-12' }, [
      scopes,
      UI.el('div', { class: 'toolbar' }, [
        UI.el('div', { class: 'search grow' }, [UI.icon('search'), search]),
        doctorSelect,
        dateInput,
        filters.date
          ? UI.el('button', {
            class: 'btn btn-ghost btn-sm', type: 'button',
            onClick: function () { filters.date = ''; load(); }
          }, 'Clear date')
          : null
      ])
    ]);
  }

  /* ------------------------------------------------------------- page --- */
  function render(target) {
    UI.mount(target, [
      UI.el('div', { class: 'page-head' }, UI.el('div', {}, [
        UI.el('h2', { text: 'Appointments' }),
        UI.el('p', { text: 'Every doctor’s diary, including the ones booked by the voice assistant.' })
      ])),
      UI.el('section', { class: 'card' }, [
        UI.el('div', { class: 'card-head' }, UI.el('div', { id: 'appointments-toolbar' })),
        UI.el('div', { class: 'card-body card-body-flush', id: 'appointments-body' },
              UI.skeleton(6))
      ])
    ]);
    load();
  }

  function load(options) {
    const config = options || {};
    const body = document.getElementById('appointments-body');
    if (!body) { return; }

    AdminStore.getAllAppointments({
      scope: filters.scope, doctor_id: filters.doctor_id,
      date: filters.date, status: filters.status, q: filters.q
    }).then(function (data) {
      doctors = data.doctors || [];
      const slot = document.getElementById('appointments-toolbar');
      const focused = document.activeElement
        && document.activeElement.id === 'appointment-search';
      const caret = focused ? document.activeElement.selectionStart : null;
      UI.mount(slot, toolbar());
      if (focused || config.keepFocus) {
        const search = document.getElementById('appointment-search');
        if (search) {
          search.focus();
          if (caret !== null) { search.setSelectionRange(caret, caret); }
        }
      }

      const rows = data.appointments;
      if (!rows.length) {
        UI.mount(body, UI.empty({
          icon: 'calendar',
          title: 'Nothing matches that',
          message: 'Try another filter, or a different date.'
        }));
        return;
      }

      const visible = rows.slice(0, shown);
      const parts = [table(visible)];
      if (rows.length > visible.length) {
        parts.push(UI.el('div', { class: 'card-body row-between wrap' }, [
          UI.el('span', {
            class: 'muted small',
            text: 'Showing ' + visible.length + ' of ' + rows.length + ' appointments'
          }),
          UI.el('button', {
            class: 'btn btn-secondary btn-sm', type: 'button',
            onClick: function () { shown += 20; load(); }
          }, 'Load more')
        ]));
      }
      UI.mount(body, parts);
    }).catch(function (error) {
      UI.mount(body, UI.apiError(error, function () { load(); }));
    });
  }

  return { render: render };
})();
