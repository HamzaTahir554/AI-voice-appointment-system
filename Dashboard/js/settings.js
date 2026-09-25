/* ==========================================================================
   settings.js - account, alerts, appearance and security.

   Only what the system can really do appears here. There is no password
   form, because passwords are issued by the clinic administrator and cannot
   be changed from a browser; a form that changed nothing would be a lie.

   The alert switches are not decoration either: app.js compares today's
   diary on every refresh and raises exactly the alerts that are switched on
   here, and the choice is stored with the doctor's record.
   ========================================================================== */
window.Pages = window.Pages || {};

window.Pages.settings = (function () {
  'use strict';

  const ALERTS = [
    { key: 'new_appointments', label: 'New appointments',
      hint: 'When the voice assistant books someone into today’s diary.' },
    { key: 'cancellations', label: 'Cancellations',
      hint: 'When a patient cancels an appointment for today.' },
    { key: 'reschedules', label: 'Changed times',
      hint: 'When a patient moves an appointment to another time.' }
  ];

  /* ------------------------------------------------------------- page --- */
  function render(container) {
    if (Nav.isAdmin()) {
      renderAdmin(container);
      return;
    }

    UI.mount(container, UI.loading('Loading settings...'));
    Store.getProfile().then(function (data) {
      UI.mount(container, [
        head('Your account, alerts and how the dashboard looks.'),
        UI.el('div', { class: 'settings-grid' }, [
          accountCard(data.doctor || {}, data.account || {}),
          passwordCard(),
          alertsCard(data.doctor || {}),
          appearanceCard(),
          securityCard(data.doctor || {}, data.account || {})
        ])
      ]);
    }).catch(function (error) {
      UI.mount(container, UI.apiError(error, function () { render(container); }));
    });
  }

  function renderAdmin(container) {
    const account = Nav.account() || {};
    UI.mount(container, [
      head('How the dashboard looks, and your session.'),
      UI.el('div', { class: 'settings-grid' }, [
        UI.el('section', { class: 'card', 'aria-labelledby': 'admin-account-heading' }, [
          UI.el('div', { class: 'card-head' },
                UI.el('h3', { id: 'admin-account-heading', text: 'Account' })),
          UI.el('div', { class: 'card-body' }, [
            UI.el('dl', { class: 'detail-list' }, [
              row('Signed in as', account.account_id || 'Administrator'),
              row('Role', 'Clinic administrator'),
              row('Can manage', 'Doctors, their clinics, working hours and leave')
            ]),
            UI.el('p', {
              class: 'muted small mt-12',
              text: 'This account manages the doctors, their sign-in details, '
                + 'the clinics and every appointment.'
            })
          ])
        ]),
        passwordCard(),
        appearanceCard(),
        securityCard(null, account)
      ])
    ]);
  }

  function head(subtitle) {
    return UI.el('div', { class: 'page-head' }, UI.el('div', {}, [
      UI.el('h2', { text: 'Settings' }),
      UI.el('p', { text: subtitle })
    ]));
  }

  function row(term, value) {
    return UI.el('div', {}, [
      UI.el('dt', { text: term }),
      UI.el('dd', { text: value })
    ]);
  }

  /* ---------------------------------------------------------- account -- */
  function accountCard(doctor, account) {
    const state = { photo: doctor.photo || '', name: doctor.name || '' };
    const preview = UI.el('span', { class: 'avatar avatar-lg', 'aria-hidden': 'true' });
    const fileInput = UI.el('input', {
      type: 'file', id: 'account-photo', class: 'visually-hidden',
      accept: 'image/png,image/jpeg,image/webp'
    });
    const removePhoto = UI.el('button', {
      class: 'btn btn-ghost btn-sm', type: 'button', hidden: !state.photo,
      onClick: function () { state.photo = ''; fileInput.value = ''; paintPhoto(); }
    }, 'Remove');
    const photoError = UI.el('p', { class: 'error-text' });

    function paintPhoto() {
      UI.clear(preview);
      preview.classList.toggle('avatar-photo', !!state.photo);
      if (state.photo) { preview.appendChild(UI.el('img', { src: state.photo, alt: '' })); }
      else { preview.textContent = UI.initials(state.name) || '?'; }
      removePhoto.hidden = !state.photo;
    }

    fileInput.addEventListener('change', function () {
      const chosen = fileInput.files && fileInput.files[0];
      if (!chosen) { return; }
      photoError.textContent = '';
      UI.readImage(chosen, 256).then(function (dataUrl) {
        state.photo = dataUrl;
        paintPhoto();
      }).catch(function (problem) {
        photoError.textContent = problem.message;
        fileInput.value = '';
      });
    });
    paintPhoto();

    const nameInput = UI.el('input', {
      class: 'input', type: 'text', id: 'ac-name', name: 'name', value: doctor.name || ''
    });
    nameInput.addEventListener('input', function () {
      state.name = nameInput.value;
      paintPhoto();
    });

    const form = UI.el('form', { class: 'stack', novalidate: true, id: 'account-form' }, [
      UI.el('div', { class: 'photo-picker' }, [
        preview,
        UI.el('div', { class: 'photo-picker-actions' }, [
          UI.el('div', { class: 'row gap-8' }, [
            UI.el('label', { class: 'btn btn-secondary btn-sm', for: 'account-photo' },
                  'Upload photo'),
            removePhoto,
            fileInput
          ]),
          UI.el('p', { class: 'hint', text: 'Shown beside your name in this dashboard.' }),
          photoError
        ])
      ]),
      UI.el('div', { class: 'field-grid' }, [
        UI.el('div', { class: 'field' }, [
          UI.el('label', { for: 'ac-name', text: 'Name' }),
          nameInput,
          UI.el('p', { class: 'error-text' })
        ]),
        UI.el('div', { class: 'field' }, [
          UI.el('label', { for: 'ac-email', text: 'Email' }),
          UI.el('input', { class: 'input', type: 'email', id: 'ac-email', name: 'email',
                           value: doctor.email || '', placeholder: 'name@clinic.pk' }),
          UI.el('p', { class: 'error-text' })
        ]),
        UI.el('div', { class: 'field' }, [
          UI.el('label', { for: 'ac-phone', text: 'Contact phone' }),
          UI.el('input', { class: 'input', type: 'tel', id: 'ac-phone', name: 'phone',
                           value: doctor.phone || '' }),
          UI.el('p', { class: 'error-text' })
        ]),
        UI.el('div', { class: 'field' }, [
          UI.el('label', { for: 'ac-username', text: 'Username' }),
          UI.el('input', { class: 'input', type: 'text', id: 'ac-username',
                           value: (account && account.username) || doctor.doctor_id || '',
                           readonly: true }),
          UI.el('p', {
            class: 'hint',
            text: 'You sign in with this, or with your ID '
              + (doctor.doctor_id || '')
              + '. Your clinic administrator sets the username.'
          })
        ])
      ]),
      UI.el('p', {
        class: 'muted small',
        text: 'Your specialization, fee and clinic details are on the Doctor '
          + 'Profile page - those are what the assistant tells callers.'
      })
    ]);

    const save = UI.el('button', { class: 'btn btn-primary', type: 'submit',
                                   form: 'account-form' }, 'Save changes');

    form.addEventListener('submit', function (event) {
      event.preventDefault();
      UI.clearErrors(form);
      const valid = UI.validate(form, {
        name: [UI.rule.required('Your name is required.')],
        phone: [UI.rule.required('A contact number is required.')]
      });
      if (!valid) { return; }
      const email = form.elements.email.value.trim();
      if (email && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
        UI.fieldError(form.elements.email, 'Enter a valid email address.');
        return;
      }

      UI.setButtonLoading(save, true);
      Store.updateDoctorProfile({
        name: form.elements.name.value.trim(),
        email: email,
        phone: form.elements.phone.value.trim(),
        photo: state.photo
      }).then(function (data) {
        UI.setButtonLoading(save, false);
        Nav.setDoctor(data.doctor);
        UI.toast('Your account was updated');
      }).catch(function (error) {
        UI.setButtonLoading(save, false);
        UI.toast(error.message || 'Could not save the change.', 'error');
      });
    });

    return UI.el('section', { class: 'card', 'aria-labelledby': 'account-heading' }, [
      UI.el('div', { class: 'card-head' },
            UI.el('h3', { id: 'account-heading', text: 'Account' })),
      UI.el('div', { class: 'card-body' }, [form, UI.el('div', { class: 'row mt-16' }, save)])
    ]);
  }

  /* ----------------------------------------------------------- alerts -- */
  function alertsCard(doctor) {
    const prefs = Object.assign({ new_appointments: true, cancellations: true,
                                  reschedules: true }, doctor.notification_prefs || {});
    const switches = {};

    const rows = ALERTS.map(function (alert) {
      const box = UI.el('input', {
        type: 'checkbox', id: 'pref-' + alert.key, checked: prefs[alert.key] !== false,
        'aria-label': alert.label
      });
      switches[alert.key] = box;
      box.addEventListener('change', save);
      return UI.el('div', { class: 'pref-row' }, [
        UI.el('div', {}, [
          UI.el('strong', { text: alert.label }),
          UI.el('p', { class: 'muted small', text: alert.hint })
        ]),
        UI.el('label', { class: 'switch switch-inline', for: 'pref-' + alert.key }, [
          box, UI.el('span', { class: 'switch-track' })
        ])
      ]);
    });

    let saving = false;
    function save() {
      if (saving) { return; }
      saving = true;
      const payload = {};
      Object.keys(switches).forEach(function (key) {
        payload[key] = switches[key].checked;
      });
      Store.updateDoctorProfile({ notification_prefs: payload })
        .then(function () { saving = false; UI.toast('Alert settings saved'); })
        .catch(function (error) {
          saving = false;
          UI.toast(error.message || 'Could not save that setting.', 'error');
        });
    }

    return UI.el('section', { class: 'card', 'aria-labelledby': 'alerts-heading' }, [
      UI.el('div', { class: 'card-head' }, UI.el('div', {}, [
        UI.el('h3', { id: 'alerts-heading', text: 'Alerts' }),
        UI.el('p', { class: 'muted small',
                     text: 'Shown while this dashboard is open.' })
      ])),
      UI.el('div', { class: 'card-body stack' }, rows.concat([
        UI.el('p', {
          class: 'muted small',
          text: 'Messages to patients are a separate thing: the system queues '
            + 'them when you block a day, and the Notifications page shows '
            + 'that queue.'
        })
      ]))
    ]);
  }

  /* --------------------------------------------------------- password -- */
  /* Everyone may change their own password and nobody else's: the server
     asks for the current one first. A doctor's USERNAME is not editable
     here - only the clinic administrator can change that. */
  function passwordCard() {
    const current = UI.el('input', {
      class: 'input', type: 'password', id: 'pw-current', name: 'current',
      autocomplete: 'current-password'
    });
    const next = UI.el('input', {
      class: 'input', type: 'password', id: 'pw-new', name: 'next',
      autocomplete: 'new-password', placeholder: 'At least 8 characters'
    });
    const repeat = UI.el('input', {
      class: 'input', type: 'password', id: 'pw-repeat', name: 'repeat',
      autocomplete: 'new-password'
    });

    const form = UI.el('form', { class: 'stack', novalidate: true, id: 'password-form' }, [
      UI.el('div', { class: 'field' }, [
        UI.el('label', { for: 'pw-current', text: 'Current password' }),
        current,
        UI.el('p', { class: 'error-text' })
      ]),
      UI.el('div', { class: 'field-grid' }, [
        UI.el('div', { class: 'field' }, [
          UI.el('label', { for: 'pw-new', text: 'New password' }),
          next,
          UI.el('p', { class: 'error-text' })
        ]),
        UI.el('div', { class: 'field' }, [
          UI.el('label', { for: 'pw-repeat', text: 'Repeat it' }),
          repeat,
          UI.el('p', { class: 'error-text' })
        ])
      ])
    ]);

    const save = UI.el('button', { class: 'btn btn-primary', type: 'submit',
                                   form: 'password-form' }, 'Change password');

    form.addEventListener('submit', function (event) {
      event.preventDefault();
      UI.clearErrors(form);
      if (!current.value) {
        UI.fieldError(current, 'Enter your current password.');
        return;
      }
      if (next.value.trim().length < 8) {
        UI.fieldError(next, 'Use at least 8 characters.');
        return;
      }
      if (next.value !== repeat.value) {
        UI.fieldError(repeat, 'The two passwords are not the same.');
        return;
      }
      if (next.value === current.value) {
        UI.fieldError(next, 'Choose a different password.');
        return;
      }

      UI.setButtonLoading(save, true);
      Store.changePassword(current.value, next.value).then(function (result) {
        UI.setButtonLoading(save, false);
        form.reset();
        UI.toast(result.other_sessions_ended
          ? 'Password changed. Other tabs were signed out.'
          : 'Password changed');
      }).catch(function (error) {
        UI.setButtonLoading(save, false);
        if (error.code === 'wrong_password') {
          UI.fieldError(current, error.message);
        } else {
          UI.fieldError(next, error.message || 'The password could not be changed.');
        }
      });
    });

    return UI.el('section', { class: 'card', 'aria-labelledby': 'password-heading' }, [
      UI.el('div', { class: 'card-head' },
            UI.el('h3', { id: 'password-heading', text: 'Password' })),
      UI.el('div', { class: 'card-body' },
            [form, UI.el('div', { class: 'row mt-16' }, save)])
    ]);
  }

  /* ------------------------------------------------------- appearance -- */
  function appearanceCard() {
    const choices = [
      { id: 'light', label: 'Light', description: 'Bright clinic display' },
      { id: 'dark', label: 'Dark', description: 'Easier at night' },
      { id: 'system', label: 'System', description: 'Follow this device' }
    ];

    const buttons = choices.map(function (choice) {
      const button = UI.el('button', {
        class: 'theme-option', type: 'button',
        'aria-pressed': Theme.preference() === choice.id ? 'true' : 'false',
        onClick: function () {
          Theme.apply(choice.id);
          buttons.forEach(function (other, index) {
            other.setAttribute('aria-pressed',
                               choices[index].id === choice.id ? 'true' : 'false');
          });
        }
      }, [
        UI.el('span', { class: 'theme-swatch theme-swatch-' + choice.id, 'aria-hidden': 'true' }),
        UI.el('strong', { text: choice.label }),
        UI.el('span', { class: 'small muted', text: choice.description })
      ]);
      return button;
    });

    return UI.el('section', { class: 'card', 'aria-labelledby': 'appearance-heading' }, [
      UI.el('div', { class: 'card-head' },
            UI.el('h3', { id: 'appearance-heading', text: 'Appearance' })),
      UI.el('div', { class: 'card-body' },
            UI.el('div', { class: 'theme-options' }, buttons))
    ]);
  }

  /* --------------------------------------------------------- security -- */
  function securityCard(doctor, account) {
    return UI.el('section', { class: 'card', 'aria-labelledby': 'security-heading' }, [
      UI.el('div', { class: 'card-head' },
            UI.el('h3', { id: 'security-heading', text: 'Security' })),
      UI.el('div', { class: 'card-body stack' }, [
        UI.el('dl', { class: 'detail-list' }, [
          row('Signed in as', (account && account.username)
            || (doctor && doctor.doctor_id) || 'Administrator'),
          row('This session', 'Ends when you log out or close this tab')
        ]),
        UI.el('p', {
          class: 'muted small',
          text: doctor
            ? 'Your username is set by the clinic administrator; your password '
              + 'is yours to change above.'
            : 'Doctor passwords are set on each doctor’s Sign-in tab.'
        }),
        UI.el('button', {
          class: 'btn btn-secondary', type: 'button',
          onClick: function () { App.logout(); }
        }, [UI.icon('logout', 'icon-sm'), 'Log out'])
      ])
    ]);
  }

  return { render: render };
})();
