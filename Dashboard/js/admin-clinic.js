/* ==========================================================================
   admin-clinic.js - the clinic.

   There is one. Every doctor works at it, so there is nothing to add, choose
   or switch between: this page edits the single record that the whole system
   reads. Change the name here and it changes in doctor profiles, appointment
   details and in what the voice assistant says, because all of
   them resolve the clinic through the same record.
   ========================================================================== */
window.Pages = window.Pages || {};

window.Pages.adminClinic = (function () {
  'use strict';

  let container = null;

  function field(id, label, value, options) {
    const config = options || {};
    const input = UI.el('input', Object.assign({
      class: 'input', type: 'text', id: id, name: config.name || id,
      value: value || '', placeholder: config.placeholder || ''
    }, config.attrs || {}));
    return {
      input: input,
      node: UI.el('div', { class: 'field' }, [
        UI.el('label', { for: id, text: label }),
        input,
        UI.el('p', { class: 'error-text' }),
        config.hint ? UI.el('p', { class: 'hint', text: config.hint }) : null
      ])
    };
  }

  /* ------------------------------------------------------------- page --- */
  function render(target) {
    container = target;
    UI.mount(container, [
      UI.el('div', { class: 'page-head' }, UI.el('div', {}, [
        UI.el('h2', { text: 'Clinic' }),
        UI.el('p', { text: 'The name and address every doctor, every appointment and the voice assistant use.' })
      ])),
      UI.el('div', { id: 'clinic-body' }, UI.skeleton(3))
    ]);
    load();
  }

  function load() {
    const body = document.getElementById('clinic-body');
    if (!body) { return; }
    AdminStore.getClinic()
      .then(function (data) { UI.mount(body, [form(data), migrationCard(data)]); })
      .catch(function (error) { UI.mount(body, UI.apiError(error, load)); });
  }

  /* ------------------------------------------------------------- form --- */
  function form(data) {
    const clinic = data.clinic || {};
    const name = field('clinic-name', 'Clinic name', clinic.name, {
      name: 'name', placeholder: 'Hamza Medical Clinic',
      hint: 'Spoken to callers when the assistant confirms an appointment.'
    });
    const address = field('clinic-address', 'Address', clinic.address, {
      name: 'address', placeholder: 'Ferozepur Road, Gulberg'
    });
    const city = field('clinic-city', 'City', clinic.city, {
      name: 'city', placeholder: 'Lahore'
    });
    const phone = field('clinic-phone', 'Clinic phone', clinic.phone, {
      name: 'phone', placeholder: '+924235551212', attrs: { type: 'tel' }
    });

    const formNode = UI.el('form', { id: 'clinic-form', novalidate: true, class: 'stack' }, [
      name.node,
      address.node,
      UI.el('div', { class: 'field-grid' }, [city.node, phone.node])
    ]);

    const save = UI.el('button', { class: 'btn btn-primary', type: 'submit',
                                   form: 'clinic-form' }, 'Save clinic details');

    formNode.addEventListener('submit', function (event) {
      event.preventDefault();
      UI.clearErrors(formNode);
      if (!name.input.value.trim()) {
        UI.fieldError(name.input, 'Enter the clinic name.');
        return;
      }
      if (!address.input.value.trim()) {
        UI.fieldError(address.input, 'Enter the clinic address.');
        return;
      }

      UI.setButtonLoading(save, true);
      AdminStore.updateClinic({
        name: name.input.value.trim(),
        address: address.input.value.trim(),
        city: city.input.value.trim(),
        phone: phone.input.value.trim()
      }).then(function () {
        UI.setButtonLoading(save, false);
        UI.toast('Clinic details saved. They now show everywhere, including '
                 + 'in what the voice assistant says.');
        load();
      }).catch(function (error) {
        UI.setButtonLoading(save, false);
        UI.toast(error.message || 'The clinic could not be saved.', 'error');
      });
    });

    return UI.el('section', { class: 'card', 'aria-labelledby': 'clinic-heading' }, [
      UI.el('div', { class: 'card-head' }, [
        UI.el('div', {}, [
          UI.el('h3', { id: 'clinic-heading', text: 'Clinic information' }),
          UI.el('p', { class: 'muted small',
                       text: data.doctors + ' doctor' + (data.doctors === 1 ? '' : 's')
                         + ' work here' })
        ])
      ]),
      UI.el('div', { class: 'card-body' }, [formNode,
                                            UI.el('div', { class: 'row mt-16' }, save)])
    ]);
  }

  /* -------------------------------------------- left over from before --- */
  /* Only appears for a database created before the clinic was centralised. */
  function migrationCard(data) {
    if (!data.needs_consolidation) { return null; }

    const move = UI.el('button', { class: 'btn btn-secondary', type: 'button' },
                       'Move everyone to this clinic');
    move.addEventListener('click', function () {
      UI.confirm({
        title: 'Move every doctor to this clinic?',
        message: 'This system has one clinic. Older records will be switched '
          + 'off and every doctor will point at this one. Nothing is deleted, '
          + 'and appointments keep their history.',
        confirmLabel: 'Move them'
      }).then(function (confirmed) {
        if (!confirmed) { return; }
        UI.setButtonLoading(move, true);
        AdminStore.consolidateClinic().then(function (result) {
          UI.setButtonLoading(move, false);
          UI.toast(result.doctors_moved.length + ' doctor(s) moved, '
                   + result.records_retired.length + ' old record(s) switched off');
          load();
        }).catch(function (error) {
          UI.setButtonLoading(move, false);
          UI.toast(error.message || 'That could not be done.', 'error');
        });
      });
    });

    const strays = data.old_records || [];
    const elsewhere = data.doctors_elsewhere || [];

    return UI.el('section', { class: 'card mt-16', 'aria-labelledby': 'tidy-heading' }, [
      UI.el('div', { class: 'card-head' },
            UI.el('h3', { id: 'tidy-heading', text: 'Older clinic records' })),
      UI.el('div', { class: 'card-body stack' }, [
        UI.el('p', {
          class: 'muted small',
          text: 'This database still has clinic records from before the system '
            + 'became single-clinic. They are not used for anything now.'
        }),
        strays.length
          ? UI.el('ul', { class: 'plain-list' }, strays.map(function (record) {
            return UI.el('li', { class: 'week-line' }, [
              UI.el('span', { text: record.name || record.clinic_id }),
              UI.el('span', { class: 'muted small', text: record.clinic_id })
            ]);
          }))
          : null,
        elsewhere.length
          ? UI.el('p', {
            class: 'muted small',
            text: elsewhere.map(function (doctor) { return doctor.name; }).join(', ')
              + (elsewhere.length === 1 ? ' is' : ' are') + ' still linked to one of them.'
          })
          : null,
        UI.el('div', { class: 'row' }, move)
      ])
    ]);
  }

  return { render: render };
})();
