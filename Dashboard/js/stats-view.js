/* ==========================================================================
   stats-view.js - the pieces both statistics screens are built from.

   The doctor sees their own numbers and the administrator sees the clinic's,
   but they are the same counts of the same records, so they are drawn by the
   same code: a period picker, a row of figures, and one bar showing how the
   total splits by status.

   Every number handed to these functions comes from the backend
   (/dashboard/statistics or /admin/statistics). Nothing is computed here
   beyond percentages for the bar's widths.
   ========================================================================== */
window.StatsView = (function () {
  'use strict';

  const PERIODS = [
    { id: 'today', label: 'Today' },
    { id: 'week', label: 'This week' },
    { id: 'month', label: 'This month' },
    { id: 'all', label: 'All time' },
    { id: 'custom', label: 'Custom' }
  ];

  /* The five the clinic asks about, plus rescheduled - which is only shown
     when it has actually happened, so a quiet clinic sees five clean
     figures instead of a zero nobody needs. */
  const FIGURES = [
    { key: 'total', label: 'Total', lead: true },
    { key: 'confirmed', label: 'Confirmed', tone: 'confirmed' },
    { key: 'pending', label: 'Pending', tone: 'pending' },
    { key: 'rescheduled', label: 'Rescheduled', tone: 'rescheduled', onlyIfAny: true },
    { key: 'completed', label: 'Completed', tone: 'completed' },
    { key: 'cancelled', label: 'Cancelled', tone: 'cancelled' }
  ];

  /* ------------------------------------------------------ period picker -- */
  /* state: { period, start, end } - mutated in place, onChange after a valid
     choice, so the caller just re-fetches with the same object. */
  function periodPicker(state, onChange) {
    const custom = UI.el('div', { class: 'period-custom', hidden: state.period !== 'custom' });
    const from = UI.el('input', {
      class: 'input', type: 'date', id: 'stats-from', value: state.start || '',
      'aria-label': 'From date'
    });
    const to = UI.el('input', {
      class: 'input', type: 'date', id: 'stats-to', value: state.end || '',
      'aria-label': 'To date'
    });
    const problem = UI.el('p', { class: 'error-text' });

    function applyCustom() {
      problem.textContent = '';
      if (!from.value || !to.value) { return; }
      if (to.value < from.value) {
        problem.textContent = 'The end date must be on or after the start date.';
        return;
      }
      state.period = 'custom';
      state.start = from.value;
      state.end = to.value;
      onChange();
    }
    from.addEventListener('change', applyCustom);
    to.addEventListener('change', applyCustom);

    UI.mount(custom, [
      UI.el('div', { class: 'row gap-8' }, [
        from, UI.el('span', { class: 'session-sep', text: 'to' }), to
      ]),
      problem
    ]);

    const buttons = UI.el('div', { class: 'segmented', role: 'group',
                                   'aria-label': 'Period' },
      PERIODS.map(function (period) {
        return UI.el('button', {
          type: 'button',
          'aria-pressed': state.period === period.id ? 'true' : 'false',
          onClick: function () {
            state.period = period.id;
            custom.hidden = period.id !== 'custom';
            buttons.querySelectorAll('button').forEach(function (node, index) {
              node.setAttribute('aria-pressed',
                                PERIODS[index].id === period.id ? 'true' : 'false');
            });
            if (period.id === 'custom') {
              if (from.value && to.value) { applyCustom(); }
              else { from.focus(); }
              return;
            }
            onChange();
          }
        }, period.label);
      }));

    return UI.el('div', { class: 'period-picker' }, [buttons, custom]);
  }

  /* ----------------------------------------------------------- figures -- */
  function figures(totals) {
    const shown = FIGURES.filter(function (figure) {
      return !figure.onlyIfAny || totals[figure.key];
    });
    return UI.el('div', { class: 'figures' }, shown.map(function (figure) {
      return UI.el('div', { class: 'figure' + (figure.lead ? ' figure-lead' : '') }, [
        UI.el('span', { class: 'figure-value', text: String(totals[figure.key] || 0) }),
        UI.el('span', { class: 'figure-label' }, [
          figure.tone
            ? UI.el('span', { class: 'dot dot-' + figure.tone, 'aria-hidden': 'true' })
            : null,
          UI.el('span', { text: figure.label })
        ])
      ]);
    }));
  }

  /* --------------------------------------------------------- status bar -- */
  /* One bar, so the split is readable at a glance without a chart library. */
  function bar(totals) {
    const total = totals.total || 0;
    if (!total) { return null; }

    const parts = FIGURES.filter(function (figure) {
      return figure.tone && totals[figure.key];
    });

    return UI.el('div', { class: 'status-bar-wrap' }, [
      UI.el('div', {
        class: 'status-bar', role: 'img',
        'aria-label': parts.map(function (part) {
          return totals[part.key] + ' ' + part.label.toLowerCase();
        }).join(', ') || 'no appointments'
      }, parts.map(function (part) {
        const share = (totals[part.key] / total) * 100;
        return UI.el('span', {
          class: 'status-slice status-slice-' + part.tone,
          style: 'width:' + share.toFixed(1) + '%',
          title: part.label + ': ' + totals[part.key]
            + ' (' + Math.round(share) + '%)'
        });
      })),
      UI.el('p', { class: 'muted small' },
            [UI.el('span', {
              text: parts.map(function (part) {
                return totals[part.key] + ' ' + part.label.toLowerCase();
              }).join(' · ')
            })])
    ]);
  }

  /* --------------------------------------------------------- today line -- */
  function todayLine(today) {
    const bits = [
      ['Confirmed', today.confirmed],
      ['Pending', today.pending],
      ['Completed', today.completed],
      ['Cancelled', today.cancelled]
    ];
    if (today.rescheduled) { bits.splice(2, 0, ['Rescheduled', today.rescheduled]); }

    return UI.el('div', { class: 'today-line' }, [
      UI.el('strong', { text: today.total + ' today' }),
      UI.el('span', {
        class: 'muted small',
        text: bits.map(function (pair) { return pair[1] + ' ' + pair[0].toLowerCase(); })
          .join(' · ')
      })
    ]);
  }

  /* A period whose totals are all zero should say so, not show five zeroes
     with no explanation. */
  function emptyNote(period) {
    return UI.el('p', {
      class: 'muted small',
      text: 'No appointments fall in this period (' + period.label.toLowerCase() + ').'
    });
  }

  return {
    PERIODS: PERIODS,
    periodPicker: periodPicker,
    figures: figures,
    bar: bar,
    todayLine: todayLine,
    emptyNote: emptyNote
  };
})();
