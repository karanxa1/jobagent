from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import httpx

from jobagent.models import Job

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"


@dataclass
class SearchSpec:
    """What we're looking for. Sources use what they can; triage does the precise filtering later."""
    keywords: list[str] = field(default_factory=lambda: [
        "AI Engineer", "ML Engineer", "Machine Learning Engineer", "LLM Engineer",
        "Applied AI Engineer", "GenAI Engineer", "AI Platform Engineer", "Agent Engineer",
        "Forward Deployed Engineer AI", "Applied Scientist",
    ])
    locations: list[str] = field(default_factory=lambda: ["India", "Remote"])
    max_age_days: int = 45


class Source(ABC):
    """A job source. `fetch` must never raise for one bad page/company: log and keep going."""

    name: str = "base"
    needs_login: bool = False  # True for LinkedIn/Naukri/etc. (searched through a logged-in browser profile)

    @abstractmethod
    async def fetch(self, client: httpx.AsyncClient, spec: SearchSpec) -> list[Job]: ...


def title_matches(title: str, spec: SearchSpec) -> bool:
    """Cheap prefilter so sources don't ship thousands of irrelevant rows to triage."""
    t = title.lower()
    hints = ("ai", "a.i.", "ml", "machine learning", "llm", "genai", "gen ai", "generative",
             "deep learning", "applied scien", "nlp", "agent", "inference", "mlops", "data scien",
             "forward deployed", "research engineer", "computer vision", "rag")
    words = set(t.replace("/", " ").replace(",", " ").replace("-", " ").split())
    return any((h in words) if len(h) <= 3 else (h in t) for h in hints)
