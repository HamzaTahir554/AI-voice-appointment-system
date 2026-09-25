/* ==========================================================================
   home.js - the Dashboard page: today at a glance.

   One call to /dashboard/summary provides the counters, today's list, the
   queue and what is coming up. Every number is computed by the backend from
   the appointment records in Firebase.
   ========================================================================== */
window.Pages = window.Pages || {};

window.Pages.dashboard = (function () {
  'use strict';

  function kpi(label, value, iconName, variant) {
    return UI.el('div', { class: 'kpi ' + (variant ? 'kpi-' + variant : '') }, [
      UI.el('span', { class: 'kpi-icon' }, UI.icon(iconName)),
      UI.el('div', {}, [
        UI.el('div', { class: 'kpi-value', text: String(value) }),
        UI.el('div', { class: 'kpi-label', text: label })
      ])
    ]);
  }

  function render(container) {
    const kpiRow = UI.el('div', { class: 'kpi-grid' }, UI.skeleton(1));
    const todayBody = UI.el('div', { class: 'card-body card-body-flush' }, UI.skeleton(5));
    const queueBody = UI.el('div', { class: 'card-body' }, UI.skeleton(3));
    const upcomingBody = UI.el('div', { class: 'card-body' }, UI.skeleton(2));

    const statsState = { period: 'month', start: '', end: '' };
    const statsBody = UI.el('div', { class: 'card-body' }, UI.skeleton(2));

    UI.mount(container, [
      kpiRow,
      UI.el('section', { class: 'card', 'aria-labelledby': 'stats-heading' }, [
        UI.el('div', { class: 'card-head' }, [
          UI.el('div', {}, [
            UI.el('h3', { id: 'stats-heading', text: 'My appointment statistics' }),
            UI.el('p', { class: 'muted small',
                         text: 'Counted from your own appointment records.' })
          ]),
          StatsView.periodPicker(statsState, function () { loadStats(statsBody, statsState); })
        ]),
        statsBody
      ]),
      UI.el('div', { class: 'split' }, [
        UI.el('section', { class: 'card', 'aria-labelledby': 'today-heading' }, [
          UI.el('div', { class: 'card-head' }, [
            UI.el('h3', { id: 'today-heading', text: "Today's Appointments" }),
            UI.el('button', {
              class: 'btn btn-secondary btn-sm', type: 'button',
              onClick: function () { Nav.go('appointments'); }
            }, 'Manage all')
          ]),
          todayBody
        ]),
        UI.el('div', { class: 'stack' }, [
          UI.el('section', { class: 'card', 'aria-labelledby': 'queue-heading' }, [
            UI.el('div', { class: 'card-head' }, [
              UI.el('h3', { id: 'queue-heading', text: 'Waiting Now' }),
              UI.el('span', {
                class: 'muted small',
                title: 'Appointments today whose time has started and that are not closed yet',
                text: 'due now'
              })
            ]),
            queueBody
          ]),
          UI.el('section', { class: 'card', 'aria-labelledby': 'upcoming-heading' }, [
            UI.el('div', { class: 'card-head' }, [
              UI.el('h3', { id: 'upcoming-heading', text: 'Coming Up' })
            ]),
            upcomingBody
          ])
        ])
      ])
    ]);

    load(kpiRow, todayBody, queueBody, upcomingBody);
    loadStats(statsBody, statsState);
  }

  /* ------------------------------------------------------- statistics -- */
  function loadStats(body, state) {
    UI.mount(body, UI.loading('Counting your appointments...'));
    Store.getStatistics(state).then(function (data) {
      const totals = data.totals;
      UI.mount(body, [
        StatsView.figures(totals),
        totals.total ? StatsView.bar(totals) : StatsView.emptyNote(data.period),
        UI.el('div', { class: 'stats-foot' }, [
          StatsView.todayLine(data.today),
          UI.el('span', { class: 'muted small',
                          text: data.all_time.total + ' in total, all time' })
        ])
      ]);
    }).catch(function (error) {
      UI.mount(body, UI.apiError(error, function () { loadStats(body, state); }));
    });
  }

  function load(kpiRow, todayBody, queueBody, upcomingBody) {
    const reload = function () { load(kpiRow, todayBody, queueBody, upcomingBody); };

    Store.getSummary().then(function (summary) {
      const stats = summary.stats;
      UI.mount(kpiRow, [
        kpi('Appointments today', stats.total, 'calendar'),
        kpi('Completed today', stats.completed, 'check-circle', 'success'),
        kpi('Still to come today', stats.upcoming, 'clock', 'info'),
        kpi('Waiting now', stats.waiting, 'users', 'warning'),
        kpi('Cancelled today', stats.cancelled, 'ban', 'danger')
      ]);

      renderToday(todayBody, summary.today, reload);
      renderQueue(queueBody, summary.queue, reload);
      renderUpcoming(upcomingBody, summary.upcoming, summary.stats.upcoming_total, reload);
    }).catch(function (error) {
      const problem = UI.apiError(error, reload);
      UI.mount(kpiRow, []);
      UI.mount(todayBody, problem);
      UI.mount(queueBody, []);
      UI.mount(upcomingBody, []);
    });
  }

  function renderToday(todayBody, list, reload) {
    if (!list.length) {
      UI.mount(todayBody, UI.empty({
        icon: 'calendar',
        title: 'No appointments today',
        message: 'Appointments booked by the voice assistant appear here automatically.'
      }));
      return;
    }

    const open = list.filter(function (a) { return Store.isActive(a.status); });
    const source = open.length ? open : list;
    const shown = source.slice(0, 8);

    UI.mount(todayBody, shown.map(function (appointment) {
      return UI.el('div', { class: 'apt-row' }, [
        UI.el('span', { class: 'apt-time', text: UI.formatTime(appointment.time) }),
        UI.el('div', { class: 'apt-info' }, [
          UI.el('strong', { text: appointment.patient_name }),
          UI.el('span', { text: appointment.patient_phone || appointment.appointment_id })
        ]),
        UI.badge(appointment.status),
        UI.el('div', { class: 'apt-actions' },
          AppointmentActions.actionButtons(appointment, reload))
      ]);
    }).concat([
      UI.el('div', { class: 'apt-row' }, [
        UI.el('span', {
          class: 'muted small grow',
          text: open.length
            ? 'Showing ' + shown.length + ' of ' + open.length + ' still open today ('
              + list.length + ' booked in total).'
            : 'The day is complete: all ' + list.length + ' appointments are closed.'
        }),
        UI.el('button', {
          class: 'btn btn-ghost btn-sm', type: 'button',
          onClick: function () { Nav.go('appointments'); }
        }, 'See the full day')
      ])
    ]));
  }

  function renderQueue(queueBody, queue, reload) {
    if (!queue.length) {
      UI.mount(queueBody, UI.empty({
        icon: 'users',
        title: 'Nobody is due',
        message: 'Appointments whose time has arrived appear here.'
      }));
      return;
    }
    UI.mount(queueBody, queue.map(function (appointment, index) {
      return UI.el('div', { class: 'queue-item' }, [
        UI.el('span', { class: 'queue-num', text: String(index + 1) }),
        UI.el('div', { class: 'grow' }, [
          UI.el('strong', { text: appointment.patient_name }),
          UI.el('span', { text: UI.formatTime(appointment.time) + ' - ' + UI.statusLabel(appointment.status) })
        ]),
        UI.el('button', {
          class: 'btn btn-primary btn-sm', type: 'button',
          onClick: function () { AppointmentActions.complete(appointment.appointment_id, reload); }
        }, 'Complete')
      ]);
    }));
  }

  function renderUpcoming(upcomingBody, upcoming, total, reload) {
    if (!upcoming.length) {
      UI.mount(upcomingBody, UI.empty({
        icon: 'clock',
        title: 'Nothing booked yet',
        message: 'Appointments for the coming days will appear here.'
      }));
      return;
    }
    const tomorrow = Store.isoFromOffset(1);
    const tomorrowCount = upcoming.filter(function (a) { return a.date === tomorrow; }).length;

    UI.mount(upcomingBody, [
      UI.el('p', {
        class: 'muted small',
        text: 'You have ' + tomorrowCount + ' appointment' + (tomorrowCount === 1 ? '' : 's')
          + ' tomorrow and ' + total + ' in the coming days.'
      }),
      UI.el('ul', { class: 'list-plain mt-16' }, upcoming.slice(0, 4).map(function (appointment) {
        return UI.el('li', {}, UI.el('div', { class: 'row-between' }, [
          UI.el('div', {}, [
            UI.el('strong', { class: 'small', text: appointment.patient_name }),
            UI.el('div', {
              class: 'cell-sub',
              text: UI.relativeDay(appointment.date) + ' - ' + UI.formatTime(appointment.time)
            })
          ]),
          UI.el('button', {
            class: 'btn btn-ghost btn-sm', type: 'button',
            onClick: function () { AppointmentActions.openDetails(appointment.appointment_id, reload); }
          }, 'View')
        ]));
      })),
      UI.el('button', {
        class: 'btn btn-secondary btn-block mt-16', type: 'button',
        onClick: function () { Nav.go('appointments'); }
      }, 'View all appointments')
    ]);
  }

  return { render: render };
})();
