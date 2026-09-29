# Firestore and dashboard performance

What made the dashboards slow, what was changed, and what was measured
before and after. Every number here was measured; how is described under
[How it was measured](#how-it-was-measured), and the scripts are in the
repository so the measurements can be repeated.

Scope: the doctor dashboard, the administrator area, and the API behind them
(`api/dashboard.py`, `api/admin.py`). The voice pipeline's booking path, the
intent model, the Dialogue Manager and the LLM judge were not changed.

---

## 1. Analysis (before any change)

Measured on the live Firestore project with 27 documents in the
collections the dashboard reads.

**One Firestore round trip costs 0.55 s from this machine - and for long
stretches every other one took about 2.35 s.** A read-only probe (`get`,
queries, `get_all`, `count()`) showed the same pattern for every kind of call,
for unary and streaming RPCs, with the native DNS resolver, and with gRPC
holding one connection (`pick_first`, one IPv6 subchannel); after a while of
use it settled to 0.55 s. TCP connects take about 50 ms. The extra time is on
the network or server side, outside the application. Four independent reads
took 2.2 s one after another and 0.55 s in parallel.

So at this data size the cost of a page is the number of **sequential** round
trips it makes, and as the data grows it is the number of **documents** it
reads. The dashboard lost on both:

| Problem | Where | Effect |
|---|---|---|
| Sign-in read the same account document three times, one after another | `/auth/login` | 4-5 sequential round trips (8.1 s measured cold) |
| The doctor dashboard fetched the profile before drawing anything, although the sign-in answer already carried it | `app.js` | a whole extra request (5.4 s) before the first counter appeared |
| The summary was requested twice at start-up and twice on every 30-second refresh | `app.js` + `home.js` | duplicate requests |
| The unread-message badge downloaded the whole message list | `app.js` / `store.js` | an extra request per refresh |
| Every patient in the clinic was read to put names on a few appointments | `patient_index()` | whole `patients` collection, 3 of 5 requests |
| Statistics downloaded every appointment to count them | `/dashboard/statistics`, `/admin/statistics` | whole history per view |
| The administrator's overview, doctor list and appointment list read every appointment, schedule, leave record and message | `api/admin.py` `read()` | whole collections, 5 sequential round trips |
| The administrator overview asked for the statistics only after the summary arrived | `admin-overview.js` | a waterfall |
| Lists downloaded every row and paged in the browser; "Load more" downloaded everything again | appointments, patients, notifications | reads and transfer grow with history |
| Independent reads inside one request ran one after another | all endpoints | every read a full round trip |

What was checked and found **not** to be a problem: there is no React, no
build step and no component re-rendering loop; there are **no Firestore
listeners** anywhere (the browser has no Firebase access by design). The
dashboard's own files (342 KB) were not what the user waited for, although
they were sent uncompressed.

---

## 2. What was changed

### Reading Firestore (`api/data.py`, `firebase/firebase_config.py`)

- **Only the records a screen shows.** Queries filter on `doctor_id` and
  `date` in Firestore: the doctor summary reads today onwards, statistics read
  the period's dates, the doctor detail reads that doctor only.
- **Patients by id, in one batch** (`get_many`, Firestore `get_all`) instead of
  the whole collection.
- **Counting without downloading.** All-time figures, list totals and the
  unread badge use Firestore `count()` aggregations (one read per 1,000
  entries counted).
- **Independent reads in parallel** (`data.parallel`), so most pages need one
  or two round trips. Each parallel read is still counted against its request.
- **Pages read in date order and stop when the page is full**
  (`page_by_date`). The last date read is completed with an equality query so
  pages never disagree about the order within a day.
- **A missing index never breaks a page.** A query Firestore refuses for want
  of a composite index is answered with a simpler query plus filtering in the
  API, with the same result, and a warning names the index to deploy. The
  process remembers the missing index for ten minutes, so the refused attempt
  is paid once, not on every request.

### Sign-in and the doctor's start-up

- One round trip for sign-in: the username lookup, the account and the doctor
  record are read together, and the account record is reused for the
  password check and the reply. If the database cannot be read, sign-in
  answers 503 rather than falling back to the starting password.
- The dashboard opens with the doctor record from the sign-in answer.
- One summary request feeds today's list, the refresh alerts and the unread
  badge (the summary now carries `unread_notifications`).

### Start-up and transfer

- **The API warms its Firestore connection in the background at start-up**
  (the clinic and doctor records, which also fills their cache, and one
  `limit 1` probe per declared index, which logs any index not yet deployed),
  so the first person to sign in after a restart does not pay for it.
- **Responses over 1 KB are compressed** (Starlette's built-in gzip; no new
  dependency), which is what the dashboard's scripts and styles are.

### Caching, and how it is invalidated

| Where | What | For how long | Dropped when |
|---|---|---|---|
| API (`firebase/cache.py`) | the clinic record, the doctor register | 60 s | at once, on **any** write to that collection through the repository |
| Browser (`api.js`) | identical requests in flight | while in flight | - |
| Browser | summary | 3 s | any POST/PATCH/DELETE, sign-out, sign-in |
| Browser | statistics | 60 s doctor / 25 s administrator | the same, and when the summary's `signature` shows the diary changed |
| Browser | profile, working week, leave | 2 min / 1 min | the same |
| Browser | doctor register / clinic (administrator) | 20 s / 60 s | the same |

Appointment lists, patients and messages are never cached. The booking rules
read doctors through `DoctorService`, which does not use the cache, so a
doctor who has just been switched off can never be booked from a stale copy.

### Pagination and search

Appointments (doctor and administrator), patients, notifications and the
doctor register are paged on the server (`offset`, `limit`, `total`,
`has_more`); *Load more* fetches only the next page. A text search has to look
at every row of the window it searches, because Firestore has no substring
search, but only the matching page is decorated and sent.

### Loading and errors

Every section already had a skeleton and an error state with a retry button;
the new *Load more* buttons show a loading state, and an answer that arrives
after the filters changed is dropped. A database failure anywhere in the API
is answered with a plain 503 message; the Firestore error text is logged,
never sent to the browser.

### Security

Unchanged, and tested again: the doctor id still comes only from the
session; paging parameters cannot widen a doctor's scope; a doctor can no
longer mark another doctor's message read by guessing its id (it is checked
by one read instead of a scan). A profile update that includes clinic fields
is now refused **before** anything is written.

