"""
One command for the whole test suite, with machine-readable results.

    python scripts/run_tests.py                        # every offline test
    python scripts/run_tests.py --category dialogue    # one area
    python scripts/run_tests.py --live                 # + real Firestore and Ollama
    python scripts/run_tests.py --list                 # show the categories

Writes reports/test_results.json and reports/test_results.xml (JUnit format,
which CI systems and IDEs read) and prints TOTAL / PASSED / FAILED / SKIPPED /
ERRORS for the whole run and per category.

Live tests (tests/test_*_live.py) touch the real Firestore project and the
local Ollama server. They are skipped unless --live is given; every document
they create is deleted afterwards.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import unittest
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
REPORTS = REPO / "reports"

CATEGORIES = {
    "intent": ["test_intents", "test_roman_urdu", "test_intent_audit"],
    "dialogue": ["test_dialog", "test_entities", "test_naturalness", "test_scenarios",
                 "test_emergency_safety"],
    "backend": ["test_backend", "test_appointments"],
    "firebase": ["test_firebase", "test_firestore_live"],
    "ollama": ["test_ollama", "test_ollama_live"],
    "end_to_end": ["test_integration", "test_api"],
    "dashboard": ["test_dashboard_api", "test_admin_api", "test_statistics"],
    "security": ["test_security", "test_data_safety"],
}
MODULE_TO_CATEGORY = {module: cat for cat, modules in CATEGORIES.items() for module in modules}


def category_of(test_id: str) -> str:
    module = test_id.split(".")[1] if test_id.startswith("tests.") else test_id.split(".")[0]
    return MODULE_TO_CATEGORY.get(module, "other")


class RecordingResult(unittest.TextTestResult):
    """Keeps one record per test: outcome, duration and message."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records: list[dict] = []
        self._started: dict[str, float] = {}

    def startTest(self, test):
        self._started[test.id()] = time.perf_counter()
        super().startTest(test)

    def _record(self, test, status, message=""):
        started = self._started.pop(test.id(), None)
        self.records.append({
            "id": test.id(),
            "category": category_of(test.id()),
            "status": status,
            "seconds": round(time.perf_counter() - started, 4) if started else 0.0,
            "message": message,
        })

    def addSuccess(self, test):
        super().addSuccess(test)
        self._record(test, "passed")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._record(test, "failed", self._exc_info_to_string(err, test))

    def addError(self, test, err):
        super().addError(test, err)
        self._record(test, "error", self._exc_info_to_string(err, test))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._record(test, "skipped", reason)

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._record(test, "expected_failure", self._exc_info_to_string(err, test))

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._record(test, "unexpected_success")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            failed = issubclass(err[0], test.failureException)
            self._record(subtest, "failed" if failed else "error",
                         self._exc_info_to_string(err, test))


def iter_tests(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from iter_tests(item)
        else:
            yield item


def totals(records: list[dict]) -> dict:
    counts = defaultdict(int)
    for record in records:
        counts[record["status"]] += 1
    total = sum(counts.values())
    passed = counts["passed"] + counts["expected_failure"]
    return {"total": total, "passed": passed, "failed": counts["failed"],
            "errors": counts["error"], "skipped": counts["skipped"],
            "unexpected_success": counts["unexpected_success"],
            "pass_rate": round(passed / max(total - counts["skipped"], 1), 4)}


def write_junit(records: list[dict], path: Path) -> None:
    root = ET.Element("testsuites")
    by_category = defaultdict(list)
    for record in records:
        by_category[record["category"]].append(record)
    for category, items in sorted(by_category.items()):
        t = totals(items)
        suite = ET.SubElement(root, "testsuite", name=category, tests=str(t["total"]),
                              failures=str(t["failed"]), errors=str(t["errors"]),
                              skipped=str(t["skipped"]),
                              time=str(round(sum(i["seconds"] for i in items), 3)))
        for item in items:
            classname, _, name = item["id"].rpartition(".")
            case = ET.SubElement(suite, "testcase", classname=classname, name=name,
                                 time=str(item["seconds"]))
            if item["status"] == "failed":
                ET.SubElement(case, "failure", message=item["message"].splitlines()[-1][:200]).text = item["message"]
            elif item["status"] == "error":
                ET.SubElement(case, "error", message=item["message"].splitlines()[-1][:200]).text = item["message"]
            elif item["status"] == "skipped":
                ET.SubElement(case, "skipped", message=item["message"][:200])
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the AI Voice Appointment System tests.")
    parser.add_argument("--category", choices=sorted(CATEGORIES) + ["other"], action="append",
                        help="limit to one or more categories (repeatable)")
    parser.add_argument("--live", action="store_true",
                        help="also run live Firestore and Ollama tests")
    parser.add_argument("--list", action="store_true", help="list categories and exit")
    parser.add_argument("--quiet", action="store_true", help="only print the summary")
    parser.add_argument("--output", default="test_results",
                        help="basename for the JSON / XML files in reports/")
    args = parser.parse_args()

    if args.list:
        for category, modules in CATEGORIES.items():
            print(f"{category:11s} {', '.join(modules)}")
        return 0

    sys.stdout.reconfigure(encoding="utf-8")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    if args.live:
        os.environ["RUN_LIVE_TESTS"] = "1"
    os.chdir(REPO)
    sys.path.insert(0, str(REPO))

    discovered = unittest.defaultTestLoader.discover(str(REPO / "tests"), top_level_dir=str(REPO))
    selected = unittest.TestSuite(
        t for t in iter_tests(discovered)
        if not args.category or category_of(t.id()) in args.category)

    runner = unittest.TextTestRunner(resultclass=RecordingResult,
                                     verbosity=0 if args.quiet else 1, stream=sys.stderr)
    started = time.perf_counter()
    result = runner.run(selected)
    elapsed = time.perf_counter() - started

    records = result.records
    by_category = defaultdict(list)
    for record in records:
        by_category[record["category"]].append(record)

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "command": " ".join([Path(sys.executable).name] + sys.argv),
        "live": args.live,
        "seconds": round(elapsed, 2),
        "totals": totals(records),
        "categories": {cat: totals(items) for cat, items in sorted(by_category.items())},
        "tests": records,
    }
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / f"{args.output}.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False),
                                                 encoding="utf-8")
    write_junit(records, REPORTS / f"{args.output}.xml")

    line = "-" * 78
    print(f"\n{line}\n{'CATEGORY':12s} {'TOTAL':>6s} {'PASSED':>7s} {'FAILED':>7s} "
          f"{'SKIPPED':>8s} {'ERRORS':>7s}\n{line}")
    for category, t in summary["categories"].items():
        print(f"{category:12s} {t['total']:6d} {t['passed']:7d} {t['failed']:7d} "
              f"{t['skipped']:8d} {t['errors']:7d}")
    t = summary["totals"]
    print(f"{line}\n{'TOTAL':12s} {t['total']:6d} {t['passed']:7d} {t['failed']:7d} "
          f"{t['skipped']:8d} {t['errors']:7d}   pass rate {t['pass_rate']:.1%}   "
          f"({elapsed:.1f}s)")
    print(f"results -> {REPORTS / (args.output + '.json')}  |  {REPORTS / (args.output + '.xml')}")
    return 0 if t["failed"] == 0 and t["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
