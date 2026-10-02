"""LinkedIn discovery through the public *guest* endpoints (no login, no cookies).

  search : https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords=..&location=..&f_TPR=r604800&start=N
           returns ~10 HTML job cards per page (start = 0, 10, 20, ...)
  detail : https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}
           full description, criteria, and the Apply button flavour:
             - `public_jobs_apply-link-onsite`      -> LinkedIn Easy Apply   (ats="linkedin")
             - `apply-button__offsite-apply-icon`   -> external company site (ats from host if the URL is exposed)

Limitation (verified Oct 2026): for offsite jobs the guest page no longer embeds the external URL (the
old `<code id="applyUrl">` blob is gone; the button opens a sign-in modal). So offsite jobs get
apply_url = the LinkedIn posting, ats = "" and extra["apply_type"] = "offsite"; resolving the real
company URL needs a logged-in browser click (the apply worker's job). If LinkedIn ever exposes the URL
again (applyUrl code blob / externalApply link), it is picked up and ats is set from its host.

Throttling: one request at a time, >= `min_interval` seconds apart; 429/999 responses back off
exponentially (and pause the whole source). Remote search uses geoId=92000000 (Worldwide) + f_WT=2.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from urllib.parse import unquote, urlencode

import httpx
from selectolax.parser import HTMLParser

from jobagent.models import Job
from jobagent.sources.aggregators import ats_from_url, html_to_text, visa_note
from jobagent.sources.base import UA, SearchSpec, Source, title_matches

log = logging.getLogger(__name__)

BASE = "https://www.linkedin.com/jobs-guest/jobs/api"
WORLDWIDE_GEO = "92000000"

# label -> query params (location text works for India cities; "Remote" = worldwide + remote filter)
DEFAULT_LOCATIONS: dict[str, dict[str, str]] = {
    "India": {"location": "India"},
    "Bengaluru": {"location": "Bengaluru, Karnataka, India"},
    "Pune": {"location": "Pune, Maharashtra, India"},
    "Mumbai": {"location": "Mumbai, Maharashtra, India"},
    "Hyderabad": {"location": "Hyderabad, Telangana, India"},
    "Delhi NCR": {"location": "Delhi, India"},
    "Remote": {"location": "Worldwide", "geoId": WORLDWIDE_GEO, "f_WT": "2"},
}


class LinkedInSource(Source):
    name = "linkedin"
    needs_login = False

    def __init__(
        self,
        locations: dict[str, dict[str, str]] | None = None,
        keywords: list[str] | None = None,     # None -> spec.keywords
        max_pages: int = 10,                     # pages of ~10 cards per (keyword, location)
        tpr_seconds: int | None = 604800,       # f_TPR window; None -> derive from spec.max_age_days
        fetch_details: bool = True,
        max_details: int = 1_000_000,
        min_interval: float = 1.6,
        max_retries: int = 3,
    ):
        self.locations = locations or DEFAULT_LOCATIONS
        self.keywords = keywords
        self.max_pages = max_pages
        self.tpr_seconds = tpr_seconds
        self.fetch_details = fetch_details
        self.max_details = max_details
        self.min_interval = min_interval
        self.max_retries = max_retries
        self._lock = asyncio.Lock()
        self._last = 0.0

    # ---------------------------------------------------------------- http
    async def _get(self, client: httpx.AsyncClient, url: str) -> str | None:
        headers = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                   "Accept-Language": "en-US,en;q=0.9"}
        backoff = 15.0
        for attempt in range(self.max_retries + 1):
            async with self._lock:
                wait = self._last + self.min_interval - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    r = await client.get(url, headers=headers, timeout=30, follow_redirects=True)
                except httpx.HTTPError as e:
                    self._last = time.monotonic()
                    log.warning("linkedin: %s -> %s", url, e)
                    if attempt >= self.max_retries:
                        return None
                    await asyncio.sleep(3)
                    continue
                self._last = time.monotonic()
                if r.status_code in (429, 999) or (r.status_code >= 500):
                    if attempt >= self.max_retries:
                        log.warning("linkedin: giving up on %s (HTTP %s)", url, r.status_code)
                        return None
                    log.warning("linkedin: HTTP %s, backing off %.0fs", r.status_code, backoff)
                    await asyncio.sleep(backoff)  # hold the lock: pause the whole source
                    backoff *= 2
                    continue
                if r.status_code == 400 and "seeMoreJobPostings" in url:
                    return ""  # LinkedIn returns 400 past the last page
                if r.status_code != 200:
                    log.warning("linkedin: HTTP %s for %s", r.status_code, url)
                    return None
                return r.text
        return None

    # ---------------------------------------------------------------- fetch
    async def fetch(self, client: httpx.AsyncClient, spec: SearchSpec) -> list[Job]:
        jobs: dict[str, Job] = {}
        try:
            tpr = self.tpr_seconds or min(spec.max_age_days, 30) * 86400
            for kw in self.keywords or spec.keywords:
                for label, loc_params in self.locations.items():
                    for page in range(self.max_pages):
                        params = {"keywords": kw, **loc_params, "f_TPR": f"r{tpr}", "start": str(page * 10)}
                        html = await self._get(client, f"{BASE}/seeMoreJobPostings/search?{urlencode(params)}")
                        if not html:
                            break
                        cards, total = self._parse_cards(html, label, spec)
                        new = 0
                        for j in cards:
                            if j.external_id not in jobs:
                                jobs[j.external_id] = j
                                new += 1
                        if total < 10 or (cards and new == 0 and page > 0):
                            break
            log.info("linkedin: %d unique matching cards", len(jobs))
            if self.fetch_details:
                for j in list(jobs.values())[: self.max_details]:
                    try:
                        await self._enrich(client, j)
                    except Exception as e:
                        log.warning("linkedin: detail %s failed: %s", j.external_id, e)
        except Exception as e:  # never raise
            log.warning("linkedin: fetch aborted: %s", e)
        return list(jobs.values())

    def _parse_cards(self, html: str, label: str, spec: SearchSpec) -> tuple[list[Job], int]:
        """-> (title-matching jobs, total cards on the page)."""
        out: list[Job] = []
        tree = HTMLParser(html)
        cards = tree.css("div.base-search-card")
        for card in cards:
            try:
                urn = card.attributes.get("data-entity-urn") or ""
                jid = urn.rsplit(":", 1)[-1] if urn else ""
                link = card.css_first("a.base-card__full-link")
                href = (link.attributes.get("href") or "") if link else ""
                if not jid:
                    m = re.search(r"-(\d{6,})(?:\?|$)", href)
                    jid = m.group(1) if m else ""
                if not jid:
                    continue
                t = card.css_first(".base-search-card__title")
                title = t.text(strip=True) if t else ""
                if not title or not title_matches(title, spec):
                    continue
                c = card.css_first(".base-search-card__subtitle")
                company = c.text(strip=True) if c else ""
                lo = card.css_first(".job-search-card__location")
                location = lo.text(strip=True) if lo else ""
                tm = card.css_first("time")
                posted = tm.attributes.get("datetime") if tm else None
                sal = card.css_first(".job-search-card__salary-info")
                remote = True if label == "Remote" or "remote" in location.lower() else None
                out.append(Job(
                    source=self.name, external_id=jid, company=company, title=title,
                    url=f"https://www.linkedin.com/jobs/view/{jid}/", location=location, remote=remote,
                    posted_at=posted, salary=" ".join(sal.text().split()) if sal else "",
                    extra={"search_location": label},
                ))
            except Exception as e:
                log.warning("linkedin: card parse failed: %s", e)
        return out, len(cards)

    async def _enrich(self, client: httpx.AsyncClient, j: Job) -> None:
        html = await self._get(client, f"{BASE}/jobPosting/{j.external_id}")
        if not html:
            return
        tree = HTMLParser(html)
        d = tree.css_first(".show-more-less-html__markup") or tree.css_first(".description__text")
        if d:
            j.description = html_to_text(d.html)
        crit = {}
        for li in tree.css("li.description__job-criteria-item"):
            h, v = li.css_first("h3"), li.css_first("span")
            if h and v:
                crit[h.text(strip=True)] = v.text(strip=True)
        if crit:
            j.extra["criteria"] = crit
        pt = tree.css_first(".posted-time-ago__text")
        if pt:
            j.extra["posted_ago"] = pt.text(strip=True)
        na = tree.css_first(".num-applicants__caption")
        if na:
            j.extra["applicants"] = na.text(strip=True)
        if not j.salary:
            s = tree.css_first(".compensation__salary")
            if s:
                j.salary = s.text(strip=True)
        if j.remote is None and re.search(r"\b(remote|work from home|wfh)\b", j.description[:1500], re.I):
            j.extra["mentions_remote"] = True
        j.visa = visa_note(j.description)

        # Apply flavour
        ext = self._external_apply_url(html)
        if ext:
            j.apply_url, j.ats = ext, ats_from_url(ext)
            j.extra["apply_type"] = "offsite"
        elif "public_jobs_apply-link-onsite" in html:
            j.ats = "linkedin"
            j.extra["apply_type"] = "easy_apply"
        elif "offsite-apply-icon" in html or "apply-link-offsite" in html:
            j.ats = ""
            j.extra["apply_type"] = "offsite"
            j.extra["external_url_requires_login"] = True
        else:
            j.extra["apply_type"] = "unknown"

    @staticmethod
    def _external_apply_url(html: str) -> str:
        m = re.search(r'<code id="applyUrl"[^>]*>\s*<!--\s*"?(.*?)"?\s*-->', html, re.S)
        if m:
            u = m.group(1).strip()
            m2 = re.search(r"[?&]url=([^&]+)", u)
            return unquote(m2.group(1)) if m2 else u
        m = re.search(r'href="(https://www\.linkedin\.com/jobs/view/externalApply/[^"]+)"', html)
        if m:
            u = m.group(1).replace("&amp;", "&")
            m2 = re.search(r"[?&]url=([^&]+)", u)
            return unquote(m2.group(1)) if m2 else u
        return ""


SOURCES: list[Source] = [LinkedInSource()]
