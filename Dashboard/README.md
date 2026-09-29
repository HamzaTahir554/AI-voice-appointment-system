# Clinic Dashboard - AI Voice Appointment System

Two areas behind one sign-in screen, decided by the account that signs in:

| Account | What it is for |
|---|---|
| **Doctor** (`D001`, ...) | their own day: today's appointments, patients, diary, working hours, leave, profile and the messages queued for their patients |
| **Administrator** (`ADMIN`) | the practice itself: the clinic details, the doctors, their sign-in details, every appointment, and anyone's working hours or leave |

Both show **real data**. Every screen reads and writes through the Appointment
Backend, which owns the business rules and talks to Firebase:

```text
Dashboard  ->  /dashboard/*  (doctor)      ->  Appointment Backend  ->  Firebase
           ->  /admin/*      (administrator)
```

An appointment booked through the voice assistant's conversation (mBERT ->
Dialogue Manager -> Appointment Backend - the pipeline takes text today; the
telephone and speech-recognition stages are not implemented) appears here
without anybody re-typing it, a
cancellation made here frees the slot for the next caller, and a doctor added
by the administrator can be booked by telephone straight away.

---

## How to run

The dashboard is served by the API, so there is nothing to build:

```powershell
# 1. set the passwords (see .env.example)
#    DASHBOARD_PASSWORD=...        doctors
#    SUPERADMIN_PASSWORD=...       the administrator

# 2. start the API
uvicorn api.main:app --port 8000

# 3. open the dashboard
#    http://127.0.0.1:8000/ui/
```

Sign in with a **username** (issued by the administrator), a **doctor ID**
(for example `D001`, from the `doctors` collection), or the **administrator
ID** (`ADMIN` by default, set by `SUPERADMIN_ID`).

Those two environment values are only the **starting** passwords. As soon as
the administrator issues a password, or a doctor changes their own, the real
one lives hashed in the database and the environment value stops working for
that account.

---

## Authentication and roles

There was no authentication in this project before, so the smallest honest one
was added (`api/auth.py`):

- `POST /auth/login {user_id, password}` returns an opaque session token and
  the **role** that id belongs to.
- The token goes in an `Authorization: Bearer` header on every later request
  and lives in `sessionStorage`, so closing the tab ends the session.
- A doctor token maps to **one** doctor id: every `/dashboard/*` route reads
  only that doctor's data, and asking for another doctor's appointment returns
  403.
- The two roles do not overlap. A doctor calling `/admin/*` gets 403; the
  administrator calling `/dashboard/*` gets 403 as well, because that data
  belongs to a doctor, not to them.
- Passwords are stored in `dashboard_accounts`, hashed with PBKDF2-HMAC-SHA256
  and compared in constant time (`firebase/account_service.py`). A password is
  never sent back to any screen, and a hash never leaves that module.
- A doctor the administrator has deactivated cannot sign in.
- Changing a password signs that account out of every other open tab.

### Who may change what

| | Administrator | Doctor |
|---|---|---|
| Their own password | yes | yes (current one required) |
| Another account's password | yes, any doctor | no |
| A username | yes, any doctor | no - there is no control and no route |
| The clinic - name, address, city, phone | yes | no - it is shown as information, and the API answers 403 |
| Any doctor's profile, hours, leave | yes | own only |
| Every appointment | yes | own only |

**What it is not:** sessions are held in the API process, so restarting it
signs everyone out. There is no password database, no reset flow and no rate
limiting. Replace it with Firebase Authentication when you need real accounts -
the routes will not have to change, only `api/auth.py`.

No Firebase credential, API key, password or server address is shown anywhere
in this interface.

---

## Pages

### The doctor

