/* ==========================================================================
   patients.js - the Patients page.

   The list comes from /dashboard/patients: the people who have appointments
   with THIS doctor, built from the appointment records. A patient record in
   this system holds a name and a phone number - nothing more is stored, and
   nothing more is shown.
   ========================================================================== */
window.Pages = window.Pages || {};

window.PatientActions = (function () {
  'use strict';

  function openDetails(patientId) {
    Store.getPatient(patientId).then(function (patient) {
      const facts = [
        ['Patient ID', patient.patient_id],
        ['Phone', patient.phone || 'Not recorded'],
        ['Appointments with you', String(patient.total_appointments)],
        ['Completed visits', String(patient.completed_visits)],
        ['Last visit', patient.last_visit ? UI.formatDate(patient.last_visit) : 'No completed visit yet']
      ];

      const upcoming = patient.next_appointment
        ? UI.el('div', { class: 'row wrap' }, [
          UI.el('span', {
            text: UI.formatDate(patient.next_appointment.date) + ' at '
              + UI.formatTime(patient.next_appointment.time)
          }),
          UI.badge(patient.next_appointment.status)
        ])
        : UI.el('p', { class: 'muted small', text: 'No upcoming appointment booked.' });

      const history = patient.history.length
        ? UI.el('ul', { class: 'list-plain' }, patient.history.slice(0, 8).map(function (appointment) {
          return UI.el('li', {}, UI.el('div', { class: 'row-between' }, [
            UI.el('div', {}, [
              UI.el('strong', { class: 'small', text: UI.formatDate(appointment.date) }),
              UI.el('div', { class: 'cell-sub', text: UI.formatTime(appointment.time) + ' - ' + appointment.appointment_id })
            ]),
            UI.badge(appointment.status)
          ]));
        }))
        : UI.el('p', { class: 'muted small', text: 'No appointment history yet.' });

      const instance = UI.modal({
        size: 'lg',
        title: patient.name,
        subtitle: 'Patient record and history with you',
        body: [
          UI.el('dl', { class: 'detail-list' }, facts.map(function (fact) {
            return UI.el('div', {}, [UI.el('dt', { text: fact[0] }), UI.el('dd', { text: fact[1] })]);
          })),
          UI.el('hr', { class: 'divider' }),
          UI.el('h4', { text: 'Next appointment' }),
          upcoming,
          UI.el('hr', { class: 'divider' }),
          UI.el('h4', { text: 'Appointment history' }),
          history
        ],
        footer: [
          UI.el('button', {
            class: 'btn btn-primary', type: 'button', 'data-autofocus': '',
            onClick: function () { instance.close(); }
          }, 'Close')
        ]
      });
    }).catch(function (error) {
      UI.toast(error.message || 'Could not open that patient.', 'error');
    });
  }

  return { openDetails: openDetails };
})();

