/* ==========================================================================
   admin-doctor-form.js - adding and editing a doctor.

   One form for both, because the fields are the same ones stored in the
   `doctors` document the voice assistant reads. Saving writes through
   /admin/doctors, so a new doctor is bookable by the voice assistant the moment the
   form closes.

   There is no clinic field: this is a single-clinic system and every doctor
   works at that clinic, which is edited in its own section. When adding a
   doctor the form can also issue their username and password, so one step
   creates both the profile and the account.

   The working week in this form is the `schedules` collection: the hours the
   booking engine offers callers. Leave dates are handled in the doctor's
   detail view, because blocking a date cancels appointments and has to show
   what it will cancel first.
   ========================================================================== */
window.DoctorForm = (function () {
  'use strict';

  const WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday',
                    'Saturday', 'Sunday'];
  const DEFAULT_SESSION = { start: '16:00', end: '20:00' };

  /* Backend error codes -> the field that should show the message. */
  const FIELD_OF = {
    invalid_name: 'name',
    invalid_specialization: 'specialization',
    invalid_phone: 'phone',
    invalid_email: 'email',
    invalid_fee: 'fee',
    invalid_experience: 'experience_years',
    invalid_duration: 'slot_duration',
    invalid_username: 'username',
    invalid_password: 'password',
    username_taken: 'username',
    invalid_photo: null,
    photo_too_large: null
  };

  function field(label, input, hint) {
    return UI.el('div', { class: 'field' }, [
      UI.el('label', { for: input.id, text: label }),
      input,
      UI.el('p', { class: 'error-text' }),
      hint ? UI.el('p', { class: 'hint', text: hint }) : null
    ]);
  }

  function input(id, name, options) {
    return UI.el('input', Object.assign({
      class: 'input', id: id, name: name, type: 'text'
    }, options || {}));
  }

  /* --------------------------------------------------------- the photo -- */
  function photoPicker(state) {
    const preview = UI.el('span', { class: 'avatar avatar-lg', 'aria-hidden': 'true' });

    function paint() {
      UI.clear(preview);
      preview.classList.toggle('avatar-photo', !!state.photo);
      if (state.photo) { preview.appendChild(UI.el('img', { src: state.photo, alt: '' })); }
      else { preview.textContent = UI.initials(state.name || '') || '?'; }
      remove.hidden = !state.photo;
    }

    const file = UI.el('input', {
      type: 'file', id: 'doctor-photo', accept: 'image/png,image/jpeg,image/webp',
      class: 'visually-hidden'
    });
    const choose = UI.el('label', { class: 'btn btn-secondary btn-sm', for: 'doctor-photo' },
                         'Upload photo');
    const remove = UI.el('button', {
      class: 'btn btn-ghost btn-sm', type: 'button', hidden: true,
      onClick: function () { state.photo = ''; file.value = ''; paint(); }
    }, 'Remove');
    const error = UI.el('p', { class: 'error-text' });

    file.addEventListener('change', function () {
      const chosen = file.files && file.files[0];
      if (!chosen) { return; }
      error.textContent = '';
      UI.readImage(chosen, 256).then(function (dataUrl) {
        state.photo = dataUrl;
        paint();
      }).catch(function (problem) {
        error.textContent = problem.message;
        file.value = '';
      });
    });

    paint();
    return {
      node: UI.el('div', { class: 'photo-picker' }, [
        preview,
        UI.el('div', { class: 'photo-picker-actions' }, [
          UI.el('div', { class: 'row gap-8' }, [choose, remove, file]),
          UI.el('p', {
            class: 'hint',
            text: 'Square images work best. The picture is stored with the '
              + 'doctor record, so it is saved at a small size.'
          }),
          error
        ])
      ]),
      repaint: paint
    };
  }

  /* --------------------------------------------------- the working week -- */
  function weekEditor(days) {
    const rows = UI.el('div', { class: 'week-editor' });

    function timeInput(value, label, onChange) {
      const node = UI.el('input', {
        class: 'input', type: 'time', value: value || '', 'aria-label': label
      });
      node.addEventListener('change', function () { onChange(node.value); });
      return node;
    }

    days.forEach(function (day) {
      const sessions = UI.el('div', { class: 'day-sessions' });

      function paint() {
        UI.clear(sessions);
        if (!day.available) {
          sessions.appendChild(UI.el('span', { class: 'day-closed', text: 'Not working' }));
          return;
        }
        day.sessions.forEach(function (session, index) {
          sessions.appendChild(UI.el('div', { class: 'session-times' }, [
            timeInput(session.start, day.day + ' session ' + (index + 1) + ' start',
                      function (value) { session.start = value; }),
            UI.el('span', { class: 'session-sep', text: 'to' }),
            timeInput(session.end, day.day + ' session ' + (index + 1) + ' end',
                      function (value) { session.end = value; }),
            UI.el('button', {
              class: 'btn btn-ghost btn-icon', type: 'button',
              'aria-label': 'Remove ' + day.day + ' session ' + (index + 1),
              onClick: function () { day.sessions.splice(index, 1); paint(); }
            }, UI.icon('trash', 'icon-sm'))
          ]));
        });
        if (day.sessions.length < 2) {
          sessions.appendChild(UI.el('button', {
            class: 'btn btn-ghost btn-sm', type: 'button',
            onClick: function () {
              day.sessions.push(Object.assign({}, DEFAULT_SESSION));
              paint();
            }
          }, [UI.icon('plus', 'icon-sm'), 'Add hours']));
        }
      }

      const toggle = UI.el('input', {
        type: 'checkbox', id: 'day-' + day.day, checked: day.available,
        'aria-label': day.day + ': working'
      });
      toggle.addEventListener('change', function () {
        day.available = toggle.checked;
        if (day.available && !day.sessions.length) {
          day.sessions.push(Object.assign({}, DEFAULT_SESSION));
        }
        paint();
      });

      paint();
      rows.appendChild(UI.el('div', { class: 'day-row' }, [
        UI.el('div', { class: 'day-name' }, [
          UI.el('label', { class: 'switch switch-inline', for: 'day-' + day.day }, [
            toggle, UI.el('span', { class: 'switch-track' })
          ]),
          UI.el('span', { text: day.day })
        ]),
        sessions
      ]));
    });

    return rows;
  }

  function blankWeek() {
    return WEEKDAYS.map(function (day) {
      const working = day !== 'Sunday';
      return {
        day: day,
        available: working,
        sessions: working ? [Object.assign({}, DEFAULT_SESSION)] : []
      };
    });
  }

  function weekFrom(schedule) {
    const byDay = {};
    (schedule || []).forEach(function (day) { byDay[day.day] = day; });
    return WEEKDAYS.map(function (name) {
      const found = byDay[name];
      return {
        day: name,
        available: !!(found && found.available),
        sessions: (found && found.sessions ? found.sessions : []).map(function (session) {
          return { start: session.start, end: session.end };
        })
      };
    });
  }

  /* -------------------------------------------------------------- open -- */
  /* options: { doctor, clinic, schedule, onSaved } - doctor omitted = add */
  function open(options) {
    const config = options || {};
    const doctor = config.doctor || {};
    const clinic = config.clinic || {};
    const editing = !!doctor.doctor_id;
    const week = editing ? weekFrom(config.schedule) : blankWeek();
    const state = { photo: doctor.photo || '', name: doctor.name || '' };

    const nameInput = input('doctor-name', 'name', {
      value: doctor.name || '', placeholder: 'Dr Ayesha Khan', autocomplete: 'off'
    });
    const picker = photoPicker(state);
    nameInput.addEventListener('input', function () {
      state.name = nameInput.value;
      picker.repaint();
    });

    const specializationInput = input('doctor-specialization', 'specialization', {
      value: doctor.specialization || '', placeholder: 'Cardiologist',
      list: 'specialization-options'
    });
    const qualificationInput = input('doctor-qualification', 'qualification', {
      value: doctor.qualification || '', placeholder: 'MBBS, FCPS (Cardiology)'
    });
    const experienceInput = input('doctor-experience', 'experience_years', {
      type: 'number', min: '0', max: '70', value: doctor.experience_years || ''
    });
    const phoneInput = input('doctor-phone', 'phone', {
      type: 'tel', value: doctor.phone || '', placeholder: '+923001234567'
    });
    const emailInput = input('doctor-email', 'email', {
      type: 'email', value: doctor.email || '', placeholder: 'name@clinic.pk'
    });
    const feeInput = input('doctor-fee', 'fee', {
      type: 'number', min: '0', step: '50', value: doctor.fee === undefined ? '' : doctor.fee
    });
    const durationInput = input('doctor-duration', 'slot_duration', {
      type: 'number', min: '5', max: '180', step: '5',
      value: config.slotDuration || 20
    });
    const aboutInput = UI.el('textarea', {
      class: 'input', id: 'doctor-about', name: 'about', rows: 3,
      placeholder: 'What patients should know before booking.'
    });
    aboutInput.value = doctor.about || '';

    /* Sign-in details, only when adding: an existing doctor's username and
       password are changed on their Sign-in tab, one at a time. */
    const usernameInput = input('doctor-username', 'username', {
      placeholder: 'ayesha.khan', autocomplete: 'off', spellcheck: 'false'
    });
    const passwordInput = input('doctor-password', 'password', {
      type: 'password', autocomplete: 'new-password',
      placeholder: 'At least 8 characters'
    });

    const specializationList = UI.el('datalist', { id: 'specialization-options' },
      (config.specializations || []).map(function (name) {
        return UI.el('option', { value: name });
      }));

    const form = UI.el('form', { id: 'doctor-form', novalidate: true, class: 'form-sections' }, [
      UI.el('section', { class: 'form-section' }, [
        UI.el('h4', { text: 'Doctor' }),
        picker.node,
        UI.el('div', { class: 'field-grid' }, [
          field('Full name', nameInput),
          field('Specialization', specializationInput),
          field('Qualifications', qualificationInput),
          field('Years of experience', experienceInput),
          field('Phone', phoneInput),
          field('Email', emailInput),
          field('Consultation fee (PKR)', feeInput,
                'Quoted to callers by the voice assistant.')
        ]),
        field('About', aboutInput)
      ]),
      editing ? null : UI.el('section', { class: 'form-section' }, [
        UI.el('h4', { text: 'Sign-in details' }),
        UI.el('div', { class: 'field-grid' }, [
          field('Username', usernameInput,
                'Optional. They can always sign in with the ID they are given.'),
          field('Password', passwordInput,
                'Optional. Hand it over yourself - it cannot be read back.')
        ])
      ]),
      UI.el('section', { class: 'form-section' }, [
        UI.el('h4', { text: 'Working hours' }),
        UI.el('p', { class: 'hint mb-8' },
              [UI.icon('building', 'icon-sm'),
               UI.el('span', { text: ' ' + (clinic.name || 'This clinic')
                 + ' \u2014 every doctor works here. The clinic is edited in '
                 + 'the Clinic section.' })]),
        UI.el('div', { class: 'field-grid' }, [
          field('Appointment length (minutes)', durationInput,
                'How long one slot lasts.')
        ]),
        weekEditor(week)
      ]),
      specializationList
    ]);

    const save = UI.el('button', {
      class: 'btn btn-primary', type: 'submit', form: 'doctor-form'
    }, editing ? 'Save changes' : 'Add doctor');

    const instance = UI.modal({
      size: 'lg',
      title: editing ? 'Edit ' + doctor.name : 'Add a doctor',
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
      UI.clearErrors(form);

      const rules = {
        name: [UI.rule.required('Enter the doctor’s name.')],
        specialization: [UI.rule.required('Enter a specialization.')],
        phone: [UI.rule.required('Enter a contact number.')]
      };
      if (!UI.validate(form, rules)) { return; }

      if (emailInput.value.trim() && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(emailInput.value.trim())) {
        UI.fieldError(emailInput, 'Enter a valid email address.');
        return;
      }
      const fee = Number(feeInput.value);
      if (feeInput.value === '' || isNaN(fee) || fee < 0) {
        UI.fieldError(feeInput, 'Enter the consultation fee, for example 1500.');
        return;
      }
      const duration = Number(durationInput.value);
      if (isNaN(duration) || duration < 5 || duration > 180) {
        UI.fieldError(durationInput, 'An appointment must be between 5 and 180 minutes.');
        return;
      }
      if (!editing && usernameInput.value.trim()
          && !/^[a-z0-9][a-z0-9._-]{2,31}$/.test(usernameInput.value.trim().toLowerCase())) {
        UI.fieldError(usernameInput,
                      'Use 3 to 32 letters, digits, dots, dashes or underscores.');
        return;
      }
      if (!editing && passwordInput.value && passwordInput.value.trim().length < 8) {
        UI.fieldError(passwordInput, 'Use at least 8 characters.');
        return;
      }

      const broken = week.find(function (day) {
        return day.available && day.sessions.some(function (session) {
          return !session.start || !session.end || session.end <= session.start;
        });
      });
      if (broken) {
        UI.toast(broken.day + ': the finish time must be after the start time.', 'error');
        return;
      }

      const payload = {
        name: nameInput.value.trim(),
        specialization: specializationInput.value.trim(),
        qualification: qualificationInput.value.trim(),
        experience_years: experienceInput.value === '' ? 0 : Number(experienceInput.value),
        phone: phoneInput.value.trim(),
        email: emailInput.value.trim(),
        fee: fee,
        about: aboutInput.value.trim(),
        photo: state.photo,
        slot_duration: duration,
        days: week.map(function (day) {
          return {
            day: day.day,
            available: day.available,
            sessions: day.available ? day.sessions : []
          };
        })
      };

      if (!editing) {
        if (usernameInput.value.trim()) {
          payload.username = usernameInput.value.trim().toLowerCase();
        }
        if (passwordInput.value) { payload.password = passwordInput.value; }
      }

      UI.setButtonLoading(save, true);
      const request = editing
        ? AdminStore.updateDoctor(doctor.doctor_id, payload)
        : AdminStore.createDoctor(payload);

      request.then(function (result) {
        UI.setButtonLoading(save, false);
        instance.close();
        UI.toast(editing
          ? (result.doctor.name + '’s details were saved')
          : (result.doctor.name + ' was added as ' + result.doctor.doctor_id));
        (result.warnings || []).forEach(function (warning) {
          UI.toast(warning, 'info');
        });
        if (config.onSaved) { config.onSaved(result); }
      }).catch(function (error) {
        UI.setButtonLoading(save, false);
        const target = FIELD_OF[error.code];
        if (target && form.elements[target]) {
          UI.fieldError(form.elements[target], error.message);
        } else {
          UI.toast(error.message || 'The doctor could not be saved.', 'error');
        }
      });
    });
  }

  return { open: open, WEEKDAYS: WEEKDAYS };
})();
