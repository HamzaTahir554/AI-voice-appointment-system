"""
Security checks (system audit section 28).

These guard the mistakes that actually happened on this project: a real
Firebase private key pasted into the shareable .env.example, and credentials
that must never reach source, logs, reports or the PDF.

Secret stores that are ALLOWED to hold credentials - `.env` and the
service-account JSON - are checked for the opposite property instead: git must
ignore them.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", "__pycache__", "models", ".venv", "venv", "node_modules"}
BINARY_SUFFIXES = {".safetensors", ".bin", ".pt", ".png", ".jpg", ".jpeg", ".pdf",
                   ".pyc", ".xlsx", ".zip"}

PEM_BODY = re.compile(r"BEGIN (?:RSA )?PRIVATE KEY-----(?:\\n|\s)+[A-Za-z0-9+/]{40,}")
SERVICE_ACCOUNT = re.compile(r"[a-z0-9-]+@[a-z0-9-]+\.iam\.gserviceaccount\.com")
# The one project report. It is scanned like source code: a report that
# quotes a key is as much a leak as a commit that contains one.
PDF_REPORT = ROOT / "docs" / "AI_Voice_Appointment_System_Final_Report.pdf"


def is_secret_store(path: Path) -> bool:
    return path.name == ".env" or "firebase-adminsdk" in path.name


def project_text_files():
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in BINARY_SUFFIXES or path.stat().st_size > 5_000_000:
            continue
        yield path


class TestNoCredentialsInShareableFiles(unittest.TestCase):
    def test_env_example_holds_placeholders_only(self):
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIsNone(PEM_BODY.search(text), ".env.example contains a real private key")
        for email in SERVICE_ACCOUNT.findall(text):
            self.assertTrue("xxxx" in email or "your-" in email,
                            ".env.example contains a real service-account address")

    def test_no_key_material_outside_the_secret_stores(self):
        offenders = []
        for path in project_text_files():
            if is_secret_store(path):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if PEM_BODY.search(text):
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [], "private key material found in shareable files")

    def test_the_pdf_report_contains_no_credentials(self):
        if not PDF_REPORT.exists():
            self.skipTest("the PDF report has not been generated yet")
        from pypdf import PdfReader
        text = "\n".join(page.extract_text() or "" for page in PdfReader(str(PDF_REPORT)).pages)
        self.assertNotIn("PRIVATE KEY-----", text)
        self.assertNotIn("fbsvc@", text)
        self.assertIsNone(PEM_BODY.search(text))
        self.assertIsNone(SERVICE_ACCOUNT.search(text))

    def test_credentials_are_never_interpolated_into_output(self):
        interpolated = re.compile(r"\{\s*(FIREBASE_PRIVATE_KEY|private_key)\b")
        passed = re.compile(r"(print|logger\.\w+|logging\.\w+)\([^\n]*,\s*"
                            r"(FIREBASE_PRIVATE_KEY|private_key)\b")
        offenders = []
        for path in ROOT.rglob("*.py"):
            if any(part in SKIP_DIRS for part in path.parts) or path.name == Path(__file__).name:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if interpolated.search(line) or passed.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{number}")
        self.assertEqual(offenders, [])


class TestSecretStoresAreIgnoredByGit(unittest.TestCase):
    REQUIRED_PATTERNS = (".env", "*.json", "__pycache__/", ".venv/", "models/")

    def test_gitignore_lists_the_required_patterns(self):
        lines = {line.strip() for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()}
        for pattern in self.REQUIRED_PATTERNS:
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, lines)

    def test_git_would_ignore_every_secret_store(self):
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        stores = [ROOT / ".env"] + list((ROOT / "firebase").glob("*firebase-adminsdk*.json"))
        for store in stores:
            if not store.exists():
                continue
            with self.subTest(file=store.name):
                result = subprocess.run(["git", "check-ignore", "--no-index", "-q", str(store)],
                                        cwd=ROOT, capture_output=True)
                if result.returncode == 128:
                    self.skipTest("not inside any git repository")
                self.assertEqual(result.returncode, 0, f"{store.name} is NOT git-ignored")

    def test_shareable_files_are_not_ignored(self):
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        for name in (".env.example", "intent_detection/data/label2id.json", "config.py"):
            with self.subTest(file=name):
                result = subprocess.run(["git", "check-ignore", "--no-index", "-q", str(ROOT / name)],
                                        cwd=ROOT, capture_output=True)
                if result.returncode == 128:
                    self.skipTest("not inside any git repository")
                self.assertEqual(result.returncode, 1, f"{name} would be ignored")


if __name__ == "__main__":
    unittest.main()