---

## 3. Firestore indexes

Declared in [`firestore.indexes.json`](../firestore.indexes.json); every query
the code makes either needs no composite index or uses one of these.

| Collection | Fields | Used by |
|---|---|---|
| `appointments` | `doctor_id` ↑, `date` ↑ | doctor summary, doctor statistics, doctor lists (oldest first), leave preview |
| `appointments` | `doctor_id` ↑, `date` ↓ | administrator lists filtered by doctor (newest first), recent visits in the doctor detail |
| `appointments` | `status` ↑, `date` ↑ | the administrator's "upcoming" count |
| `notifications` | `doctor_id` ↑, `created_at` ↓ | the doctor's messages, newest first |

Create them with `python scripts/firestore_indexes.py --create`, or
`firebase deploy --only firestore:indexes`. Until they exist the fallback
answers correctly with more reads. The in-memory database used by the tests
enforces the same rule against this file, so a query needing an undeclared
index fails in the tests exactly as it would in production.

---

## 4. Results

### 4.1 The live project

The committed code ("before", with only the measurement added) and the
optimised code ("after") ran side by side against the same live Firestore
project, the one this system uses (the same few dozen documents as in
section 1). Both ran on warm servers: one unrecorded warm-up run
each, then three recorded runs, alternating. The table shows medians of the
three runs. The composite indexes were **not** deployed, so "after" includes
the fallback queries.

