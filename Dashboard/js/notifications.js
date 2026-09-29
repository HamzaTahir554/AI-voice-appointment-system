/* ==========================================================================
   notifications.js - the notification centre.

   These are the records the Appointment Backend queues in the
   `notifications` collection when a doctor blocks a day: one per affected
   patient, holding the message that should reach them. Delivery (SMS or an
   automated call) is not connected, so each item also shows whether it has
   actually been sent - which, today, it has not.
   ========================================================================== */
window.Pages = window.Pages || {};

window.Pages.notifications = (function () {
  'use strict';

  const TYPES = {
    appointment_cancelled_by_doctor: {
      icon: 'ban', tone: 'is-danger', title: 'Appointment cancelled by you'
    },
    appointment_cancelled_by_clinic: {
      icon: 'ban', tone: 'is-danger', title: 'Appointment cancelled by the clinic'
    },
    appointment_cancelled: { icon: 'ban', tone: 'is-danger', title: 'Appointment cancelled' },
    appointment_booked: { icon: 'calendar-plus', tone: 'is-success', title: 'New appointment booked' },
    appointment_rescheduled: { icon: 'refresh', tone: 'is-info', title: 'Appointment rescheduled' }
  };

  function meta(type) {
    return TYPES[type] || { icon: 'activity', tone: '', title: String(type || 'Notification').replace(/_/g, ' ') };
  }

  function render(container) {
    const listBody = UI.el('div', { class: 'card-body card-body-flush' }, UI.skeleton(5));

    const markAll = UI.el('button', {
      class: 'btn btn-secondary btn-sm', type: 'button', disabled: true,
      onClick: function () {
        UI.setButtonLoading(markAll, true);
        Store.markAllNotificationsRead().then(function () {
          UI.setButtonLoading(markAll, false);
          UI.toast('All notifications marked as read');
          load(listBody, markAll);
        }).catch(function (error) {
          UI.setButtonLoading(markAll, false);
          UI.toast(error.message || 'Could not update the notifications.', 'error');
        });
      }
    }, [UI.icon('check', 'icon-sm'), 'Mark all as read']);

    UI.mount(container, [
      UI.el('div', { class: 'page-head' }, [
        UI.el('div', {}, [
          UI.el('h2', { text: 'Notifications' }),
          UI.el('p', { text: 'Messages the system has queued for your patients.' })
        ]),
        markAll
      ]),
      UI.el('section', { class: 'card' }, [listBody])
    ]);

    load(listBody, markAll);
  }

  /* Newest first, one page at a time; "Load more" appends the next page. */
  let items = [];
  let ticket = 0;

  function load(listBody, markAll) {
    const mine = ++ticket;
    UI.mount(listBody, UI.skeleton(5));
    Store.getNotificationsPage(0).then(function (page) {
      if (mine !== ticket) { return; }
      items = page.notifications;
      paint(listBody, markAll, page);
    }).catch(function (error) {
      if (mine !== ticket) { return; }
      UI.mount(listBody, UI.apiError(error, function () { load(listBody, markAll); }));
    });
  }

  function loadMore(listBody, markAll, button) {
    const mine = ++ticket;
    UI.setButtonLoading(button, true);
    Store.getNotificationsPage(items.length).then(function (page) {
      if (mine !== ticket) { return; }
      items = items.concat(page.notifications);
      paint(listBody, markAll, page);
    }).catch(function (error) {
      if (mine !== ticket) { return; }
      UI.setButtonLoading(button, false);
      UI.toast(error.message || 'Could not load more messages.', 'error');
    });
  }

  function paint(listBody, markAll, page) {
    markAll.disabled = !page.unread;

    if (!items.length) {
      UI.mount(listBody, UI.empty({
        icon: 'bell',
        title: "You're all caught up",
        message: 'When you block a day, the message queued for each affected patient appears here.'
      }));
      return;
    }

    UI.mount(listBody, items.map(function (item) {
      const info = meta(item.type);
      const sent = item.status === 'sent';
      return UI.el('button', {
        class: 'note-item' + (item.read_by_doctor ? '' : ' is-unread'),
        type: 'button',
        'aria-label': (item.read_by_doctor ? 'Read' : 'Unread') + ' notification: ' + info.title,
        onClick: function () {
          if (item.read_by_doctor) { return; }
          Store.markNotificationRead(item.notification_id)
            .then(function () { load(listBody, markAll); })
            .catch(function (error) { UI.toast(error.message || 'Could not mark it read.', 'error'); });
        }
      }, [
        UI.el('span', { class: 'note-icon ' + info.tone }, UI.icon(info.icon)),
        UI.el('span', { class: 'note-body' }, [
          UI.el('strong', { text: info.title }),
          UI.el('p', { text: item.message || '' }),
          UI.el('span', {
            class: 'pill',
            text: sent ? 'Message sent' : 'Delivery pending - SMS and calls are not connected'
          })
        ]),
        UI.el('span', { class: 'row' }, [
          UI.el('span', { class: 'note-time', text: when(item.created_at) }),
          item.read_by_doctor ? null : UI.el('span', { class: 'note-unread-dot', 'aria-hidden': 'true' })
        ])
      ]);
    }));

    if (page.has_more) {
      const more = UI.el('button', {
        class: 'btn btn-secondary btn-sm', type: 'button',
        onClick: function () { loadMore(listBody, markAll, more); }
      }, 'Load more');
      listBody.appendChild(UI.el('div', { class: 'card-body row-between wrap' }, [
        UI.el('span', { class: 'muted small',
                        text: 'Showing ' + items.length + ' of ' + page.total + ' messages' }),
        more
      ]));
    }
  }

  function when(created) {
    const stamp = Date.parse(created);
    return isNaN(stamp) ? String(created || '') : UI.timeAgo(stamp);
  }

  return { render: render };
})();
