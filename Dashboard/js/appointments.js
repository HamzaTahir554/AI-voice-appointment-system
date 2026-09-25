/* ==========================================================================
   appointments.js - the Appointments page and every appointment action.

   Each action is one call to the Appointment Backend, which applies the same
   rules the voice assistant obeys: cancelling only changes the status,
   rescheduling checks the slot, and a completed appointment cannot be
   completed twice. Errors come back from the backend and are shown as they
   are written, never invented here.
   ========================================================================== */
window.Pages = window.Pages || {};

window.AppointmentActions = (function () {
  'use strict';

  function refreshCurrentPage() {
    Nav.render();
  }

  function report(error) {
    UI.toast(error && error.message ? error.message : 'The action failed.', 'error');
  }

  /* ------------------------------------------------------ details ------ */
  function openDetails(id, onChange) {
    Store.getAppointment(id).then(function (appointment) {
      const rows = [
        ['Patient', appointment.patient_name],
        ['Phone', appointment.patient_phone || 'Not recorded'],
        ['Appointment ID', appointment.appointment_id],
        ['Date', UI.formatDate(appointment.date)],
        ['Time', UI.formatTime(appointment.time)],
        ['Clinic', appointment.clinic_name || 'Not recorded'],
        ['Patient ID', appointment.patient_id]
      ];
      if (appointment.cancellation_reason) {
        rows.push(['Cancellation reason', appointment.cancellation_reason]);
      }

      const active = Store.isActive(appointment.status);
      const footer = [];

      if (active) {
        footer.push(UI.el('button', {
          class: 'btn btn-secondary', type: 'button',
          onClick: function () { instance.close(); complete(appointment.appointment_id, onChange); }
        }, [UI.icon('check', 'icon-sm'), 'Mark Completed']));
        footer.push(UI.el('button', {
          class: 'btn btn-secondary', type: 'button',
          onClick: function () { instance.close(); openReschedule(appointment.appointment_id, onChange); }
        }, [UI.icon('refresh', 'icon-sm'), 'Reschedule']));
        footer.push(UI.el('button', {
          class: 'btn btn-danger', type: 'button',
          onClick: function () { instance.close(); confirmCancel(appointment.appointment_id, onChange); }
        }, [UI.icon('ban', 'icon-sm'), 'Cancel']));
      }
      footer.push(UI.el('button', {
        class: 'btn btn-primary', type: 'button', 'data-autofocus': '',
        onClick: function () { instance.close(); }
      }, 'Close'));

      const instance = UI.modal({
        title: appointment.patient_name,
        subtitle: UI.relativeDay(appointment.date) + ' at ' + UI.formatTime(appointment.time),
        body: [
          UI.el('dl', { class: 'detail-list' }, rows.map(function (row) {
            return UI.el('div', {}, [UI.el('dt', { text: row[0] }), UI.el('dd', { text: row[1] })]);
          }).concat([
            UI.el('div', {}, [UI.el('dt', { text: 'Status' }), UI.el('dd', {}, UI.badge(appointment.status))])
          ]))
        ],
        footer: footer
      });
    }).catch(report);
  }

  /* --------------------------------------------------- reschedule ------ */
  function openReschedule(id, onChange) {
    Store.getAppointment(id).then(function (appointment) {
      const slotHint = UI.el('p', { class: 'hint' });
      const timeInput = UI.el('input', {
        class: 'input', type: 'time', id: 'rs-time', name: 'time', value: appointment.time
      });

      const dateInput = UI.el('input', {
        class: 'input', type: 'date', id: 'rs-date', name: 'date',
        value: appointment.date, min: Store.todayISO(), 'data-autofocus': ''
      });

      function loadSlots() {
        if (!dateInput.value) { return; }
        slotHint.textContent = 'Checking free slots...';
        Store.getAvailability(dateInput.value).then(function (result) {
          const slots = (result.data && result.data.available_slots) || [];
          slotHint.textContent = slots.length
            ? 'Free that day: ' + slots.slice(0, 8).map(UI.formatTime).join(', ')
              + (slots.length > 8 ? ' and more' : '')
            : 'No free slots that day.';
        }).catch(function (error) {
          slotHint.textContent = error.message || 'Could not check that day.';
        });
      }
      dateInput.addEventListener('change', loadSlots);

      const form = UI.el('form', { class: 'stack', novalidate: true, id: 'reschedule-form' }, [
        UI.el('div', { class: 'form-grid' }, [
          UI.el('div', { class: 'field' }, [
            UI.el('label', { for: 'rs-date', text: 'New date' }),
            dateInput,
            UI.el('p', { class: 'error-text' })
          ]),
          UI.el('div', { class: 'field' }, [
            UI.el('label', { for: 'rs-time', text: 'New time' }),
            timeInput,
            UI.el('p', { class: 'error-text' })
          ])
        ]),
        slotHint,
        UI.el('p', {
          class: 'muted small',
          text: 'The patient is not contacted automatically: message delivery is not connected yet.'
        })
      ]);

      const save = UI.el('button', { class: 'btn btn-primary', type: 'submit', form: 'reschedule-form' },
        'Save Changes');

      const instance = UI.modal({
        title: 'Reschedule appointment',
        subtitle: appointment.patient_name + ' - ' + appointment.appointment_id,
        body: [form],
        footer: [
          UI.el('button', {
            class: 'btn btn-secondary', type: 'button',
            onClick: function () { instance.close(); }
          }, 'Cancel'),
          save
        ]
      });
      loadSlots();

      form.addEventListener('submit', function (event) {
        event.preventDefault();
        const valid = UI.validate(form, {
          date: [
            UI.rule.required('Date is required.'),
            UI.rule.date('Enter a valid date.'),
            { test: function (value) { return value >= Store.todayISO(); },
              message: 'The new date cannot be in the past.' }
          ],
          time: [UI.rule.required('Time is required.')]
        });
        if (!valid) { return; }

        UI.setButtonLoading(save, true);
        Store.rescheduleAppointment(id, {
          date: form.elements.date.value, time: form.elements.time.value
        }).then(function () {
          UI.setButtonLoading(save, false);
          instance.close();
          UI.toast('Appointment moved to ' + UI.formatDateShort(form.elements.date.value)
            + ', ' + UI.formatTime(form.elements.time.value));
          (onChange || refreshCurrentPage)();
        }).catch(function (error) {
          UI.setButtonLoading(save, false);
          UI.fieldError(timeInput, error.message || 'That slot is not available.');
        });
      });
    }).catch(report);
  }

  /* ------------------------------------------------------- cancel ------ */
  function confirmCancel(id, onChange) {
    Store.getAppointment(id).then(function (appointment) {
      UI.confirm({
        title: 'Cancel appointment?',
        message: 'Cancel ' + appointment.patient_name + ' at ' + UI.formatTime(appointment.time)
          + ' on ' + UI.formatDate(appointment.date) + '? The record is kept with a cancelled '
          + 'status and the slot becomes free for another patient.',
        confirmLabel: 'Cancel Appointment',
        cancelLabel: 'Keep Appointment',
        danger: true
      }).then(function (confirmed) {
        if (!confirmed) { return; }
        Store.cancelAppointment(id).then(function () {
          UI.toast('Appointment cancelled');
          (onChange || refreshCurrentPage)();
        }).catch(report);
      });
    }).catch(report);
  }

  /* ----------------------------------------------------- complete ------ */
  function complete(id, onChange) {
    Store.completeAppointment(id).then(function () {
      UI.toast('Appointment marked as completed');
      (onChange || refreshCurrentPage)();
    }).catch(report);
  }

  /* ------------------------------------------------ new appointment ---- */
  function openCreate(onChange) {
    const patientSelect = UI.el('select', {
      class: 'select', id: 'na-patient', name: 'patient', 'data-autofocus': ''
    }, [UI.el('option', { value: '', text: 'Loading patients...' })]);

    const newPatientFields = UI.el('div', { class: 'form-grid', hidden: true }, [
      UI.el('div', { class: 'field' }, [
        UI.el('label', { for: 'na-name', text: 'Patient name' }),
        UI.el('input', { class: 'input', type: 'text', id: 'na-name', name: 'name' }),
        UI.el('p', { class: 'error-text' })
      ]),
      UI.el('div', { class: 'field' }, [
        UI.el('label', { for: 'na-phone', text: 'Phone' }),
        UI.el('input', { class: 'input', type: 'tel', id: 'na-phone', name: 'phone', placeholder: '03001234567' }),
        UI.el('p', { class: 'error-text' })
      ])
    ]);

    const newPatientToggle = UI.el('label', { class: 'row small' }, [
      UI.el('input', {
        type: 'checkbox', id: 'na-new', name: 'newPatient',
        onChange: function (event) {
          newPatientFields.hidden = !event.target.checked;
          patientSelect.disabled = event.target.checked;
        }
      }),
      UI.el('span', { text: 'This is a new patient' })
    ]);

    const slotSelect = UI.el('select', { class: 'select', id: 'na-time', name: 'time' },
      [UI.el('option', { value: '', text: 'Choose a date first' })]);

    const dateInput = UI.el('input', {
      class: 'input', type: 'date', id: 'na-date', name: 'date',
      value: Store.todayISO(), min: Store.todayISO()
    });

    function loadSlots() {
      UI.mount(slotSelect, [UI.el('option', { value: '', text: 'Checking...' })]);
      Store.getAvailability(dateInput.value).then(function (result) {
        const slots = (result.data && result.data.available_slots) || [];
        UI.mount(slotSelect, slots.length
          ? [UI.el('option', { value: '', text: 'Select a time' })].concat(
            slots.map(function (slot) {
              return UI.el('option', { value: slot, text: UI.formatTime(slot) });
            }))
          : [UI.el('option', { value: '', text: 'No free slots that day' })]);
      }).catch(function (error) {
        UI.mount(slotSelect, [UI.el('option', { value: '', text: error.message || 'Unavailable' })]);
      });
    }
    dateInput.addEventListener('change', loadSlots);

    const form = UI.el('form', { class: 'stack', novalidate: true, id: 'new-apt-form' }, [
      UI.el('div', { class: 'field' }, [
        UI.el('label', { for: 'na-patient', text: 'Patient' }),
        patientSelect,
        UI.el('p', { class: 'error-text' })
      ]),
      newPatientToggle,
      newPatientFields,
      UI.el('div', { class: 'form-grid' }, [
        UI.el('div', { class: 'field' }, [
          UI.el('label', { for: 'na-date', text: 'Date' }),
          dateInput,
          UI.el('p', { class: 'error-text' })
        ]),
        UI.el('div', { class: 'field' }, [
          UI.el('label', { for: 'na-time', text: 'Time' }),
          slotSelect,
          UI.el('p', { class: 'error-text' })
        ])
      ])
    ]);

    const save = UI.el('button', { class: 'btn btn-primary', type: 'submit', form: 'new-apt-form' },
      'Book Appointment');

    const instance = UI.modal({
      title: 'New appointment',
      subtitle: 'Booked through the Appointment Backend, exactly like a phone booking.',
      body: [form],
      footer: [
        UI.el('button', {
          class: 'btn btn-secondary', type: 'button',
          onClick: function () { instance.close(); }
        }, 'Cancel'),
        save
      ]
    });

    Store.getPatients().then(function (patients) {
      UI.mount(patientSelect, [UI.el('option', { value: '', text: 'Select a patient' })].concat(
        patients.map(function (patient) {
          return UI.el('option', {
            value: patient.patient_id,
            text: patient.name + ' (' + patient.patient_id + ')'
          });
        })));
    }).catch(function () {
      UI.mount(patientSelect, [UI.el('option', { value: '', text: 'Could not load patients' })]);
    });
    loadSlots();

    form.addEventListener('submit', function (event) {
      event.preventDefault();
      const creatingPatient = form.elements.newPatient.checked;
      const rules = {
        date: [UI.rule.required('Date is required.'), UI.rule.date('Enter a valid date.')],
        time: [UI.rule.required('Choose a time.')]
      };
      if (creatingPatient) {
        rules.name = [UI.rule.required('Patient name is required.')];
        rules.phone = [UI.rule.required('Phone number is required.')];
      } else {
        rules.patient = [UI.rule.required('Select a patient.')];
      }
      if (!UI.validate(form, rules)) { return; }

      UI.setButtonLoading(save, true);
      const ready = creatingPatient
        ? Store.createPatient({
          name: form.elements.name.value.trim(),
          phone: form.elements.phone.value.trim()
        }).then(function (patient) { return patient.patient_id; })
        : Promise.resolve(form.elements.patient.value);

      ready.then(function (patientId) {
        return Store.createAppointment({
          patient_id: patientId,
          date: form.elements.date.value,
          time: form.elements.time.value
        });
      }).then(function (result) {
        UI.setButtonLoading(save, false);
        instance.close();
        UI.toast('Appointment booked (' + (result.appointment_id || 'saved') + ')');
        (onChange || refreshCurrentPage)();
      }).catch(function (error) {
        UI.setButtonLoading(save, false);
        UI.toast(error.message || 'The appointment could not be booked.', 'error');
      });
    });
  }

  /* Buttons for one appointment, shared by every table. */
  function actionButtons(appointment, onChange) {
    const id = appointment.appointment_id;
    const buttons = [
      UI.el('button', {
        class: 'btn btn-secondary btn-sm', type: 'button',
        onClick: function () { openDetails(id, onChange); }
      }, 'View')
    ];
    if (Store.isActive(appointment.status)) {
      buttons.push(UI.el('button', {
        class: 'btn btn-primary btn-sm', type: 'button',
        onClick: function () { complete(id, onChange); }
      }, 'Complete'));
      buttons.push(UI.el('button', {
        class: 'btn btn-ghost btn-sm', type: 'button',
        'aria-label': 'Cancel appointment for ' + appointment.patient_name,
        onClick: function () { confirmCancel(id, onChange); }
      }, 'Cancel'));
    }
    return buttons;
  }

  return {
    openDetails: openDetails,
    openReschedule: openReschedule,
    confirmCancel: confirmCancel,
    complete: complete,
    openCreate: openCreate,
    actionButtons: actionButtons
  };
})();