| Page | Before | After | Requests | Firestore reads |
|---|---:|---:|---:|---:|
| Doctor: sign in → dashboard | 7,307 ms | 1,196 ms | 6 → 3 | 25 → 18 |
| Doctor: appointments | 1,147 ms | 1,127 ms | 1 → 1 | 6 → 6 |
| Doctor: patients | 1,132 ms | 1,138 ms | 1 → 1 | 6 → 5 |
| Doctor: schedule | 1,072 ms | 592 ms | 2 → 2 | 7 → 7 |
| Doctor: profile | 1,671 ms | 556 ms | 1 → 1 | 3 → 3 |
| Doctor: notifications | 577 ms | 561 ms | 1 → 1 | 1 → 1 |
| Doctor: settings | 1,695 ms | 0 ms | 1 → 0 | 3 → 0 |
| Doctor: back to the dashboard | 1,746 ms | 581 ms | 2 → 1 | 10 → 5 |
| Doctor: 30-second refresh | 3,389 ms | 572 ms | 4 → 1 | 18 → 5 |
| Administrator: sign in → overview | 5,705 ms | 1,710 ms | 3 → 3 | 28 → 32 |
| Administrator: doctors | 3,004 ms | 723 ms | 1 → 1 | 42 → 37 |
| Administrator: appointments | 1,746 ms | 551 ms | 1 → 1 | 13 → 1 |
| Administrator: clinic | 1,714 ms | 555 ms | 1 → 1 | 7 → 1 |
| Administrator: back to the overview | 4,047 ms | 1,049 ms | 2 → 1 | 25 → 8 |
| Administrator: 30-second refresh | 4,016 ms | 1,052 ms | 2 → 2 | 25 → 20 |
| **One pass through every page** | **40.8 s** | **12.0 s** | **29 → 20** | **219 → 149** |

The summary the brief asked for:

| Area | Before | After |
|---|---:|---:|
| Dashboard initial load (doctor, sign-in to a usable dashboard) | 7.3 s | 1.2 s |
| Administrator overview (sign-in to overview) | 5.7 s | 1.7 s |
| Appointments page (doctor / administrator) | 1.15 s / 1.75 s | 1.13 s / 0.55 s |
| Doctors page (administrator) | 3.0 s | 0.72 s |
| Patients page | 1.13 s | 1.14 s |
| Statistics request, median per request (doctor / administrator) | 0.62 s / 1.17 s | 0.87 s / 1.10 s |
| API requests, one pass through every page | 29 | 20 |
| Firestore reads, one pass through every page | 219 | 149 |

### 4.2 The same pages on a large clinic

The same page requests against an in-memory clinic of **20,000
appointments, 5,000 patients, 10 doctors and 1,000 messages**, counted
exactly (Firestore bills reads the same way whatever the network).

| Page | Reads before | Reads after |
|---|---:|---:|
| Doctor: sign in → dashboard | 16,115 | 435 |
| Doctor: appointments (first page) | 7,002 | 47 |
| Doctor: patients (first page) | 7,002 | 3,655 |
| Doctor: notifications (first page) | 93 | 23 |
| Doctor: 30-second refresh | 16,108 | 292 |
| Administrator: sign in → overview | 46,027 | 1,605 |
| Administrator: doctors | 20,037 | 2,830 |
| Administrator: appointments (first page) | 25,012 | 164 |
| Administrator: statistics, all time | 20,011 | 178 |
| Schedule, profile, clinic | 22 | 11 |
| **Total** | **157,429** | **9,240** |

### 4.3 Other measurements

- **First sign-in after the API starts** (live project, median of three
  restarts each): 4,035 ms without the start-up warm-up, 2,919 ms with it.
  The first requests after a restart stay slower than on a warm server
  (about 2.9 s against 0.6 s); the probe in section 1 showed the connection
  itself only settling after some use.
