# Browser checks

Two scripts drive the real dashboard in headless Chrome and check what a
person would see: signing in as each role, every page, adding and editing
doctors, credentials, the clinic record, leave with its preview, appointment
actions, statistics, and the narrow-screen layout. Any JavaScript error on the
page fails the run.

They use Node's built-in `WebSocket` to speak the Chrome DevTools Protocol,
so there is nothing to `npm install`.

## Requirements

- Node.js 22 or newer (tested on 24)
- Google Chrome. Set `CHROME_PATH` if it is not in the default Windows location.

## Running them

Start the API against the **in-memory database**, so the checks never touch
Firestore, with test passwords of your choosing:

```powershell
$env:USE_LOCAL_DB = "1"
$env:DASHBOARD_PASSWORD = "test-doc"
$env:SUPERADMIN_PASSWORD = "test-admin"
$env:OLLAMA_ENABLED = "false"
uvicorn api.main:app --port 8011
```

Then, in a second terminal:

```powershell
node tests/browser/dashboard_checks.js http://127.0.0.1:8011/ui/ ADMIN test-admin D001 test-doc
node tests/browser/admin_checks.js     http://127.0.0.1:8011/ui/ ADMIN test-admin D001 test-doc
```

Each prints one line per check and a total, and exits non-zero if anything
failed.

## Notes

- Restart the API between runs. The in-memory database keeps what the last
  run created, and the checks book appointments and add doctors.
- `admin_checks.js` changes the administrator's and D001's passwords on
  purpose, to test that, and puts both back before it finishes.
