"""APPLIED_JOBS.md: the human-readable record of every application, and the final word on duplicates.

Every submitted application is appended as a table row. Before a job is applied to, it is checked against
this file by (normalized company + role) and by normalized URL, so the same job is never applied to twice,
even if jobs.db is deleted or the same role shows up on LinkedIn, Naukri and the company's own ATS.
You can add rows by hand for jobs you applied to yourself; they are respected too.
"""
from __future__ import annotations

import re
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse

from jobagent.models import Job, normalize_company, normalize_title

HEADER = """# Applied jobs

Maintained by jobagent. One row per submitted application; rows are never removed.
Add your own rows (same columns) for jobs you applied to manually, and the agent will skip them.

| # | Date | Company | Role | Location | Source | Link | Notes |
|---|------|---------|------|----------|--------|------|-------|
"""

_TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "ref", "source", "src",
             "gh_src", "lever-source", "trk", "refId", "trackingId"}


def normalize_url(url: str) -> str:
    u = urlparse(url.strip())
    q = urlencode(sorted((k, v) for k, v in parse_qsl(u.query) if k not in _TRACKING))
    return f"{(u.hostname or '').removeprefix('www.')}{u.path.rstrip('/')}{'?' + q if q else ''}".lower()


def _cell(s: str) -> str:
    return " ".join(str(s or "").replace("|", "/").split())[:140]


class Ledger:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        if not path.exists():
            path.write_text(HEADER)
        self.reload()

    def reload(self) -> None:
        self.keys: set[str] = set()
        self.urls: set[str] = set()
        self.count = 0
        for line in self.path.read_text().splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 7 or not cells[0].isdigit():
                continue
            self.count = max(self.count, int(cells[0]))
            _, _, company, role, _, _, link = cells[:7]
            self.keys.add(f"{normalize_company(company)}|{normalize_title(role)}")
            for m in re.findall(r"\((https?://[^)\s]+)\)", link) or ([link] if link.startswith("http") else []):
                self.urls.add(normalize_url(m))

    def has(self, job: Job) -> bool:
        return (f"{normalize_company(job.company)}|{normalize_title(job.title)}" in self.keys
                or normalize_url(job.url) in self.urls or normalize_url(job.apply_url) in self.urls)

    def record(self, job: Job, notes: str = "") -> None:
        with self.lock:
            if self.has(job):
                return
            self.count += 1
            row = (f"| {self.count} | {datetime.now():%Y-%m-%d %H:%M} | {_cell(job.company)} | {_cell(job.title)} | "
                   f"{_cell(job.location)} | {_cell(job.source)} | [posting]({job.url}) | {_cell(notes)} |\n")
            with self.path.open("a") as f:
                f.write(row)
            self.keys.add(f"{normalize_company(job.company)}|{normalize_title(job.title)}")
            self.urls.update({normalize_url(job.url), normalize_url(job.apply_url)})
