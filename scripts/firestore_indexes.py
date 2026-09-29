"""
Check - and, when asked, create - the Firestore composite indexes this
project declares in firestore.indexes.json.

    python scripts/firestore_indexes.py            # compare, change nothing
    python scripts/firestore_indexes.py --create   # create the missing ones

Uses the same service account as the API (.env), through the Firestore
Admin API that ships with google-cloud-firestore, so the Firebase CLI is not
needed. (With the CLI, `firebase deploy --only firestore:indexes` does the
same from firebase.json.)

Without these indexes the dashboard still works: api/data.py notices the
missing index, answers with a simpler query and logs a warning naming it.
With them, the doctor's diary, the paged appointment lists and the
notifications are read straight from the index. Creating an index takes a
few minutes and does not change or move any document.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import config  # noqa: E402

DECLARED = REPO / "firestore.indexes.json"


def credentials():
    from google.oauth2 import service_account

    if config.FIREBASE_CREDENTIALS_FILE:
        path = Path(config.FIREBASE_CREDENTIALS_FILE)
        if not path.is_absolute():
            path = REPO / path
        return service_account.Credentials.from_service_account_file(str(path))
    if config.FIREBASE_PROJECT_ID and config.FIREBASE_CLIENT_EMAIL and config.FIREBASE_PRIVATE_KEY:
        return service_account.Credentials.from_service_account_info({
            "type": "service_account", "project_id": config.FIREBASE_PROJECT_ID,
            "client_email": config.FIREBASE_CLIENT_EMAIL,
            "private_key": config.FIREBASE_PRIVATE_KEY,
            "token_uri": "https://oauth2.googleapis.com/token"})
    raise SystemExit("Firebase is not configured in .env - nothing to check.")


def declared() -> list[dict]:
    spec = json.loads(DECLARED.read_text(encoding="utf-8"))
    return [{"collection": index["collectionGroup"],
             "fields": [(f["fieldPath"], f.get("order", "ASCENDING"))
                        for f in index["fields"]]}
            for index in spec.get("indexes", [])]


def describe(index: dict) -> str:
    return index["collection"] + " (" + ", ".join(
        f"{name} {order.lower()}" for name, order in index["fields"]) + ")"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--create", action="store_true",
                        help="create the declared indexes that are missing")
    args = parser.parse_args()

    from google.cloud import firestore_admin_v1
    from google.cloud.firestore_admin_v1.types import Index

    creds = credentials()
    project = config.FIREBASE_PROJECT_ID or creds.project_id
    client = firestore_admin_v1.FirestoreAdminClient(credentials=creds)

    wanted = declared()
    live: dict[str, list] = {}
    for index in wanted:
        group = index["collection"]
        if group not in live:
            parent = f"projects/{project}/databases/(default)/collectionGroups/{group}"
            live[group] = [(
                [(f.field_path, Index.IndexField.Order(f.order).name) for f in item.fields
                 if f.field_path != "__name__"], Index.State(item.state).name)
                for item in client.list_indexes(parent=parent)]

    missing = []
    for index in wanted:
        state = next((s for fields, s in live[index["collection"]]
                      if fields == index["fields"]), None)
        print(f"  {'ok  ' if state == 'READY' else ('....' if state else 'MISSING')}  "
              f"{describe(index)}" + (f"  [{state}]" if state and state != "READY" else ""))
        if state is None:
            missing.append(index)

    if not missing:
        print("Every declared index exists.")
        return 0
    if not args.create:
        print(f"{len(missing)} index(es) missing. Run with --create to build them "
              "(a few minutes; no document is changed).")
        return 1

    for index in missing:
        parent = (f"projects/{project}/databases/(default)/collectionGroups/"
                  f"{index['collection']}")
        body = Index(query_scope=Index.QueryScope.COLLECTION, fields=[
            Index.IndexField(field_path=name, order=Index.IndexField.Order[order])
            for name, order in index["fields"]])
        client.create_index(parent=parent, index=body)
        print(f"  creating {describe(index)}")
    print("Firestore is building them; run this again to see when they are READY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
