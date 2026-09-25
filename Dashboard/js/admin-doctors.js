/* ==========================================================================
   admin-doctors.js - the doctor register.

   A table of who works at the clinic, what they charge, when they work and
   whether callers can currently book them. Everything on this page comes
   from /admin/doctors; the switches change the same `active` flag the
   booking rules and the voice assistant's doctor matcher already read.

   Removing a doctor archives the record. The appointments, patients and
   messages that point at it are kept, and it can be restored.
   ========================================================================== */
window.Pages = window.Pages || {};

window.Pages.adminDoctors = (function () {
  'use strict';

  const STATUS_FILTERS = [
    { id: 'listed', label: 'All current' },
    { id: 'active', label: 'Active' },
    { id: 'inactive', label: 'Deactivated' },
    { id: 'archived', label: 'Removed' }
  ];

  let filters = { q: '', status: 'listed', specialization: '' };
  let specializations = [];
  let clinicName = '';
  let container = null;

  /* ------------------------------------------------------------ table --- */
  function doctorCell(doctor) {
    return UI.el('td', { 'data-label': 'Doctor' }, UI.el('div', { class: 'cell-person' }, [
      UI.avatar(doctor.name, false, doctor.photo),
      UI.el('div', {}, [
        UI.el('button', {
          class: 'link-btn', type: 'button',
          title: 'See ' + doctor.name + '’s appointments',
          onClick: function () { openDetails(doctor.doctor_id, 'appointments'); }
        }, doctor.name),
        UI.el('span', { class: 'muted small', text: doctor.doctor_id })
      ])
    ]));
  }

  function contactCell(doctor) {
    return UI.el('td', { 'data-label': 'Contact' }, [
      UI.el('div', { text: doctor.phone || '—' }),
      doctor.email ? UI.el('span', { class: 'muted small', text: doctor.email }) : null
    ]);
  }

  function availabilityCell(doctor) {
    const availability = doctor.availability;
    const notes = [];
    if (availability.on_leave_today) { notes.push('Away today'); }
    if (!availability.working_days.length) { notes.push('No hours set'); }
    return UI.el('td', { 'data-label': 'Availability' }, [
      UI.el('div', { text: DoctorDetail.hoursText(availability) }),
      notes.length
        ? UI.el('span', { class: 'muted small', text: notes.join(' · ') })
        : UI.el('span', { class: 'muted small',
                          text: doctor.appointments.upcoming + ' upcoming' })
    ]);
  }

  /* Four actions per row. Text buttons would need a third of the table
     width, so each is an icon with its name on hover and for screen
     readers. */
  function iconButton(icon, label, onClick, danger) {
    return UI.el('button', {
      class: 'btn btn-ghost btn-icon btn-sm' + (danger ? ' is-danger' : ''),
      type: 'button', title: label, 'aria-label': label, onClick: onClick
    }, UI.icon(icon, 'icon-sm'));
  }

  function actionsCell(doctor, refresh) {
    const buttons = [
      iconButton('eye', 'View ' + doctor.name,
                 function () { openDetails(doctor.doctor_id); }),
      iconButton('edit', 'Edit ' + doctor.name,
                 function () { edit(doctor, refresh); })
    ];

    if (doctor.status === 'archived') {
      buttons.push(iconButton('refresh', 'Restore ' + doctor.name,
                              function () { restore(doctor, refresh); }));
    } else {
      buttons.push(doctor.status === 'active'
        ? iconButton('ban', 'Deactivate ' + doctor.name,
                     function () { toggleActive(doctor, refresh); })
        : iconButton('check-circle', 'Activate ' + doctor.name,
                     function () { toggleActive(doctor, refresh); }));
      buttons.push(iconButton('trash', 'Remove ' + doctor.name,
                              function () { remove(doctor, refresh); }, true));
    }

    return UI.el('td', { class: 'cell-right', 'data-label': 'Actions' },
                 UI.el('div', { class: 'row-actions' }, buttons));
  }

  function table(rows, refresh) {
    return UI.el('div', { class: 'table-wrap' }, UI.el('table', { class: 'data table-doctors' }, [
      UI.el('thead', {}, UI.el('tr', {}, [
        UI.el('th', { text: 'Doctor' }),
        UI.el('th', { text: 'Specialization' }),
        UI.el('th', { text: 'Contact' }),
        UI.el('th', { text: 'Status' }),
        UI.el('th', { text: 'Availability' }),
        UI.el('th', { class: 'cell-right', text: 'Actions' })
      ])),
      UI.el('tbody', {}, rows.map(function (doctor) {
        return UI.el('tr', { class: doctor.status === 'active' ? '' : 'row-muted' }, [
          doctorCell(doctor),
          UI.el('td', { 'data-label': 'Specialization' }, [
            UI.el('div', { text: doctor.specialization || '—' }),
            UI.el('span', { class: 'muted small', text: doctor.qualification || '' })
          ]),
          contactCell(doctor),
          UI.el('td', { 'data-label': 'Status' }, DoctorDetail.statusBadge(doctor.status)),
          availabilityCell(doctor),
          actionsCell(doctor, refresh)
        ]);
      }))
    ]));
  }

  /* ---------------------------------------------------------- actions --- */
  function openDetails(doctorId, tab) {
    DoctorDetail.open(doctorId, {
      specializations: specializations,
      tab: tab,
      onChanged: function () { load(); }
    });
  }

  function edit(doctor, refresh) {
    AdminStore.getDoctor(doctor.doctor_id).then(function (detail) {
      DoctorForm.open({
        doctor: detail.doctor,
        clinic: detail.clinic,
        schedule: detail.schedule,
        slotDuration: detail.doctor.availability.slot_duration || 20,
        specializations: specializations,
        onSaved: refresh
      });
    }).catch(function (error) {
      UI.toast(error.message || 'Could not open that doctor.', 'error');
    });
  }

  function add() {
    DoctorForm.open({
      specializations: specializations,
      onSaved: function () { load(); }
    });
  }

  function toggleActive(doctor, refresh) {
    const deactivating = doctor.status === 'active';
    const upcoming = doctor.appointments.upcoming;
    UI.confirm({
      title: deactivating ? 'Deactivate ' + doctor.name + '?' : 'Activate ' + doctor.name + '?',
      message: deactivating
        ? 'Callers will no longer be offered this doctor and no new '
          + 'appointment can be booked for them. '
          + (upcoming
            ? 'The ' + upcoming + ' appointment(s) already in their diary are kept - '
              + 'block their dates if those should be cancelled too.'
            : 'They have nothing booked.')
        : 'The voice assistant will offer this doctor again and callers can book them.',
      confirmLabel: deactivating ? 'Deactivate' : 'Activate',
      danger: deactivating
    }).then(function (confirmed) {
      if (!confirmed) { return; }
      AdminStore.setActive(doctor.doctor_id, !deactivating).then(function (result) {
        UI.toast(result.note);
        refresh();
      }).catch(function (error) {
        UI.toast(error.message || 'That change could not be saved.', 'error');
      });
    });
  }

  function remove(doctor, refresh) {
    UI.confirm({
      title: 'Remove ' + doctor.name + '?',
      message: 'The doctor is taken off the register and can no longer be '
        + 'booked. Their ' + doctor.appointments.total + ' appointment record(s) '
        + 'are kept, and you can restore them from the Removed filter.',
      confirmLabel: 'Remove doctor',
      danger: true
    }).then(function (confirmed) {
      if (!confirmed) { return; }
      AdminStore.removeDoctor(doctor.doctor_id).then(function () {
        UI.toast(doctor.name + ' was removed from the register');
        refresh();
      }).catch(function (error) {
        UI.toast(error.message || 'That doctor could not be removed.', 'error');
      });
    });
  }

  function restore(doctor, refresh) {
    UI.confirm({
      title: 'Restore ' + doctor.name + '?',
      message: 'The record comes back deactivated. Activate the doctor '
        + 'afterwards to let callers book them again.',
      confirmLabel: 'Restore'
    }).then(function (confirmed) {
      if (!confirmed) { return; }
      AdminStore.restoreDoctor(doctor.doctor_id).then(function (result) {
        UI.toast(result.note);
        refresh();
      }).catch(function (error) {
        UI.toast(error.message || 'That doctor could not be restored.', 'error');
      });
    });
  }

  /* ------------------------------------------------------------- page --- */
  function toolbar() {
    const search = UI.el('input', {
      class: 'input', type: 'search', id: 'doctor-search',
      placeholder: 'Search name, ID, specialization or clinic',
      'aria-label': 'Search doctors', value: filters.q, autocomplete: 'off'
    });
    let timer = null;
    search.addEventListener('input', function () {
      clearTimeout(timer);
      timer = setTimeout(function () {
        filters.q = search.value.trim();
        load({ keepFocus: true });
      }, 220);
    });

    const status = UI.el('select', {
      class: 'input', id: 'doctor-status', 'aria-label': 'Filter by status'
    }, STATUS_FILTERS.map(function (option) {
      return UI.el('option', { value: option.id, text: option.label,
                               selected: filters.status === option.id });
    }));
    status.addEventListener('change', function () {
      filters.status = status.value;
      load();
    });

    const specialization = UI.el('select', {
      class: 'input', id: 'doctor-specialization-filter',
      'aria-label': 'Filter by specialization'
    }, [UI.el('option', { value: '', text: 'All specializations' })].concat(
      specializations.map(function (name) {
        return UI.el('option', { value: name, text: name,
                                 selected: filters.specialization === name });
      })));
    specialization.addEventListener('change', function () {
      filters.specialization = specialization.value;
      load();
    });

    return UI.el('div', { class: 'toolbar' }, [
      UI.el('div', { class: 'search grow' }, [UI.icon('search'), search]),
      status,
      specialization
    ]);
  }

  function render(target) {
    container = target;
    UI.mount(container, [
      UI.el('div', { class: 'page-head' }, [
        UI.el('div', {}, [
          UI.el('h2', { text: 'Doctors' }),
          UI.el('p', { id: 'doctors-subtitle',
                       text: 'Everyone the voice assistant can book appointments with.' })
        ]),
        UI.el('button', {
          class: 'btn btn-primary', type: 'button', onClick: add
        }, [UI.icon('plus', 'icon-sm'), 'Add doctor'])
      ]),
      UI.el('section', { class: 'card', id: 'doctors-card' }, [
        UI.el('div', { class: 'card-head' }, UI.el('div', { id: 'doctors-toolbar' })),
        UI.el('div', { class: 'card-body card-body-flush', id: 'doctors-body' },
              UI.skeleton(5))
      ])
    ]);
    load();
  }

  function load(options) {
    const config = options || {};
    const body = document.getElementById('doctors-body');
    if (!body) { return; }

    AdminStore.getDoctors({
      q: filters.q, status: filters.status, specialization: filters.specialization
    }).then(function (data) {
      specializations = data.specializations || [];
      const first = data.doctors[0];
      clinicName = (first && first.clinic && first.clinic.name) || clinicName;
      const subtitle = document.getElementById('doctors-subtitle');
      if (subtitle && clinicName) {
        subtitle.textContent = 'Everyone the assistant can book at ' + clinicName + '.';
      }
      const toolbarSlot = document.getElementById('doctors-toolbar');
      const focused = document.activeElement && document.activeElement.id === 'doctor-search';
      const caret = focused ? document.activeElement.selectionStart : null;
      UI.mount(toolbarSlot, toolbar());
      if (focused || config.keepFocus) {
        const search = document.getElementById('doctor-search');
        if (search) {
          search.focus();
          if (caret !== null) { search.setSelectionRange(caret, caret); }
        }
      }

      if (!data.doctors.length) {
        UI.mount(body, UI.empty({
          icon: 'users',
          title: filters.q || filters.specialization || filters.status !== 'listed'
            ? 'No doctors match that'
            : 'No doctors yet',
          message: filters.q || filters.specialization || filters.status !== 'listed'
            ? 'Try a different search or filter.'
            : 'Add the first doctor so the voice assistant has someone to book.',
          action: UI.el('button', {
            class: 'btn btn-primary', type: 'button', onClick: add
          }, 'Add doctor')
        }));
        return;
      }
      UI.mount(body, table(data.doctors, function () { load(); }));
    }).catch(function (error) {
      UI.mount(body, UI.apiError(error, function () { load(); }));
    });
  }

  return { render: render, openAddForm: add, openDetails: openDetails };
})();