/* ---------------------------------------------------------- the page --- */
window.Pages.appointments = (function () {
  'use strict';

  const FILTERS = [
    { id: 'all', label: 'All' },
    { id: 'today', label: 'Today' },
    { id: 'upcoming', label: 'Upcoming' },
    { id: 'completed', label: 'Completed' },
    { id: 'cancelled', label: 'Cancelled' },
    { id: 'past', label: 'Past' }
  ];

  const filters = { scope: 'all', query: '', date: '' };
  const PAGE_SIZE = 20;
  let shownCount = PAGE_SIZE;

  function render(container) {
    const tableBody = UI.el('div', { class: 'card-body card-body-flush' }, UI.skeleton(6));

    const searchInput = UI.el('input', {
      class: 'input', type: 'search', id: 'apt-search',
      placeholder: 'Search patient, appointment ID or phone',
      value: filters.query, 'aria-label': 'Search appointments'
    });
    let debounce = null;
    searchInput.addEventListener('input', function () {
      clearTimeout(debounce);
      debounce = setTimeout(function () {
        filters.query = searchInput.value;
        load(tableBody, true);
      }, 220);
    });

    const dateInput = UI.el('input', {
      class: 'input', type: 'date', id: 'apt-date', value: filters.date,
      'aria-label': 'Filter by date'
    });
    dateInput.addEventListener('change', function () {
      filters.date = dateInput.value;
      load(tableBody, true);
    });

    const segmented = UI.el('div', { class: 'segmented', role: 'group', 'aria-label': 'Filter appointments' },
      FILTERS.map(function (filter) {
        const button = UI.el('button', {
          type: 'button',
          'aria-pressed': filters.scope === filter.id ? 'true' : 'false',
          text: filter.label,
          onClick: function () {
            filters.scope = filter.id;
            segmented.querySelectorAll('button').forEach(function (other) {
              other.setAttribute('aria-pressed', other === button ? 'true' : 'false');
            });
            load(tableBody, true);
          }
        });
        return button;
      }));

    UI.mount(container, [
      UI.el('div', { class: 'page-head' }, [
        UI.el('div', {}, [
          UI.el('h2', { text: 'All appointments' }),
          UI.el('p', { text: 'Everything in the clinic diary, including bookings made by the voice assistant.' })
        ]),
        UI.el('button', {
          class: 'btn btn-primary', type: 'button',
          onClick: function () { AppointmentActions.openCreate(function () { load(tableBody, true); }); }
        }, [UI.icon('plus', 'icon-sm'), 'New Appointment'])
      ]),
      UI.el('div', { class: 'card' }, [
        UI.el('div', { class: 'card-body' }, [
          UI.el('div', { class: 'toolbar' }, [
            UI.el('div', { class: 'search grow' }, [UI.icon('search'), searchInput]),
            dateInput,
            UI.el('button', {
              class: 'btn btn-ghost btn-sm', type: 'button',
              onClick: function () {
                filters.scope = 'all'; filters.query = ''; filters.date = '';
                searchInput.value = ''; dateInput.value = '';
                segmented.querySelectorAll('button').forEach(function (button, index) {
                  button.setAttribute('aria-pressed', index === 0 ? 'true' : 'false');
                });
                load(tableBody, true);
              }
            }, 'Reset')
          ]),
          UI.el('div', { class: 'toolbar mt-16' }, segmented)
        ])
      ]),
      UI.el('div', { class: 'card' }, [tableBody])
    ]);

    load(tableBody, true);
  }

  function load(tableBody, resetPaging) {
    if (resetPaging) { shownCount = PAGE_SIZE; }
    UI.mount(tableBody, UI.skeleton(6));

    Store.getAppointments(filters).then(function (list) {
      if (!list.length) {
        UI.mount(tableBody, UI.empty({
          icon: 'calendar',
          title: 'No appointments found',
          message: 'Nothing matches these filters. Try another date or reset them.'
        }));
        return;
      }

      const page = list.slice(0, shownCount);
      const reload = function () { load(tableBody); };

      const rows = page.map(function (appointment) {
        return UI.el('tr', {}, [
          UI.el('td', { 'data-label': 'ID', class: 'cell-sub nowrap', text: appointment.appointment_id }),
          UI.el('td', { 'data-label': 'Date', class: 'nowrap' }, [
            UI.el('span', { text: UI.formatDateShort(appointment.date) }),
            UI.el('span', { class: 'cell-sub', text: ' ' + UI.relativeDay(appointment.date) })
          ]),
          UI.el('td', { 'data-label': 'Time', class: 'nowrap', text: UI.formatTime(appointment.time) }),
          UI.el('td', { 'data-label': 'Patient' }, [
            UI.el('span', { class: 'cell-primary', text: appointment.patient_name }),
            UI.el('div', { class: 'cell-sub', text: appointment.patient_id })
          ]),
          UI.el('td', { 'data-label': 'Phone', class: 'nowrap', text: appointment.patient_phone || '-' }),
          UI.el('td', { 'data-label': 'Status' }, UI.badge(appointment.status)),
          UI.el('td', { 'data-label': 'Actions' },
            UI.el('div', { class: 'cell-actions' },
              AppointmentActions.actionButtons(appointment, reload)))
        ]);
      });

      UI.mount(tableBody, UI.el('div', { class: 'table-wrap' }, [
        UI.el('table', { class: 'data' }, [
          UI.el('thead', {}, UI.el('tr', {}, [
            UI.el('th', { scope: 'col', text: 'Appointment ID' }),
            UI.el('th', { scope: 'col', text: 'Date' }),
            UI.el('th', { scope: 'col', text: 'Time' }),
            UI.el('th', { scope: 'col', text: 'Patient' }),
            UI.el('th', { scope: 'col', text: 'Phone' }),
            UI.el('th', { scope: 'col', text: 'Status' }),
            UI.el('th', { scope: 'col', class: 'text-right', text: 'Actions' })
          ])),
          UI.el('tbody', {}, rows)
        ])
      ]));

      if (list.length > page.length) {
        tableBody.appendChild(UI.el('div', { class: 'card-body row-between wrap' }, [
          UI.el('span', {
            class: 'muted small',
            text: 'Showing ' + page.length + ' of ' + list.length + ' appointments'
          }),
          UI.el('button', {
            class: 'btn btn-secondary btn-sm', type: 'button',
            onClick: function () { shownCount += PAGE_SIZE; load(tableBody); }
          }, 'Load more')
        ]));
      }
    }).catch(function (error) {
      UI.mount(tableBody, UI.apiError(error, function () { load(tableBody, true); }));
    });
  }

  return { render: render };
})();
