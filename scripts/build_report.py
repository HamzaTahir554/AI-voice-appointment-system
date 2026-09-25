"""
Build the final project report.

    python scripts/build_report.py

Renders docs/report/final_report.html to
docs/AI_Voice_Appointment_System_Final_Report.pdf with headless Chrome (or
Edge), then checks the result: it must open, have pages, and contain no
private key or service-account address. The HTML is the source of the report;
edit it and run this again.

The figures the report shows are the real training outputs in
intent_detection/outputs/, referenced from the HTML by relative path.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "docs" / "report" / "final_report.html"
OUTPUT = REPO / "docs" / "AI_Voice_Appointment_System_Final_Report.pdf"

BROWSERS = [
    "chrome", "google-chrome", "chromium", "msedge",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

PRIVATE_KEY = re.compile(r"PRIVATE KEY-----")
SERVICE_ACCOUNT = re.compile(r"[a-z0-9-]+@[a-z0-9-]+\.iam\.gserviceaccount\.com")


def find_browser() -> str | None:
    for candidate in BROWSERS:
        found = shutil.which(candidate) or (candidate if Path(candidate).exists() else None)
        if found:
            return found
    return None


def render(browser: str) -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    command = [
        browser, "--headless=new", "--disable-gpu", "--no-first-run",
        "--no-pdf-header-footer",
        "--print-to-pdf=" + str(OUTPUT),
        SOURCE.resolve().as_uri(),
    ]
    subprocess.run(command, check=True, capture_output=True, timeout=180)


def verify() -> int:
    """Open the PDF and make sure it is a report and not a leak."""
    try:
        from pypdf import PdfReader
    except ImportError:
        print("pypdf is not installed; skipping the content check")
        return 0

    reader = PdfReader(str(OUTPUT))
    pages = len(reader.pages)
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    if pages < 5 or "AI Voice Appointment System" not in text:
        print("the PDF does not look like the report", file=sys.stderr)
        return 1
    if PRIVATE_KEY.search(text) or SERVICE_ACCOUNT.search(text):
        print("the PDF contains credential material - not publishing it", file=sys.stderr)
        OUTPUT.unlink()
        return 1
    print(f"{OUTPUT.relative_to(REPO)}: {pages} pages, "
          f"{OUTPUT.stat().st_size / 1024:.0f} KB, no credential material found")
    return 0


def main() -> int:
    if not SOURCE.exists():
        print(f"missing report source: {SOURCE}", file=sys.stderr)
        return 1
    browser = find_browser()
    if browser is None:
        print("Chrome or Edge is needed to print the report to PDF", file=sys.stderr)
        return 1
    render(browser)
    return verify()


if __name__ == "__main__":
    sys.exit(main())
