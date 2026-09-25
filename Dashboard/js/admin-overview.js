/* ==========================================================================
   admin-overview.js - the administrator's first screen.

   Counters for the register and the diary, today's appointments across every
   doctor, and what has changed lately. Every number comes from
   /admin/summary, which counts the real records - nothing here is estimated
   or cached.
   ========================================================================== */
window.Pages = window.Pages || {};

window.Pages.adminOverview = (function () {
  'use strict';

  function stat(value, label, note) {
    return UI.el('div', { class: 'stat' }, [
      UI.el('span', { class: 'stat-value', text: String(value) }),
      UI.el('span', { class: 'stat-label', text: label }),
      note ? UI.el('span', { class: 'stat-note', text: note }) : null
    ]);
  }

  function statCards(summary) {
    const doctors = summary.doctors;
    const appointments = summary.appointments;
    return [
      UI.el('section', { class: 'card', 'aria-labelledby': 'register-heading' }, [
        UI.el('div', { class: 'card-head' }, [
          UI.el('h3', { id: 'register-heading', text: 'Doctors' }),
          UI.el('button', {
            class: 'link-btn', type: 'button',
            onClick: function () { Nav.go('doctors'); }
          }, 'Manage')
        ]),
        UI.el('div', { class: 'card-body stat-row' }, [
          stat(doctors.total, 'On the register'),
          stat(doctors.active, 'Bookable now'),
          stat(doctors.inactive, 'Deactivated'),
          stat(doctors.on_leave_today || 0, 'On leave today'),
          doctors.archived ? stat(doctors.archived, 'Removed') : null
        ])
      ]),
      /* Only what the statistics card below does not answer: what is
         happening now. Everything counted by status lives there, under an
         explicit period, so the two cannot appear to disagree. */
      UI.el('section', { class: 'card', 'aria-labelledby': 'diary-heading' }, [
        UI.el('div', { class: 'card-head' }, [
          UI.el('h3', { id: 'diary-heading', text: 'The diary now' }),
          UI.el('button', {
            class: 'link-btn', type: 'button',
            onClick: function () { Nav.go('appointments'); }
          }, 'View all')
        ]),
        UI.el('div', { class: 'card-body stat-row' }, [
          stat(appointments.today, 'Today', appointments.today_active + ' still live'),
          stat(appointments.upcoming, 'Still to come', 'today and later')
        ])
      ])
    ];
  }


  /* ------------------------------------------------------- statistics -- */
  /* Kept in one object so the period picker and the doctor filter can both
     change it and ask for the numbers again. */
  const statsState = { period: 'month', start: '', end: '', doctorId: '' };

  function statsCard() {
    const body = UI.el('div', { class: 'card-body' }, UI.skeleton(2));
    const doctorBody = UI.el('div', { class: 'card-body card-body-flush' },
                             UI.skeleton(4));

    function reload() { loadStats(body, doctorBody); }

    return {
      node: UI.el('div', { class: 'stack-16' }, [
        UI.el('section', { class: 'card', 'aria-labelledby': 'stats-heading' }, [
          UI.el('div', { class: 'card-head' }, [
            UI.el('div', {}, [
              UI.el('h3', { id: 'stats-heading', text: 'Appointment statistics' }),
              UI.el('p', { class: 'muted small', id: 'stats-scope',
                           text: 'Every doctor in the clinic.' })
            ]),
            StatsView.periodPicker(statsState, reload)
          ]),
          body
        ]),
        UI.el('section', { class: 'card', 'aria-labelledby': 'per-doctor-heading' }, [
          UI.el('div', { class: 'card-head' }, [
            UI.el('div', {}, [
              UI.el('h3', { id: 'per-doctor-heading', text: 'By doctor' }),
              UI.el('p', { class: 'muted small',
                           text: 'For the same period. Choose a doctor to narrow the figures above.' })
            ])
          ]),
          doctorBody
        ])
      ]),
      load: reload
    };
  }

  function loadStats(body, doctorBody) {
    UI.mount(body, UI.loading('Counting appointments...'));
    AdminStore.getStatistics(statsState).then(function (data) {
      const totals = data.totals;
      const scope = document.getElementById('stats-scope');
      if (scope) {
        scope.textContent = data.doctor
          ? data.doctor.name + ' only \u00b7 ' + data.period.label
          : 'Every doctor in the clinic \u00b7 ' + data.period.label;
      }

      UI.mount(body, [
        data.doctor
          ? UI.el('div', { class: 'row gap-8 mb-8' }, [
            UI.el('span', { class: 'badge badge-neutral', text: data.doctor.name }),
            UI.el('button', {
              class: 'link-btn', type: 'button',
              onClick: function () {
                statsState.doctorId = '';
                loadStats(body, doctorBody);
              }
            }, 'Show the whole clinic')
          ])
          : null,
        StatsView.figures(totals),
        totals.total ? StatsView.bar(totals) : StatsView.emptyNote(data.period),
        UI.el('div', { class: 'stats-foot' }, [
          StatsView.todayLine(data.today),
          UI.el('span', { class: 'muted small',
                          text: data.all_time.total + ' in total, all time' })
        ])
      ]);

      renderPerDoctor(doctorBody, data, function () { loadStats(body, doctorBody); });
    }).catch(function (error) {
      UI.mount(body, UI.apiError(error, function () { loadStats(body, doctorBody); }));
      UI.mount(doctorBody, []);
    });
  }

  function renderPerDoctor(body, data, reload) {
    const rows = data.doctors || [];
    if (!rows.length) {
      UI.mount(body, UI.empty({
        icon: 'users', title: 'No doctors yet',
        message: 'Add a doctor and their appointments will be counted here.'
      }));
      return;
    }

    UI.mount(body, UI.el('div', { class: 'table-wrap' },
      UI.el('table', { class: 'data table-stats' }, [
        UI.el('thead', {}, UI.el('tr', {}, [
          UI.el('th', { text: 'Doctor' }),
          UI.el('th', { class: 'num', text: 'Total' }),
          UI.el('th', { class: 'num', text: 'Confirmed' }),
          UI.el('th', { class: 'num', text: 'Pending' }),
          UI.el('th', { class: 'num', text: 'Completed' }),
          UI.el('th', { class: 'num', text: 'Cancelled' }),
          UI.el('th', { class: 'cell-right', text: '' })
        ])),
        UI.el('tbody', {}, rows.map(function (row) {
          const chosen = statsState.doctorId === row.doctor_id;
          return UI.el('tr', { class: chosen ? 'is-chosen' : '' }, [
            UI.el('td', { 'data-label': 'Doctor' }, [
              UI.el('button', {
                class: 'link-btn', type: 'button',
                title: 'See ' + row.name + '\u2019s appointments',
                onClick: function () {
                  Nav.go('doctors');
                  setTimeout(function () {
                    window.Pages.adminDoctors.openDetails(row.doctor_id, 'appointments');
                  }, 250);
                }
              }, row.name),
              row.active ? null : UI.el('span', { class: 'muted small',
                                                  text: ' (deactivated)' })
            ]),
            UI.el('td', { class: 'num', 'data-label': 'Total' },
                  UI.el('strong', { text: String(row.total) })),
            UI.el('td', { class: 'num', 'data-label': 'Confirmed', text: String(row.confirmed) }),
            UI.el('td', { class: 'num', 'data-label': 'Pending', text: String(row.pending) }),
            UI.el('td', { class: 'num', 'data-label': 'Completed', text: String(row.completed) }),
            UI.el('td', { class: 'num', 'data-label': 'Cancelled', text: String(row.cancelled) }),
            UI.el('td', { class: 'cell-right', 'data-label': '' }, UI.el('button', {
              class: 'btn btn-ghost btn-sm', type: 'button',
              onClick: function () {
                statsState.doctorId = chosen ? '' : row.doctor_id;
                reload();
              }
            }, chosen ? 'Clear' : 'Figures'))
          ]);
        }))
      ])));
  }

  function todayCard(summary) {
    const rows = summary.today;
    const body = rows.length
      ? UI.el('div', { class: 'table-wrap' }, UI.el('table', { class: 'data' }, [
        UI.el('thead', {}, UI.el('tr', {}, [
          UI.el('th', { text: 'Time' }),
          UI.el('th', { text: 'Patient' }),
          UI.el('th', { text: 'Doctor' }),
          UI.el('th', { text: 'Status' })
        ])),
        UI.el('tbody', {}, rows.slice(0, 10).map(function (appointment) {
          return UI.el('tr', {}, [
            UI.el('td', { 'data-label': 'Time', text: UI.formatTime(appointment.time) }),
            UI.el('td', { 'data-label': 'Patient' }, [
              UI.el('div', { text: appointment.patient_name }),
              UI.el('span', { class: 'muted small', text: appointment.patient_phone || '' })
            ]),
            UI.el('td', { 'data-label': 'Doctor' }, UI.el('button', {
              class: 'link-btn', type: 'button',
              onClick: function () {
                Nav.go('doctors');
                setTimeout(function () {
                  window.Pages.adminDoctors.openDetails(appointment.doctor_id);
                }, 250);
              }
            }, appointment.doctor_id)),
            UI.el('td', { 'data-label': 'Status' }, UI.badge(appointment.status))
          ]);
        }))
      ]))
      : UI.empty({
        icon: 'calendar',
        title: 'Nothing booked for today',
        message: 'Appointments booked by the voice assistant appear here as soon as the '
          + 'assistant confirms them.'
      });

    return UI.el('section', { class: 'card', 'aria-labelledby': 'today-heading' }, [
      UI.el('div', { class: 'card-head' }, [
        UI.el('div', {}, [
          UI.el('h3', { id: 'today-heading', text: "Today's appointments" }),
          UI.el('p', { class: 'muted small', text: UI.formatDate(summary.date || '') })
        ]),
        rows.length > 10
          ? UI.el('span', { class: 'muted small', text: 'Showing 10 of ' + rows.length })
          : null
      ]),
      UI.el('div', { class: 'card-body card-body-flush' }, body)
    ]);
  }

  function activityCard(summary) {
    const events = summary.activity || [];
    const body = events.length
      ? UI.el('ul', { class: 'activity' }, events.map(function (event) {
        return UI.el('li', {}, [
          UI.el('span', { class: 'activity-dot activity-' + event.kind, 'aria-hidden': 'true' }),
          UI.el('div', {}, [
            UI.el('div', { text: event.text }),
            UI.el('span', { class: 'muted small', text: UI.timeAgo(event.at) })
          ])
        ]);
      }))
      : UI.el('p', {
        class: 'muted small',
        text: 'Changes to the register and messages queued for patients will '
          + 'be listed here.'
      });

    return UI.el('section', { class: 'card', 'aria-labelledby': 'activity-heading' }, [
      UI.el('div', { class: 'card-head' },
            UI.el('h3', { id: 'activity-heading', text: 'Recent activity' })),
      UI.el('div', { class: 'card-body' }, body)
    ]);
  }

  function render(container) {
    UI.mount(container, UI.loading('Loading the clinic overview...'));

    AdminStore.getSummary().then(function (summary) {
      const stats = statsCard();
      UI.mount(container, [
        UI.el('div', { class: 'page-head' }, [
          UI.el('div', {}, [
            UI.el('h2', { text: 'Clinic overview' }),
            UI.el('p', { text: 'The register and the diary at a glance.' })
          ]),
          UI.el('button', {
            class: 'btn btn-primary', type: 'button',
            onClick: function () {
              Nav.go('doctors');
              setTimeout(function () { window.Pages.adminDoctors.openAddForm(); }, 250);
            }
          }, [UI.icon('plus', 'icon-sm'), 'Add doctor'])
        ]),
        UI.el('div', { class: 'grid-2' }, statCards(summary)),
        stats.node,
        UI.el('div', { class: 'grid-2-wide' }, [
          todayCard(summary),
          activityCard(summary)
        ])
      ]);
      stats.load();
    }).catch(function (error) {
      UI.mount(container, UI.apiError(error, function () { render(container); }));
    });
  }

  return { render: render };
})();