- **Dashboard download**: 341,761 bytes, 90,811 bytes with compression
  (every file under `/ui`, measured over HTTP; ETags are kept, so a repeat
  visit revalidates instead of downloading again).

### 4.4 What did not get faster, and why

- **Doctor appointments and patients pages**: the same on the live project,
  because at three appointments there is nothing to save - each is one small
  query either way. The difference shows at size: a first page of
  appointments reads 47 documents instead of 7,002. The patients page still
  reads the doctor's history (section 6).
- **The doctor's statistics request** is slower per request on the live
  project (0.87 s against 0.62 s, median): it now makes eight small queries
  in parallel (seven counts and the period) where the old code made one read
  of the doctor's three appointments, and with this connection's latency a
  wave of parallel calls often waits for a slow one. It is asked for a third
  as often, and on a large diary it reads the period instead of the whole
  history.
- **The administrator's sign-in reads 28 → 32 documents** on the live
  project: a `count()` is billed at least one read even when it counts only
  a few documents. On the large clinic the same page reads 1,605 instead of
  46,027.
- **More database calls** in all (75 → 85 per pass through every page), made
  in parallel rather than one after another.
- **The schedule page** measured faster (1,072 → 592 ms) although its
  endpoints were not changed; that is network variation, not a result of
  this work.

---

## 5. How it was measured

- **Per-request accounting** (`firebase/metrics.py`). Every repository call
  records its round trips, the documents it reads (as Firestore bills them)
  and its writes, against the HTTP request being served. With `PERF_DEBUG=1`
  the API logs one line per request and adds a `Server-Timing` header,
  visible in the browser's developer tools. Nothing is shown in the
  interface, and without the flag nothing is added to any response.
- **Page timing** (`tests/browser/measure_pages.js`). Headless Chrome signs in
  as each role and opens every page, timing from the click until the network
  is quiet, and adds up each page's requests and database work from the
  headers. The 30-second refresh is measured as its own step, 30 seconds after
  the page it refreshes, so it can never land inside another page's timing.
- **The comparison** ran the committed code (with only the instrumentation
  added) and the optimised code side by side against the same live project,
  with one warm-up run each and then three recorded runs, alternating; the
  table shows medians.
- **Scale** (`tests/test_performance.py`, and the same page requests against
  an in-memory clinic). Firestore bills reads the same way whatever the
  network, so document counts at scale are exact; in-memory times are not
  reported because they do not model Firestore's own aggregation.
- **The tests** (`tests/test_performance.py`, 31 tests) keep it that way:
  they fail if a screen goes back to reading a whole collection, if a page
  reads more than a page, if any paged list stops joining up to exactly the
  unpaged answer, if a statistic differs from a count over every record, or
  if the answers change when no index is deployed.

---

## 6. Known limits

- **Patients page**: Firestore cannot group or de-duplicate, so the list of a
  doctor's patients still reads that doctor's appointment history (never
  anybody else's), then only their patients. A per-doctor patient summary
  would need a new collection kept up to date by every booking; the data
  model was left as it is.
- **Text search** reads the window being searched.
- **The administrator's doctor register** reads the clinic's appointments
  from today on to count each doctor's upcoming work.
- **"All time" statistics per doctor** are seven `count()` queries per doctor,
  run together: cheap in reads, but many small requests.
- **The 30-second refresh** reads the doctor's appointments from today on.
  Pushing changes to the browser would need a server-side listener and a
  streaming connection; polling was kept.
- **Latency** was measured from one machine on one connection, and varies
  between 0.55 s and 2.35 s per round trip there.
- **Right after the API starts**, the first requests are still slower than on
  a warm server (about 2.9 s against 0.6 s, section 4.3), even with the
  start-up warm-up.
- **Indexes**: the four composite indexes were not deployed while these
  measurements were taken. With them, the doctor's diary, the lists filtered
  by doctor and the messages read straight from the index instead of through
  the fallback.