| Page | Data it uses |
|---|---|
| **Dashboard** | `GET /dashboard/summary` - counters for today, today's list, who is due now, and what is coming up. Plus `GET /dashboard/statistics` - their own appointments counted by status for today, this week, this month, all time or a custom range. Every number is computed by the backend from the appointment records. |
| **Appointments** | `GET /dashboard/appointments` with scope / date / status / query filters; view, complete, reschedule and cancel; book a new appointment (free slots come from the doctor's real availability). |
| **Patients** | `GET /dashboard/patients` - the people with appointments in this doctor's diary, with visit counts and history. A patient record holds a name and a phone number; nothing else is stored, so nothing else is shown. |
| **Schedule** | `GET/PATCH /dashboard/schedule` - the weekly `schedules` rows the booking engine reads, and `GET/POST/DELETE /dashboard/leave` for blocked dates. |
| **Doctor Profile** | `GET/PATCH /dashboard/profile` - the `doctors` and `clinics` documents. The fee and address the voice assistant quotes change with this form. |
| **Notifications** | `GET /dashboard/notifications` - the messages the backend queued for patients, with their delivery state. |
| **Settings** | Account (photo, name, email, phone), their own password, alerts, appearance and the session. The username is shown but not editable - only the administrator changes it. |

### The administrator

| Page | Data it uses |
|---|---|
| **Overview** | `GET /admin/summary` - how many doctors are on the register and bookable, what is in the diary now, today's appointments across every doctor, and recent activity. Plus `GET /admin/statistics` - the clinic's appointments counted by status for a chosen period, with one row per doctor; choosing a doctor narrows the figures to them. |
| **Doctors** | `GET /admin/doctors` - the register, with each doctor's clinic, contact, status, working hours and workload. Search and filter by status or specialization; add, edit, deactivate, remove and restore. |
| **Doctor details** | `GET /admin/doctors/{id}` - four tabs: profile; availability (the working week, and blocked dates through `.../leave`, which previews what it would cancel first); that doctor's appointments; and Sign-in, where their username and password are set. |
| **Appointments** | `GET /admin/appointments` - every doctor's diary in one table, filtered by scope, doctor, date or search. Cancel (with a reason, which is kept), move to another free slot, or mark completed - each one through the Appointment Backend. |
| **Clinic** | `GET·PATCH /admin/clinic` - the one clinic every doctor works at. Its name and address are what the assistant reads to callers, so changing them here changes them everywhere at once. There is nothing to add, choose or switch. |
| **Settings** | The administrator's own password, appearance and the session. |

### Appointment statuses

The dashboard displays the backend's own statuses and invents none:
`pending`, `confirmed`, `rescheduled`, `completed`, `cancelled`,
`cancelled_by_doctor` (shown as "Cancelled by you").

"Waiting now" on the doctor's home page is **derived**, not stored: an active
appointment today whose time has already started. The backend has no check-in
state, so nothing else would be honest.

---

## What the administrator's actions really do

| Action | Effect on the system |
|---|---|
| **Add a doctor** | Writes a `doctors` document (next free id), a `clinics` document, and the `schedules` rows for the working week. Name aliases are generated so `DoctorService.find_doctor` recognises the spoken name; a clash with an existing doctor is reported rather than hidden. |
| **Edit** | Writes the same documents. Renaming regenerates the aliases - Urdu aliases already on the record are kept. |
| **Deactivate** | Sets `active: false`, which `validation.check_doctor` already refuses bookings for and `find_doctor` already skips. Appointments already in the diary are left alone; sign-in is refused. |
| **Remove** | Archives: `active: false` and `archived: true`. Nothing is deleted - appointments, patients and messages stay - and the record can be restored from the "Removed" filter. |
| **Add a doctor** | Writes the `doctors` document and the working week, attaches them to the one clinic, and - if the form was filled in - issues their username and password in the same step. |
| **Set a username or password** | Writes `dashboard_accounts` only. The doctor can sign in with either the username or their ID; the password is hashed and cannot be read back, so it has to be handed over in person. |
| **Cancel an appointment** | The backend's own cancellation: the record stays, the status becomes `cancelled`, the reason records that the clinic did it, the slot is freed, and a message for the patient joins the queue. |
| **Edit the clinic** | Writes the one `clinics` document. Every doctor profile, appointment and spoken confirmation reads it, which is why a doctor cannot change it. |
| **Block dates** | Runs the existing cascade in `appointment_backend/cancellation_service.py`, after showing exactly which appointments and how many patients it will affect. |

```text
Blocking a date
   -> the date is written to doctor_unavailability (bookings refused)
   -> every live appointment that day becomes cancelled_by_doctor
   -> one notification record per affected patient is queued
```

The dialog before confirming shows how many appointments will be cancelled.
Re-opening a date does not resurrect them - those patients were told to rebook.

**Message delivery is not implemented.** The queue is written and shown, but no
SMS is sent and no call is placed. `firebase/notification_service.py` defines
the `NotificationSender` interface to implement against a provider, with
credentials from the environment (`NOTIFICATION_PROVIDER`).

---

## Alerts (doctor Settings)

The three switches are not decoration. While the dashboard is open it re-reads
today's diary every 30 seconds and compares it with the previous read, so it
can tell the doctor when the voice assistant has **booked**, when a patient has
**cancelled**, and when an appointment has been **moved**. Each switch is
stored on the doctor's own record (`notification_prefs`) and turns one of those
alerts off. There is no push channel in this system; polling is what makes the
alerts possible at all.

---

## Folder structure

```text
Dashboard/
├── index.html              application shell, sign-in screen, icon sprite
├── README.md
├── css/                    global.css, components.css, login.css,
│                           dashboard.css, admin.css, responsive.css
└── js/
    ├── api.js              HTTP layer: session token, error mapping
    ├── store.js            the doctor's data - one function per backend call
    ├── admin-store.js      the administrator's data, the same way
    ├── ui.js               elements, formatting, modals, toasts, validation
    ├── auth.js             sign-in, and which role signed in
    ├── navigation.js       routing and the shell, per role
    ├── app.js              start-up, session restore, polling, alerts
    ├── home.js, appointments.js, patients.js, schedule.js,
    │   profile.js, notifications.js, settings.js          (doctor)
    └── admin-overview.js, admin-doctors.js, admin-clinic.js,
        admin-appointments.js, admin-doctor-form.js,
        admin-doctor-detail.js                             (administrator)
```

`js/store.js` and `js/admin-store.js` are the whole integration surface. Each
function is one backend call, so a change in the API is a change in one file:

```text
store.js                                admin-store.js
getSummary()        /dashboard/summary  getSummary()      /admin/summary
getStatistics(f)    /dashboard/statistics   getStatistics(f)  /admin/statistics
getAppointmentsPage(f) /dashboard/appointments   (one page)
getAppointment(id)  /dashboard/appointments/{id}
createAppointment() POST   ...          getDoctors(f)     /admin/doctors
cancelAppointment() POST   .../cancel   getDoctor(id)     /admin/doctors/{id}
rescheduleAppointment()                 createDoctor(d)   POST   /admin/doctors
completeAppointment()                   updateDoctor(id)  PATCH  /admin/doctors/{id}
getAvailability(date)                   setActive(id, on) POST   .../status
getPatientsPage(f) · getPatient(id)     removeDoctor(id)  DELETE /admin/doctors/{id}
createPatient(d)                        restoreDoctor(id) POST   .../restore
getProfile() · updateDoctorProfile(p)   getSchedule(id) · setSchedule(id, days)
getSchedule() · updateSchedule(days)    getLeave(id) · addLeave(id, e) · removeLeave(id, d)
getLeave() · addLeave() · removeLeave() getAppointments(id, f)
getNotificationsPage(offset)            getAllAppointments(f)  /admin/appointments (one page)
```

---

## Design

Plain HTML, CSS and classic scripts - no build step, no framework, no CDN
except the web font. The look is meant to be unremarkable: one accent colour,
a flat sidebar, 5-9px corners, hairline borders, a single typeface, and tables
that look like tables. Colour is reserved for meaning (status badges,
destructive actions), not decoration.

Every screen works from 1440px down to 360px. Below 1000px the doctor register
becomes one labelled block per doctor, the same pattern the doctor's own tables
use below 768px, so nothing has to be read through a horizontal scrollbar.

Dark mode follows the operating system unless the user picks a side in
Settings.

---

## Keeping up to date

There is no websocket and no Firestore listener anywhere in the system: the
browser has no Firebase access by design, and the API reads on request. While
the dashboard is open it refreshes every 30 seconds, only when the tab is
visible and no dialog is open, so a voice booking shows up on its own without
interrupting anything.

That refresh is **one request**. `/dashboard/summary` carries today's diary,
the unread-message count, and a short signature of every appointment from
today on. The page it redraws reuses that same answer, and the statistics
card is asked for again only when the signature has changed (or its minute is
up), so a quiet diary costs one small read every 30 seconds.

---

## Loading data

- **Lists come a page at a time.** Appointments, patients, messages and the
  doctor register load 20 (register: 50) rows; *Load more* asks the server
  for the next page only and appends it. The server reads only as far as
  that page (see `docs/PERFORMANCE.md`).
- **Opening the dashboard** uses the doctor record the sign-in already
  returned; it does not fetch the profile first.
- **Two identical requests at once become one** (`api.js`), and a few
  answers may be reused for a short time:

  | Answer | Reused for |
  |---|---|
  | Summary (doctor and administrator) | 3 seconds - just long enough for the refresh and the page it redraws to share it |
  | Statistics | 60 s (doctor), 25 s (administrator); sooner when the diary changes |
  | Profile, working week, leave | 2 minutes / 1 minute |
  | Doctor register, clinic | 20 seconds / 1 minute |
  | Appointment lists, patients, messages | never - read again every time |

- **Any change forgets all of it.** Every POST, PATCH or DELETE, signing out
  and signing in clears whatever was being reused, so an edit is never
  followed by an older answer. Nothing is kept in browser storage.

---

## Error handling

Failures are shown, never swallowed:

| Situation | What the user sees |
|---|---|
| API not running / timeout | "Cannot reach the appointment system", with a Try again button |
| Session expired or API restarted | Returned to sign-in with "Your session has ended" |
| Slot already taken, day blocked, appointment already completed | The backend's own message, on the field or as a toast |
| Another doctor's record, or the wrong area for the role | 403, and the record is never listed |
| Nothing to show | An empty state explaining what would appear |

Destructive actions ask first; every write shows a loading state and a toast.

---

## Current limitations

| Area | State |
|---|---|
| Authentication | Prototype session (see above), not Firebase Auth |
| Patient notifications | Queued in Firebase; SMS and calls not connected |
| Real-time | 30-second polling (one request), not push |
| Appointment reason / notes | Not stored by the backend, so not shown |
| Patient demographics | Only name and phone exist in the database |
| Doctor photo | Stored inside the doctor record as a small image; there is no file storage service in this project |
| Clinics | One clinic serves the whole practice; there is no multi-clinic mode |
| Password reset | The administrator issues a new one; there is no email or self-service reset |

---

## Tests

```powershell
python scripts/run_tests.py --category dashboard   # doctor API + administration API
python scripts/run_tests.py                        # the whole offline suite
```

- `tests/test_dashboard_api.py` - sign-in, doctor scoping (403 across doctors),
  cancel keeping the record, complete refusing twice, reschedule, the leave
  cascade with its notifications, and that a voice booking reaches the
  dashboard while a dashboard cancellation frees the slot for the voice side.
- `tests/test_admin_api.py` - the two roles cannot borrow each other's area, a
  doctor added by the administrator is found by `find_doctor` and bookable
  through the public route, deactivating refuses new bookings while keeping the
  diary, removing archives instead of deleting, restoring works, and the
  administrator's schedule and leave writes are the doctor's own ones.
- `tests/test_performance.py` - with 720 appointments and 400 patients in
  the store: which documents each screen reads (no whole collections, one
  page reads a page), that every page of every list joins up to exactly the
  old unpaged answer, that the statistics equal a count over every record,
  that the answers are identical when no Firestore index is deployed, and
  that an edit is visible on the very next request.

The interface itself was driven in headless Chrome against a running API:
sign-in for both roles, every page, adding a doctor, viewing, editing,
deactivating, removing and restoring, the availability and appointment tabs,
alert preferences, the absence of any connection or configuration detail, and
the narrow-screen layout - 59 checks, no console errors.