window.Pages.patients = (function () {
  'use strict';

  let query = '';

  function render(container) {
    const listBody = UI.el('div', { class: 'card-body card-body-flush' }, UI.skeleton(6));

    const searchInput = UI.el('input', {
      class: 'input', type: 'search', id: 'patient-search',
      placeholder: 'Search by name, patient ID or phone',
      value: query, 'aria-label': 'Search patients'
    });
    let debounce = null;
    searchInput.addEventListener('input', function () {
      clearTimeout(debounce);
      debounce = setTimeout(function () {
        query = searchInput.value;
        load(listBody);
      }, 220);
    });

    UI.mount(container, [
      UI.el('div', { class: 'page-head' }, [
        UI.el('div', {}, [
          UI.el('h2', { text: 'Patients' }),
          UI.el('p', { text: 'People with appointments in your diary.' })
        ]),
        UI.el('button', {
          class: 'btn btn-primary', type: 'button',
          onClick: function () { openAdd(function () { load(listBody); }); }
        }, [UI.icon('plus', 'icon-sm'), 'Add Patient'])
      ]),
      UI.el('div', { class: 'card' }, [
        UI.el('div', { class: 'card-body' }, [
          UI.el('div', { class: 'toolbar' }, [
            UI.el('div', { class: 'search grow' }, [UI.icon('search'), searchInput])
          ])
        ])
      ]),
      UI.el('div', { class: 'card' }, [listBody])
    ]);

    load(listBody);
  }

  /* One page from the server; "Load more" appends the next one. */
  let shown = [];
  let ticket = 0;

  function load(listBody) {
    const mine = ++ticket;
    UI.mount(listBody, UI.skeleton(6));
    Store.getPatientsPage({ query: query, offset: 0 }).then(function (page) {
      if (mine !== ticket) { return; }
      shown = page.patients;
      paint(listBody, page);
    }).catch(function (error) {
      if (mine !== ticket) { return; }
      UI.mount(listBody, UI.apiError(error, function () { load(listBody); }));
    });
  }

  function loadMore(listBody, button) {
    const mine = ++ticket;
    UI.setButtonLoading(button, true);
    Store.getPatientsPage({ query: query, offset: shown.length }).then(function (page) {
      if (mine !== ticket) { return; }
      shown = shown.concat(page.patients);
      paint(listBody, page);
    }).catch(function (error) {
      if (mine !== ticket) { return; }
      UI.setButtonLoading(button, false);
      UI.toast(error.message || 'Could not load more patients.', 'error');
    });
  }

  function paint(listBody, page) {
    const patients = shown;
    if (!patients.length) {
      UI.mount(listBody, UI.empty({
        icon: 'users',
        title: 'No patients found',
        message: query
          ? 'No patient matches "' + query + '".'
          : 'Patients appear here once they have an appointment with you.'
      }));
      return;
    }

    const rows = patients.map(function (patient) {
      const next = patient.next_appointment;
      return UI.el('tr', {}, [
        UI.el('td', { 'data-label': 'Patient' }, [
          UI.el('div', { class: 'patient-name' }, [
            UI.avatar(patient.name),
            UI.el('div', {}, [
              UI.el('span', { class: 'cell-primary', text: patient.name }),
              UI.el('div', { class: 'cell-sub', text: patient.patient_id })
            ])
          ])
        ]),
        UI.el('td', { 'data-label': 'Phone', class: 'nowrap', text: patient.phone || '-' }),
        UI.el('td', { 'data-label': 'Appointments', text: String(patient.total_appointments) }),
        UI.el('td', {
          'data-label': 'Last visit', class: 'nowrap',
          text: patient.last_visit ? UI.formatDateShort(patient.last_visit) : 'None'
        }),
        UI.el('td', { 'data-label': 'Next appointment', class: 'nowrap' },
          next
            ? UI.el('span', { text: UI.relativeDay(next.date) + ' ' + UI.formatTime(next.time) })
            : UI.el('span', { class: 'pill', text: 'None booked' })),
        UI.el('td', { 'data-label': 'Actions' }, UI.el('div', { class: 'cell-actions' }, [
          UI.el('button', {
            class: 'btn btn-secondary btn-sm', type: 'button',
            onClick: function () { PatientActions.openDetails(patient.patient_id); }
          }, 'View')
        ]))
      ]);
    });

    UI.mount(listBody, UI.el('div', { class: 'table-wrap' }, [
      UI.el('table', { class: 'data' }, [
        UI.el('thead', {}, UI.el('tr', {}, [
          UI.el('th', { scope: 'col', text: 'Patient' }),
          UI.el('th', { scope: 'col', text: 'Phone' }),
          UI.el('th', { scope: 'col', text: 'Appointments' }),
          UI.el('th', { scope: 'col', text: 'Last visit' }),
          UI.el('th', { scope: 'col', text: 'Next appointment' }),
          UI.el('th', { scope: 'col', class: 'text-right', text: 'Actions' })
        ])),
        UI.el('tbody', {}, rows)
      ])
    ]));

    if (page.has_more) {
      const more = UI.el('button', {
        class: 'btn btn-secondary btn-sm', type: 'button',
        onClick: function () { loadMore(listBody, more); }
      }, 'Load more');
      listBody.appendChild(UI.el('div', { class: 'card-body row-between wrap' }, [
        UI.el('span', { class: 'muted small',
                        text: 'Showing ' + patients.length + ' of ' + page.total + ' patients' }),
        more
      ]));
    }
  }

  /* ------------------------------------------------------ add patient -- */
  function openAdd(onSaved) {
    const form = UI.el('form', { class: 'stack', novalidate: true, id: 'patient-form' }, [
      UI.el('div', { class: 'field' }, [
        UI.el('label', { for: 'np-name', text: 'Full name' }),
        UI.el('input', { class: 'input', type: 'text', id: 'np-name', name: 'name', 'data-autofocus': '' }),
        UI.el('p', { class: 'error-text' })
      ]),
      UI.el('div', { class: 'field' }, [
        UI.el('label', { for: 'np-phone', text: 'Phone' }),
        UI.el('input', {
          class: 'input', type: 'tel', id: 'np-phone', name: 'phone',
          placeholder: '03001234567'
        }),
        UI.el('p', { class: 'hint', text: 'The number the patient calls from, so the voice assistant recognises them.' }),
        UI.el('p', { class: 'error-text' })
      ])
    ]);

    const save = UI.el('button', { class: 'btn btn-primary', type: 'submit', form: 'patient-form' }, 'Add Patient');

    const instance = UI.modal({
      title: 'Register a patient',
      subtitle: 'Saved to the clinic database.',
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
        name: [UI.rule.required('Patient name is required.')],
        phone: [UI.rule.required('Phone number is required.')]
      });
      if (!valid) { return; }

      UI.setButtonLoading(save, true);
      Store.createPatient({
        name: form.elements.name.value.trim(),
        phone: form.elements.phone.value.trim()
      }).then(function (patient) {
        UI.setButtonLoading(save, false);
        instance.close();
        UI.toast(patient.name + ' registered as ' + patient.patient_id);
        onSaved();
      }).catch(function (error) {
        UI.setButtonLoading(save, false);
        UI.fieldError(form.elements.phone, error.message || 'Could not register this patient.');
      });
    });
  }

  return { render: render };
})();
