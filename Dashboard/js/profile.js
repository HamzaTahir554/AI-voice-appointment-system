/* ==========================================================================
   profile.js - doctor profile and clinic information.

   Reads and writes the `doctors` and `clinics` documents through the
   backend. The voice assistant reads the same records: the fee it quotes and
   the address it reads out change as soon as this form is saved.
   ========================================================================== */
window.Pages = window.Pages || {};

window.Pages.profile = (function () {
  'use strict';

  function factRow(label, value) {
    return UI.el('div', {}, [
      UI.el('dt', { text: label }),
      UI.el('dd', { text: (value === 0 || value) ? String(value) : 'Not provided' })
    ]);
  }

  function render(container) {
    UI.mount(container, UI.loading('Loading profile...'));

    Store.getProfile().then(function (data) {
      const doctor = data.doctor || {};
      const clinic = data.clinic || {};

      UI.mount(container, [
        UI.el('div', { class: 'page-head' }, [
          UI.el('div', {}, [
            UI.el('h2', { text: 'Doctor Profile' }),
            UI.el('p', { text: 'What the voice assistant tells patients about you and your clinic.' })
          ]),
          UI.el('button', {
            class: 'btn btn-primary', type: 'button',
            onClick: function () { openEdit(doctor, clinic, container); }
          }, [UI.icon('edit', 'icon-sm'), 'Edit Profile'])
        ]),

        UI.el('section', { class: 'card' }, [
          UI.el('div', { class: 'profile-hero' }, [
            UI.avatar(doctor.name || 'Doctor', true),
            UI.el('div', { class: 'profile-hero-main' }, [
              UI.el('h2', { text: doctor.name || 'Doctor' }),
              UI.el('div', { class: 'role', text: doctor.specialization || 'Specialization not set' }),
              UI.el('div', {
                class: 'meta',
                text: (doctor.qualification || 'Qualifications not set')
                  + '  |  ' + (doctor.experience_years || 0) + ' years experience'
                  + '  |  ' + doctor.doctor_id
              })
            ]),
            UI.el('div', { class: 'fee-tag' }, [
              UI.el('strong', { text: UI.money(doctor.fee) }),
              UI.el('span', { text: 'Consultation fee' })
            ])
          ])
        ]),

        UI.el('div', { class: 'split-even' }, [
          UI.el('section', { class: 'card', 'aria-labelledby': 'contact-heading' }, [
            UI.el('div', { class: 'card-head' }, UI.el('h3', { id: 'contact-heading', text: 'Contact details' })),
            UI.el('div', { class: 'card-body' }, [
              UI.el('dl', { class: 'detail-list' }, [
                factRow('Doctor ID', doctor.doctor_id),
                factRow('Phone', doctor.phone),
                factRow('Specialization', doctor.specialization),
                factRow('Qualifications', doctor.qualification),
                factRow('Experience', (doctor.experience_years || 0) + ' years')
              ])
            ])
          ]),
          UI.el('section', { class: 'card', 'aria-labelledby': 'clinic-heading' }, [
            UI.el('div', { class: 'card-head' }, UI.el('h3', { id: 'clinic-heading', text: 'Clinic information' })),
            UI.el('div', { class: 'card-body' }, [
              UI.el('dl', { class: 'detail-list' }, [
                factRow('Clinic', clinic.name),
                factRow('Address', clinic.address),
                factRow('City', clinic.city),
                factRow('Clinic phone', clinic.phone),
                factRow('Consultation fee', UI.money(doctor.fee))
              ]),
              UI.el('p', {
                class: 'hint',
                text: 'Shared by everyone at this practice and kept up to date '
                  + 'by your clinic administrator.'
              })
            ])
          ])
        ]),

        UI.el('section', { class: 'card', 'aria-labelledby': 'about-heading' }, [
          UI.el('div', { class: 'card-head' }, UI.el('h3', { id: 'about-heading', text: 'About' })),
          UI.el('div', { class: 'card-body' },
            UI.el('p', { class: 'muted', text: doctor.about || 'No description added yet.' }))
        ])
      ]);
    }).catch(function (error) {
      UI.mount(container, UI.apiError(error, function () { render(container); }));
    });
  }

  function field(id, label, value, options) {
    const config = options || {};
    return UI.el('div', { class: 'field' }, [
      UI.el('label', { for: id, text: label }),
      config.textarea
        ? UI.el('textarea', { class: 'textarea', id: id, name: config.name || id, text: value || '' })
        : UI.el('input', {
          class: 'input', id: id, name: config.name || id,
          value: (value === 0 || value) ? value : '',
          type: config.type || 'text', 'data-autofocus': config.autofocus
        }),
      UI.el('p', { class: 'error-text' })
    ]);
  }

  function openEdit(doctor, clinic, container) {
    const form = UI.el('form', { class: 'stack', novalidate: true, id: 'profile-form' }, [
      UI.el('h4', { text: 'Doctor' }),
      UI.el('div', { class: 'form-grid' }, [
        field('pf-name', 'Full name', doctor.name, { name: 'name', autofocus: true }),
        field('pf-spec', 'Specialization', doctor.specialization, { name: 'specialization' }),
        field('pf-qual', 'Qualifications', doctor.qualification, { name: 'qualification' }),
        field('pf-exp', 'Years of experience', doctor.experience_years, { name: 'experience_years', type: 'number' }),
        field('pf-phone', 'Phone', doctor.phone, { name: 'phone' }),
        field('pf-fee', 'Consultation fee (Rs.)', doctor.fee, { name: 'fee', type: 'number' })
      ]),
      field('pf-about', 'About', doctor.about, { name: 'about', textarea: true }),
      UI.el('hr', { class: 'divider' }),
      UI.el('h4', { text: 'Clinic' }),
      /* One clinic, shared by everyone who works here: the administrator
         sets it, so it is shown rather than edited. */
      UI.el('div', { class: 'readonly-block' }, [
        UI.el('dl', { class: 'detail-list' }, [
          UI.el('div', {}, [
            UI.el('dt', { text: 'Clinic' }),
            UI.el('dd', { text: clinic.name || '—' })
          ]),
          UI.el('div', {}, [
            UI.el('dt', { text: 'Address' }),
            UI.el('dd', { text: [clinic.address, clinic.city].filter(Boolean).join(', ') || '—' })
          ]),
          UI.el('div', {}, [
            UI.el('dt', { text: 'Clinic phone' }),
            UI.el('dd', { text: clinic.phone || '—' })
          ])
        ]),
        UI.el('p', {
          class: 'hint',
          text: 'Everyone at this practice shares these details, so your clinic '
            + 'administrator keeps them up to date.'
        })
      ])
    ]);

    const save = UI.el('button', { class: 'btn btn-primary', type: 'submit', form: 'profile-form' }, 'Save Changes');

    const instance = UI.modal({
      size: 'lg',
      title: 'Edit profile',
      subtitle: 'Saved to the clinic database and used by the voice assistant.',
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
      const rules = {
        name: [UI.rule.required('Doctor name is required.')],
        specialization: [UI.rule.required('Specialization is required.')],
        qualification: [UI.rule.required('Qualifications are required.')],
        experience_years: [
          UI.rule.required('Experience is required.'),
          UI.rule.number('Experience must be a number of years.')
        ],
        phone: [UI.rule.required('Phone number is required.')],
        fee: [UI.rule.required('Consultation fee is required.'), UI.rule.number('Fee must be a valid number.')]
      };

      if (!UI.validate(form, rules)) { return; }

      const values = form.elements;
      const patch = {
        name: values.name.value.trim(),
        specialization: values.specialization.value.trim(),
        qualification: values.qualification.value.trim(),
        experience_years: Number(values.experience_years.value),
        phone: values.phone.value.trim(),
        fee: Number(values.fee.value),
        about: values.about.value.trim()
      };

      UI.setButtonLoading(save, true);
      Store.updateDoctorProfile(patch).then(function (data) {
        UI.setButtonLoading(save, false);
        instance.close();
        Nav.setDoctor(data.doctor);
        UI.toast('Profile saved');
        render(container);
      }).catch(function (error) {
        UI.setButtonLoading(save, false);
        UI.toast(error.message || 'The profile could not be saved.', 'error');
      });
    });
  }

  return { render: render };
})();
