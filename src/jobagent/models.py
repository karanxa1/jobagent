from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from enum import StrEnum


class Status(StrEnum):
    NEW = "new"                    # discovered, not yet triaged
    REJECTED = "rejected"          # triage said not a fit
    QUEUED = "queued"              # passed triage, waiting for a browser worker
    IN_PROGRESS = "in_progress"
    APPLIED = "applied"            # submitted and verified
    READY = "ready"                # dry run: form filled, stopped before submit
    NEEDS_HUMAN = "needs_human"    # captcha, unknown question, login wall, etc.
    FAILED = "failed"
    SKIPPED = "skipped"            # duplicate company/role, closed posting, ...


_COMPANY_SUFFIX = re.compile(r"\b(inc|incorporated|llc|ltd|limited|pvt|private|corp|corporation|co|gmbh|plc|"
                             r"technologies|technology|labs|ai|hq|india)\b\.?")


def normalize_company(name: str) -> str:
    n = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower())
    return " ".join(_COMPANY_SUFFIX.sub(" ", n).split()) or n.strip()


def company_key(name: str) -> str:
    """Looser identity for per-company limits: "WisdomAI" == "Wisdom AI" == "Wisdom Ai Inc" -> "wisdom"."""
    k = normalize_company(name).replace(" ", "")
    return re.sub(r"(ai|hq|labs?|inc)$", "", k) or k


def normalize_title(title: str) -> str:
    t = re.sub(r"\(.*?\)|\[.*?\]", " ", (title or "").lower())   # drop "(Remote)", "[India]"
    t = re.sub(r"\b(sr)\b\.?", "senior", t)
    t = re.sub(r"\b(jr)\b\.?", "junior", t)
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return " ".join(t.split())


@dataclass
class Job:
    source: str                    # e.g. "greenhouse", "yc", "a16z", "linkedin"
    external_id: str               # id unique within the source
    company: str
    title: str
    url: str                       # human-readable posting URL
    apply_url: str = ""            # where the application form lives (defaults to url)
    location: str = ""
    remote: bool | None = None
    description: str = ""          # plain text, may be truncated
    ats: str = ""                  # greenhouse | lever | ashby | workable | linkedin | naukri | ... | "" (unknown)
    posted_at: str | None = None   # ISO-8601 if known
    salary: str = ""
    visa: str = ""                 # anything the source says about sponsorship
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.apply_url = self.apply_url or self.url

    @property
    def key(self) -> str:
        return f"{self.source}:{self.external_id}"

    @property
    def fingerprint(self) -> str:
        """Same role at the same company seen through different sources dedupes to one fingerprint."""
        return hashlib.sha1(f"{normalize_company(self.company)}|{normalize_title(self.title)}".encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ApplyResult:
    status: Status
    summary: str = ""
    screenshot: str | None = None
    answers: dict = field(default_factory=dict)  # questions asked by the form -> what we answered
