/* ==========================================================================
   admin-doctor-detail.js - everything about one doctor, in one dialog.

   Three tabs, because they answer three different questions:

     Profile       who the doctor is and what the assistant tells callers
     Availability  the working week and the days they are away
     Appointments  what is actually in their diary

   Blocking a date here runs the backend's own cascade
   (cancellation_service): the live appointments that day become
   cancelled_by_doctor and one message per patient is queued. The dialog says
   how many appointments that will be before anything happens.
   ========================================================================== */
window.DoctorDetail = (function () {
  'use strict';

  function line(term, value) {
    return UI.el('div', {}, [
      UI.el('dt', { text: term }),
      UI.el('dd', { text: value || '—' })
    ]);
  }

  function statusBadge(status) {
    const labels = { active: 'Active', inactive: 'Deactivated', archived: 'Removed' };
    const classes = { active: 'badge-confirmed', inactive: 'badge-cancelled',
                      archived: 'badge-neutral' };
    return UI.el('span', { class: 'badge ' + (classes[status] || 'badge-neutral'),
                           text: labels[status] || status });
  }

  const WEEK = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday',
                'Saturday', 'Sunday'];

  /* Mon-Sat reads better in a table column than six comma-separated days,
     and runs that are not consecutive stay separate (Mon-Wed, Fri). */
  function dayRanges(days) {
    const ordered = WEEK.filter(function (day) { return days.indexOf(day) !== -1; });
    const runs = [];
    ordered.forEach(function (day) {
      const last = runs[runs.length - 1];
      if (last && WEEK.indexOf(day) === WEEK.indexOf(last[last.length - 1]) + 1) {
        last.push(day);
      } else {
        runs.push([day]);
      }
    });
    return runs.map(function (run) {
      const first = run[0].slice(0, 3);
      return run.length > 1 ? first + '–' + run[run.length - 1].slice(0, 3) : first;
    }).join(', ');
  }

  function hoursText(availability) {
    if (!availability.working_days || !availability.working_days.length) {
      return 'No working days set';
    }
    const span = availability.start_time && availability.end_time
      ? ' ' + UI.formatTime(availability.start_time) + '–' + UI.formatTime(availability.end_time)
      : '';
    return dayRanges(availability.working_days) + span;
  }

  /* --------------------------------------------------------- profile ---- */
  function profileTab(detail) {
    const doctor = detail.doctor;
    const clinic = detail.clinic || {};
    return UI.el('div', { class: 'stack-16' }, [
      UI.el('div', { class: 'detail-identity' }, [
        UI.avatar(doctor.name, true, doctor.photo),
        UI.el('div', {}, [
          UI.el('h4', { text: doctor.name }),
          UI.el('p', { class: 'muted', text: doctor.specialization || 'Doctor' }),
          UI.el('div', { class: 'row gap-8 mt-8' }, [
            statusBadge(doctor.status),
            UI.el('span', { class: 'badge badge-neutral', text: doctor.doctor_id })
          ])
        ])
      ]),
      doctor.about ? UI.el('p', { class: 'muted', text: doctor.about }) : null,
      UI.el('dl', { class: 'detail-list' }, [
        line('Qualifications', doctor.qualification),
        line('Experience', doctor.experience_years
          ? doctor.experience_years + ' years' : ''),
        line('Consultation fee', doctor.fee ? UI.money(doctor.fee) : ''),
        line('Phone', doctor.phone),
        line('Email', doctor.email),
        line('Clinic', clinic.name),
        line('Address', [clinic.address, clinic.city].filter(Boolean).join(', ')),
        line('Clinic phone', clinic.phone)
      ])
    ]);
  }

  /* ---------------------------------------------------- availability ---- */
  function availabilityTab(detail, refresh) {
    const doctorId = detail.doctor.doctor_id;
    const working = detail.schedule.filter(function (day) { return day.available; });

    const weekList = working.length
      ? UI.el('ul', { class: 'plain-list' }, working.map(function (day) {
        return UI.el('li', { class: 'week-line' }, [
          UI.el('strong', { text: day.day }),
          UI.el('span', {
            text: day.sessions.map(function (session) {
              return UI.formatTime(session.start) + ' - ' + UI.formatTime(session.end);
            }).join(', ')
          })
        ]);
      }))
      : UI.el('p', {
        class: 'muted small',
        text: 'No working days are set, so the assistant cannot offer this '
          + 'doctor any appointment. Edit the doctor to add working hours.'
      });

    const leaveWrap = UI.el('div', {});

    function paintLeave(rows) {
      UI.clear(leaveWrap);
      if (!rows.length) {
        leaveWrap.appendChild(UI.el('p', { class: 'muted small',
                                           text: 'No blocked dates.' }));
        return;
      }
      leaveWrap.appendChild(UI.el('ul', { class: 'plain-list' }, rows.map(function (row) {
        const cancelled = (detail.appointments.on_leave_dates || {})[row.date] || 0;
        return UI.el('li', { class: 'week-line' }, [
          UI.el('div', {}, [
            UI.el('strong', { text: UI.formatDate(row.date) }),
            row.reason ? UI.el('span', { class: 'muted small', text: ' ' + row.reason }) : null,
            cancelled
              ? UI.el('span', { class: 'muted small',
                                text: ' · ' + cancelled + ' appointment'
                                  + (cancelled === 1 ? '' : 's') + ' cancelled' })
              : null
          ]),
          UI.el('button', {
            class: 'btn btn-ghost btn-sm', type: 'button',
            onClick: function () { reopen(row.date); }
          }, 'Re-open')
        ]);
      })));
    }

    function loadLeave() {
      AdminStore.getLeave(doctorId).then(paintLeave).catch(function (error) {
        UI.mount(leaveWrap, UI.apiError(error, loadLeave));
      });
    }

    function reopen(date) {
      UI.confirm({
        title: 'Re-open ' + UI.formatDate(date) + '?',
        message: 'New bookings will be accepted again. Appointments cancelled '
          + 'earlier stay cancelled - those patients were told to rebook.',
        confirmLabel: 'Re-open the day'
      }).then(function (confirmed) {
        if (!confirmed) { return; }
        AdminStore.removeLeave(doctorId, date).then(function () {
          UI.toast('That date is open again');
          loadLeave();
          if (refresh) { refresh(); }
        }).catch(function (error) {
          UI.toast(error.message || 'Could not re-open that date.', 'error');
        });
      });
    }

    function blockDates() {
      const start = UI.el('input', { class: 'input', type: 'date', id: 'leave-start',
                                     name: 'start', 'data-autofocus': '' });
      const end = UI.el('input', { class: 'input', type: 'date', id: 'leave-end', name: 'end' });
      const reason = UI.el('input', { class: 'input', type: 'text', id: 'leave-reason',
                                      name: 'reason', placeholder: 'Conference, leave, emergency' });
      /* What this would cancel, shown before anything is written. */
      const impact = UI.el('div', { class: 'impact' }, UI.el('p', {
        class: 'muted small',
        text: 'Choose the dates to see which appointments would be cancelled.'
      }));
      let previewTimer = null;

      function preview() {
        clearTimeout(previewTimer);
        if (!start.value) { return; }
        if (end.value && end.value < start.value) { return; }
        previewTimer = setTimeout(function () {
          UI.mount(impact, UI.el('p', { class: 'muted small', text: 'Checking...' }));
          AdminStore.previewLeave(doctorId, {
            startDate: start.value, endDate: end.value || start.value
          }).then(function (data) {
            if (!data.appointments.length) {
              UI.mount(impact, UI.el('p', {
                class: 'muted small',
                text: 'Nothing is booked on those days, so no patient is affected.'
              }));
              return;
            }
            UI.mount(impact, [
              UI.el('p', { class: 'impact-head' }, [
                UI.icon('alert', 'icon-sm'),
                UI.el('strong', {
                  text: data.appointments.length + ' appointment'
                    + (data.appointments.length === 1 ? '' : 's') + ' will be cancelled'
                }),
                UI.el('span', {
                  class: 'muted small',
                  text: data.patients + ' patient' + (data.patients === 1 ? '' : 's')
                    + ' will be messaged'
                })
              ]),
              UI.el('ul', { class: 'plain-list' },
                data.appointments.slice(0, 6).map(function (appointment) {
                  return UI.el('li', { class: 'week-line' }, [
                    UI.el('span', {
                      text: UI.formatDateShort(appointment.date) + ' · '
                        + UI.formatTime(appointment.time)
                    }),
                    UI.el('span', { class: 'muted small',
                                    text: appointment.patient_name })
                  ]);
                })),
              data.appointments.length > 6
                ? UI.el('p', { class: 'muted small',
                               text: 'and ' + (data.appointments.length - 6) + ' more' })
                : null
            ]);
          }).catch(function (error) {
            UI.mount(impact, UI.el('p', { class: 'error-text',
                                          text: error.message || 'Could not check those dates.' }));
          });
        }, 200);
      }

      start.addEventListener('change', preview);
      end.addEventListener('change', preview);

      const form = UI.el('form', { id: 'leave-form', novalidate: true, class: 'stack' }, [
        UI.el('div', { class: 'field-grid' }, [
          UI.el('div', { class: 'field' }, [
            UI.el('label', { for: 'leave-start', text: 'First day' }), start,
            UI.el('p', { class: 'error-text' })
          ]),
          UI.el('div', { class: 'field' }, [
            UI.el('label', { for: 'leave-end', text: 'Last day (optional)' }), end,
            UI.el('p', { class: 'error-text' })
          ])
        ]),
        UI.el('div', { class: 'field' }, [
          UI.el('label', { for: 'leave-reason', text: 'Reason' }), reason,
          UI.el('p', { class: 'error-text' })
        ]),
        impact,
        UI.el('p', {
          class: 'muted small',
          text: 'Cancelled appointments stay in the history, and each patient '
            + 'is queued a message telling them to book again.'
        })
      ]);

      const submit = UI.el('button', { class: 'btn btn-danger', type: 'submit',
                                       form: 'leave-form' }, 'Block the dates');
      const dialog = UI.modal({
        size: 'sm',
        title: 'Block dates for ' + detail.doctor.name,
        body: [form],
        footer: [
          UI.el('button', {
            class: 'btn btn-secondary', type: 'button',
            onClick: function () { dialog.close(); }
          }, 'Cancel'),
          submit
        ]
      });

      form.addEventListener('submit', function (event) {
        event.preventDefault();
        UI.clearErrors(form);
        if (!start.value) {
          UI.fieldError(start, 'Choose the first day.');
          return;
        }
        if (end.value && end.value < start.value) {
          UI.fieldError(end, 'The last day must be on or after the first day.');
          return;
        }
        UI.setButtonLoading(submit, true);
        AdminStore.addLeave(doctorId, {
          startDate: start.value,
          endDate: end.value || start.value,
          reason: reason.value.trim() || 'Doctor unavailable'
        }).then(function (result) {
          UI.setButtonLoading(submit, false);
          dialog.close();
          UI.toast(result.cancelled_appointments
            ? result.blocked_dates.length + ' date(s) blocked, '
              + result.cancelled_appointments + ' appointment(s) cancelled, '
              + result.notifications_queued + ' patient message(s) queued'
            : result.blocked_dates.length + ' date(s) blocked');
          loadLeave();
          if (refresh) { refresh(); }
        }).catch(function (error) {
          UI.setButtonLoading(submit, false);
          UI.toast(error.message || 'Those dates could not be blocked.', 'error');
        });
      });
    }

    loadLeave();

    return UI.el('div', { class: 'stack-16' }, [
      UI.el('section', {}, [
        UI.el('h4', { text: 'Working week' }),
        weekList
      ]),
      UI.el('section', {}, [
        UI.el('div', { class: 'row space-between' }, [
          UI.el('h4', { text: 'Blocked dates' }),
          UI.el('button', {
            class: 'btn btn-secondary btn-sm', type: 'button', onClick: blockDates
          }, [UI.icon('ban', 'icon-sm'), 'Block dates'])
        ]),
        leaveWrap
      ])
    ]);
  }


  /* ------------------------------------------------------- credentials -- */
  function credentialsTab(detail, onChanged) {
    const doctorId = detail.doctor.doctor_id;
    const panel = UI.el('div', { class: 'stack-16' }, UI.loading('Loading sign-in details...'));

    function paint(account) {
      const usernameInput = UI.el('input', {
        class: 'input', type: 'text', id: 'cred-username', name: 'username',
        value: account.username && account.username !== doctorId ? account.username : '',
        placeholder: 'ahmed.khan', autocomplete: 'off', spellcheck: 'false'
      });
      const usernameForm = UI.el('form', { id: 'username-form', novalidate: true,
                                           class: 'stack' }, [
        UI.el('div', { class: 'field' }, [
          UI.el('label', { for: 'cred-username', text: 'Username' }),
          usernameInput,
          UI.el('p', { class: 'error-text' }),
          UI.el('p', {
            class: 'hint',
            text: 'Letters, digits, dots, dashes or underscores. They can also '
              + 'sign in with their ID, ' + doctorId + '.'
          })
        ])
      ]);
      const saveUsername = UI.el('button', {
        class: 'btn btn-secondary', type: 'submit', form: 'username-form'
      }, 'Save username');

      usernameForm.addEventListener('submit', function (event) {
        event.preventDefault();
        UI.clearErrors(usernameForm);
        const wanted = usernameInput.value.trim().toLowerCase();
        if (wanted.length < 3) {
          UI.fieldError(usernameInput, 'Use at least 3 characters.');
          return;
        }
        UI.setButtonLoading(saveUsername, true);
        AdminStore.setCredentials(doctorId, { username: wanted })
          .then(function (result) {
            UI.setButtonLoading(saveUsername, false);
            UI.toast('Username saved');
            paint(result.account);
            if (onChanged) { onChanged(); }
          })
          .catch(function (error) {
            UI.setButtonLoading(saveUsername, false);
            UI.fieldError(usernameInput, error.message || 'That username could not be saved.');
          });
      });

      const passwordInput = UI.el('input', {
        class: 'input', type: 'password', id: 'cred-password', name: 'password',
        autocomplete: 'new-password', placeholder: 'At least 8 characters'
      });
      const repeatInput = UI.el('input', {
        class: 'input', type: 'password', id: 'cred-password-2', name: 'repeat',
        autocomplete: 'new-password'
      });
      const passwordForm = UI.el('form', { id: 'password-form', novalidate: true,
                                           class: 'stack' }, [
        UI.el('div', { class: 'field-grid' }, [
          UI.el('div', { class: 'field' }, [
            UI.el('label', { for: 'cred-password', text: 'New password' }),
            passwordInput,
            UI.el('p', { class: 'error-text' })
          ]),
          UI.el('div', { class: 'field' }, [
            UI.el('label', { for: 'cred-password-2', text: 'Repeat it' }),
            repeatInput,
            UI.el('p', { class: 'error-text' })
          ])
        ]),
        UI.el('p', {
          class: 'hint',
          text: 'Give it to the doctor yourself - it is stored scrambled and '
            + 'cannot be read back from here. Setting it signs them out of any '
            + 'open tab.'
        })
      ]);
      const savePassword = UI.el('button', {
        class: 'btn btn-primary', type: 'submit', form: 'password-form'
      }, 'Set password');

      passwordForm.addEventListener('submit', function (event) {
        event.preventDefault();
        UI.clearErrors(passwordForm);
        if (passwordInput.value.trim().length < 8) {
          UI.fieldError(passwordInput, 'Use at least 8 characters.');
          return;
        }
        if (passwordInput.value !== repeatInput.value) {
          UI.fieldError(repeatInput, 'The two passwords are not the same.');
          return;
        }
        UI.setButtonLoading(savePassword, true);
        AdminStore.setCredentials(doctorId, { password: passwordInput.value })
          .then(function (result) {
            UI.setButtonLoading(savePassword, false);
            passwordInput.value = '';
            repeatInput.value = '';
            UI.toast('Password set for ' + detail.doctor.name);
            paint(result.account);
            if (onChanged) { onChanged(); }
          })
          .catch(function (error) {
            UI.setButtonLoading(savePassword, false);
            UI.fieldError(passwordInput, error.message || 'That password could not be set.');
          });
      });

      UI.mount(panel, [
        UI.el('dl', { class: 'detail-list' }, [
          line('Signs in with', account.username && account.username !== doctorId
            ? account.username + ' or ' + doctorId : doctorId),
          line('Password', account.has_password
            ? 'Set by the clinic' : 'Not set yet - the starting password still applies')
        ]),
        UI.el('section', {}, [UI.el('h4', { text: 'Username' }), usernameForm,
                              UI.el('div', { class: 'row mt-8' }, saveUsername)]),
        UI.el('section', {}, [UI.el('h4', { text: 'Password' }), passwordForm,
                              UI.el('div', { class: 'row mt-8' }, savePassword)])
      ]);
    }

    AdminStore.getCredentials(doctorId).then(paint).catch(function (error) {
      UI.mount(panel, UI.apiError(error, function () {
        AdminStore.getCredentials(doctorId).then(paint);
      }));
    });

    return panel;
  }

  /* ----------------------------------------------------- appointments --- */
  function appointmentsTab(detail) {
    const counts = detail.appointments.counts;
    const upcoming = detail.appointments.upcoming;
    const recent = detail.appointments.recent;

    function table(rows, emptyText) {
      if (!rows.length) {
        return UI.el('p', { class: 'muted small', text: emptyText });
      }
      return UI.el('div', { class: 'table-wrap' }, UI.el('table', { class: 'data' }, [
        UI.el('thead', {}, UI.el('tr', {}, [
          UI.el('th', { text: 'When' }),
          UI.el('th', { text: 'Patient' }),
          UI.el('th', { text: 'Status' })
        ])),
        UI.el('tbody', {}, rows.map(function (appointment) {
          return UI.el('tr', {}, [
            UI.el('td', { 'data-label': 'When' }, [
              UI.el('strong', { text: UI.formatDateShort(appointment.date) }),
              UI.el('span', { class: 'muted small', text: ' ' + UI.formatTime(appointment.time) })
            ]),
            UI.el('td', { 'data-label': 'Patient' }, [
              UI.el('div', { text: appointment.patient_name }),
              UI.el('span', { class: 'muted small', text: appointment.patient_phone || '' })
            ]),
            UI.el('td', { 'data-label': 'Status' }, UI.badge(appointment.status))
          ]);
        }))
      ]));
    }

    return UI.el('div', { class: 'stack-16' }, [
      UI.el('div', { class: 'mini-stats' }, [
        UI.el('div', { class: 'mini-stat' }, [
          UI.el('span', { class: 'mini-stat-value', text: String(counts.today) }),
          UI.el('span', { class: 'mini-stat-label', text: 'Today' })
        ]),
        UI.el('div', { class: 'mini-stat' }, [
          UI.el('span', { class: 'mini-stat-value', text: String(counts.upcoming) }),
          UI.el('span', { class: 'mini-stat-label', text: 'Upcoming' })
        ]),
        UI.el('div', { class: 'mini-stat' }, [
          UI.el('span', { class: 'mini-stat-value', text: String(counts.total) }),
          UI.el('span', { class: 'mini-stat-label', text: 'All time' })
        ])
      ]),
      UI.el('section', {}, [
        UI.el('h4', { text: 'Next appointments' }),
        table(upcoming, 'Nothing booked yet.')
      ]),
      UI.el('section', {}, [
        UI.el('h4', { text: 'Recently seen' }),
        table(recent, 'No past appointments.')
      ])
    ]);
  }

  /* ------------------------------------------------------------- open --- */
  function open(doctorId, options) {
    const config = options || {};
    const body = UI.el('div', {}, UI.loading('Loading doctor...'));
    const instance = UI.modal({
      size: 'lg',
      title: 'Doctor details',
      body: [body],
      footer: [
        UI.el('button', {
          class: 'btn btn-secondary', type: 'button',
          onClick: function () { instance.close(); }
        }, 'Close')
      ]
    });

    function load() {
      AdminStore.getDoctor(doctorId).then(function (detail) {
        paint(detail);
      }).catch(function (error) {
        UI.mount(body, UI.apiError(error, load));
      });
    }

    function paint(detail) {
      const heading = body.closest('.modal').querySelector('h2');
      if (heading) { heading.textContent = detail.doctor.name; }
      const panel = UI.el('div', { class: 'tab-panel' });
      const tabs = [
        { id: 'profile', label: 'Profile', build: function () { return profileTab(detail); } },
        { id: 'availability', label: 'Availability',
          build: function () { return availabilityTab(detail, config.onChanged); } },
        { id: 'appointments', label: 'Appointments',
          build: function () { return appointmentsTab(detail); } },
        { id: 'credentials', label: 'Sign-in',
          build: function () { return credentialsTab(detail, config.onChanged); } }
      ];

      const bar = UI.el('div', { class: 'tabs', role: 'tablist' });
      function select(id) {
        Array.prototype.forEach.call(bar.children, function (button) {
          const active = button.dataset.tab === id;
          button.setAttribute('aria-selected', active ? 'true' : 'false');
          button.classList.toggle('is-active', active);
        });
        UI.mount(panel, tabs.find(function (tab) { return tab.id === id; }).build());
      }

      tabs.forEach(function (tab) {
        bar.appendChild(UI.el('button', {
          class: 'tab', type: 'button', role: 'tab', 'data-tab': tab.id,
          onClick: function () { select(tab.id); }
        }, tab.label));
      });

      const edit = UI.el('button', {
        class: 'btn btn-secondary btn-sm', type: 'button',
        onClick: function () {
          instance.close();
          DoctorForm.open({
            doctor: detail.doctor,
            clinic: detail.clinic,
            schedule: detail.schedule,
            slotDuration: detail.doctor.availability.slot_duration || 20,
            specializations: config.specializations,
            onSaved: config.onChanged
          });
        }
      }, [UI.icon('edit', 'icon-sm'), 'Edit']);

      UI.mount(body, [
        UI.el('div', { class: 'row space-between' }, [bar, edit]),
        panel
      ]);
      const wanted = tabs.some(function (tab) { return tab.id === config.tab; })
        ? config.tab : 'profile';
      select(wanted);
    }

    load();
  }

  return { open: open, statusBadge: statusBadge, hoursText: hoursText,
           dayRanges: dayRanges };
})();
