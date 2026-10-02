"""Free public job feeds that carry AI/ML roles. All verified to return data without login (Oct 2026).

Sources in this module:
  - HNWhoIsHiringSource   Hacker News "Ask HN: Who is hiring?" (latest threads) via the Algolia HN API
  - RemoteOKSource        https://remoteok.com/api
  - RemotiveSource        https://remotive.com/api/remote-jobs (the public feed is small, ~20 rows)
  - WeWorkRemotelySource  RSS feeds
  - HimalayasSource       https://himalayas.app/jobs/api/search (keyword + country filters)
  - ArbeitnowSource       https://www.arbeitnow.com/api/job-board-api?visa_sponsorship=true (EU, visa sponsors)
  - JobicySource          https://jobicy.com/api/v2/remote-jobs (not in SOURCES: 0 India-eligible AI rows)
  - WorkingNomadsSource   https://www.workingnomads.com/api/exposed_jobs/
  - HiristSource          hirist.tech (India tech board) via its public JSON backend (gladiator.hirist.tech)

Tried and dropped: Cutshort (no public JSON; search pages are client-rendered behind Cloudflare and the
backend route returns "page config not found" without a session).

This module also holds small helpers shared by the other source modules (html_to_text, ats_from_url,
visa_note, ...). Feeds that require attribution (RemoteOK, Jobicy, Arbeitnow) keep the source's own URL
as `url` so links back are preserved.
"""
from __future__ import annotations

import asyncio
import html as _html
import logging
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import quote_plus, urlparse

import httpx
from selectolax.parser import HTMLParser

from jobagent.models import Job
from jobagent.sources.base import UA, SearchSpec, Source, title_matches

log = logging.getLogger(__name__)

DESC_MAX = 6000

# --------------------------------------------------------------------------------------------------
# shared helpers (imported by linkedin/naukri/wellfound/instahyre)
# --------------------------------------------------------------------------------------------------

_ATS_HOSTS = [
    ("greenhouse.io", "greenhouse"), ("lever.co", "lever"), ("ashbyhq.com", "ashby"),
    ("workable.com", "workable"), ("smartrecruiters.com", "smartrecruiters"),
    ("myworkdayjobs.com", "workday"), ("myworkdaysite.com", "workday"), ("workday.com", "workday"),
    ("icims.com", "icims"), ("bamboohr.com", "bamboohr"), ("recruitee.com", "recruitee"),
    ("jobvite.com", "jobvite"), ("breezy.hr", "breezy"), ("teamtailor.com", "teamtailor"),
    ("personio.", "personio"), ("successfactors", "successfactors"), ("oraclecloud.com", "oracle"),
    ("taleo.net", "taleo"), ("zohorecruit", "zoho"), ("keka.com", "keka"), ("darwinbox", "darwinbox"),
    ("freshteam.com", "freshteam"), ("rippling.com", "rippling"), ("gem.com", "gem"),
    ("wellfound.com", "wellfound"), ("angel.co", "wellfound"), ("ycombinator.com", "yc"),
    ("linkedin.com", "linkedin"), ("naukri.com", "naukri"), ("instahyre.com", "instahyre"),
    ("hirist.tech", "hirist"), ("cutshort.io", "cutshort"), ("jobs.workable", "workable"),
    ("dover.com", "dover"), ("pinpointhq.com", "pinpoint"), ("jazzhr.com", "jazzhr"),
    ("applytojob.com", "jazzhr"), ("hire.trakstar.com", "trakstar"), ("comeet.", "comeet"),
]


def ats_from_url(url: str) -> str:
    """Best-effort ATS name from an apply URL's host; falls back to the bare registrable-ish host."""
    if not url:
        return ""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return ""
    for needle, name in _ATS_HOSTS:
        if needle in host:
            return name
    host = host.removeprefix("www.")
    return host


def html_to_text(s: str | None, limit: int = DESC_MAX) -> str:
    """HTML (or HTML-escaped HTML) -> compact plain text, truncated to `limit` chars."""
    if not s:
        return ""
    if "&lt;" in s:  # escaped markup (RSS, some APIs)
        s = _html.unescape(s)
    try:
        s = re.sub(r"(?i)<p\b", "\n<p", s)
        s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>|</div>|</tr>", "\n", s)
        s = re.sub(r"(?i)<li[^>]*>", "\n- ", s)
        text = HTMLParser(s).text(separator="")
    except Exception:
        text = re.sub(r"<[^>]+>", " ", s)
    text = _html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()[:limit]


_VISA_RE = re.compile(
    r"[^.\n]{0,120}\b(visa|sponsorship|sponsor|h-?1b|work permit|relocation (?:support|assistance|package))\b[^.\n]{0,120}",
    re.I,
)


def visa_note(text: str) -> str:
    """Return the sentence fragment mentioning visa sponsorship / relocation, if any."""
    if not text:
        return ""
    m = _VISA_RE.search(text)
    return " ".join(m.group(0).split())[:300] if m else ""


def ts_to_iso(ts: float | int | str | None) -> str | None:
    """Unix seconds or milliseconds -> ISO-8601 UTC."""
    if ts in (None, "", 0):
        return None
    try:
        v = float(ts)
        if v > 1e12:
            v /= 1000.0
        return datetime.fromtimestamp(v, UTC).isoformat()
    except Exception:
        return None


def too_old(iso: str | None, max_age_days: int) -> bool:
    if not iso:
        return False
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return datetime.now(UTC) - dt > timedelta(days=max_age_days)
    except Exception:
        return False


_ELIGIBLE = ("worldwide", "anywhere", "global", "india", "asia", "apac", "emea", "international")


def remote_ok_for_india(location: str) -> bool:
    """Coarse geo prefilter for remote feeds: drop only postings explicitly restricted elsewhere."""
    loc = (location or "").lower().strip()
    if not loc or loc in ("remote", "remote job"):
        return True
    return any(k in loc for k in _ELIGIBLE)


async def get_json(client: httpx.AsyncClient, url: str, **kw):
    headers = {"User-Agent": UA, "Accept": "application/json", **kw.pop("headers", {})}
    r = await client.get(url, headers=headers, timeout=kw.pop("timeout", 30), follow_redirects=True, **kw)
    r.raise_for_status()
    return r.json()


async def get_text(client: httpx.AsyncClient, url: str, **kw) -> str:
    headers = {"User-Agent": UA, **kw.pop("headers", {})}
    r = await client.get(url, headers=headers, timeout=kw.pop("timeout", 30), follow_redirects=True, **kw)
    r.raise_for_status()
    return r.text


# --------------------------------------------------------------------------------------------------
# Hacker News: Who is hiring?
# --------------------------------------------------------------------------------------------------

_AI_RE = re.compile(r"\b(ai|ml|llms?|genai|machine learning|deep learning|nlp|agents?|agentic|applied scientist|"
                    r"computer vision|inference|rag|mlops|foundation models?)\b", re.I)
_GEO_RE = re.compile(r"\b(remote|india|bangalore|bengaluru|pune|hyderabad|mumbai|delhi|gurgaon|noida|chennai|"
                     r"visa|sponsor\w*|relocat\w*|worldwide|anywhere|global)\b", re.I)
_US_ONLY_RE = re.compile(r"remote\s*\(?\s*(us|usa|u\.s\.|united states|us[- ]only|north america|canada|eu|europe|uk)\b"
                         r"|\b(us|usa)[- ]only\b|us time ?zones?|must be (?:located|based) in the (?:us|u\.s\.)", re.I)
_ROLE_RE = re.compile(r"engineer|scientist|developer|researcher|architect|\blead\b|head of|manager|director|"
                      r"\bmts\b|member of technical staff|founding|swe\b|programmer|specialist", re.I)
_NO_VISA_RE = re.compile(r"\b(no|not|unable|cannot|can't|don't|do not|won't|without)\b", re.I)
_URL_RE = re.compile(r"https?://[^\s<>\"')]+")


class HNWhoIsHiringSource(Source):
    """Latest 'Ask HN: Who is hiring?' threads. One Job per matching top-level comment."""

    name = "hn_whoishiring"

    def __init__(self, threads: int = 2):
        self.threads = threads

    async def fetch(self, client: httpx.AsyncClient, spec: SearchSpec) -> list[Job]:
        jobs: list[Job] = []
        try:
            d = await get_json(client, "https://hn.algolia.com/api/v1/search_by_date"
                                       "?tags=story,author_whoishiring&hitsPerPage=10")
            stories = [h for h in d.get("hits", []) if "who is hiring" in (h.get("title") or "").lower()]
        except Exception as e:
            log.warning("hn: thread lookup failed: %s", e)
            return jobs
        for st in stories[: self.threads]:
            try:
                item = await get_json(client, f"https://hn.algolia.com/api/v1/items/{st['objectID']}", timeout=60)
            except Exception as e:
                log.warning("hn: thread %s failed: %s", st.get("objectID"), e)
                continue
            for c in item.get("children") or []:
                try:
                    j = self._parse(c, spec)
                    if j:
                        jobs.append(j)
                except Exception as e:
                    log.warning("hn: comment %s parse failed: %s", c.get("id"), e)
        return jobs

    def _parse(self, c: dict, spec: SearchSpec) -> Job | None:
        raw = c.get("text") or ""
        if not raw or c.get("type") != "comment":
            return None
        posted = c.get("created_at")
        if too_old(posted, spec.max_age_days):
            return None
        text = html_to_text(raw, 20000)
        if not _AI_RE.search(text) or not _GEO_RE.search(text):
            return None
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        header = lines[0] if lines else ""
        if re.search(r"hire me|seeking (?:work|a role)|looking for (?:work|a job)", header, re.I):
            return None
        segs = [s.strip() for s in re.split(r"\s+[|—–]\s+|\s+-\s+|\|", header) if s.strip()]
        company = re.sub(r"\s*\(.*?\)\s*", " ", segs[0]).strip() if segs else ""
        def role_like(x: str) -> bool:
            return len(x) < 100 and "http" not in x and bool(_ROLE_RE.search(x)) and title_matches(x, spec)
        title = next((s for s in segs[1:] if role_like(s)), "")
        if not title:  # roles are often listed in the body
            title = next((ln.lstrip("-*• ").strip() for ln in lines[1:40] if role_like(ln.lstrip("-*• "))), "")
        if not title:
            return None
        lower = text.lower()
        visa = visa_note(text)
        positive_visa = bool(visa) and not _NO_VISA_RE.search(visa)
        if _US_ONLY_RE.search(header) and not (positive_visa or "india" in lower or "worldwide" in lower or "anywhere" in lower):
            return None
        geo_segs = [s for s in segs[1:] if _GEO_RE.search(s) or re.search(r"onsite|hybrid|, [A-Z]{2}\b", s, re.I)]
        location = "; ".join(geo_segs)[:200]
        urls = [u.rstrip(".,);") for u in _URL_RE.findall(_html.unescape(raw).replace("&#x2F;", "/"))]
        apply = next((u for u in urls if "news.ycombinator.com" not in u), "")
        cid = str(c.get("id"))
        return Job(
            source=self.name, external_id=cid, company=company[:100] or (c.get("author") or ""),
            title=title[:150], url=f"https://news.ycombinator.com/item?id={cid}", apply_url=apply,
            location=location, remote=("remote" in header.lower()) or None, description=text[:DESC_MAX],
            ats=ats_from_url(apply) if apply else "", posted_at=posted, visa=visa,
            extra={"hn_author": c.get("author"), "thread": c.get("story_id")},
        )


# --------------------------------------------------------------------------------------------------
# RemoteOK
# --------------------------------------------------------------------------------------------------

class RemoteOKSource(Source):
    name = "remoteok"

    async def fetch(self, client: httpx.AsyncClient, spec: SearchSpec) -> list[Job]:
        jobs: list[Job] = []
        try:
            data = await get_json(client, "https://remoteok.com/api")
        except Exception as e:
            log.warning("remoteok: %s", e)
            return jobs
        for it in data:
            try:
                if not isinstance(it, dict) or "position" not in it:
                    continue
                title = it.get("position") or ""
                if not title_matches(title, spec):
                    continue
                loc = (it.get("location") or "").strip()
                if not remote_ok_for_india(loc):
                    continue
                posted = it.get("date") or ts_to_iso(it.get("epoch"))
                if too_old(posted, spec.max_age_days):
                    continue
                desc = html_to_text(it.get("description"))
                sal = ""
                if it.get("salary_min") and it.get("salary_max") and str(it["salary_max"]) != "0":
                    sal = f"USD {it['salary_min']}-{it['salary_max']}"
                url = it.get("url") or f"https://remoteok.com/remote-jobs/{it.get('id')}"
                jobs.append(Job(
                    source=self.name, external_id=str(it.get("id")), company=it.get("company") or "",
                    title=title, url=url, apply_url=it.get("apply_url") or url, location=loc or "Worldwide",
                    remote=True, description=desc, ats="", posted_at=posted, salary=sal, visa=visa_note(desc),
                    extra={"tags": it.get("tags")},
                ))
            except Exception as e:
                log.warning("remoteok: row failed: %s", e)
        return jobs


# --------------------------------------------------------------------------------------------------
# Remotive
# --------------------------------------------------------------------------------------------------

class RemotiveSource(Source):
    """Remotive's public API currently serves a small delayed sample (~20 jobs) regardless of filters."""

    name = "remotive"

    async def fetch(self, client: httpx.AsyncClient, spec: SearchSpec) -> list[Job]:
        jobs: list[Job] = []
        seen: set[str] = set()
        for url in ("https://remotive.com/api/remote-jobs?category=ai-ml",
                    "https://remotive.com/api/remote-jobs?category=software-dev"):
            try:
                data = await get_json(client, url)
            except Exception as e:
                log.warning("remotive: %s", e)
                continue
            for it in data.get("jobs", []):
                try:
                    jid = str(it.get("id"))
                    title = it.get("title") or ""
                    if jid in seen or not title_matches(title, spec):
                        continue
                    seen.add(jid)
                    loc = it.get("candidate_required_location") or ""
                    if not remote_ok_for_india(loc):
                        continue
                    posted = it.get("publication_date")
                    if too_old(posted, spec.max_age_days):
                        continue
                    desc = html_to_text(it.get("description"))
                    jobs.append(Job(
                        source=self.name, external_id=jid, company=it.get("company_name") or "", title=title,
                        url=it.get("url") or "", location=loc or "Worldwide", remote=True, description=desc,
                        posted_at=posted, salary=it.get("salary") or "", visa=visa_note(desc),
                        extra={"job_type": it.get("job_type"), "category": it.get("category")},
                    ))
                except Exception as e:
                    log.warning("remotive: row failed: %s", e)
        return jobs


# --------------------------------------------------------------------------------------------------
# We Work Remotely (RSS)
# --------------------------------------------------------------------------------------------------

class WeWorkRemotelySource(Source):
    name = "weworkremotely"
    FEEDS = ("https://weworkremotely.com/remote-jobs.rss",
             "https://weworkremotely.com/categories/remote-programming-jobs.rss",
             "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
             "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss")

    async def fetch(self, client: httpx.AsyncClient, spec: SearchSpec) -> list[Job]:
        jobs: dict[str, Job] = {}
        for feed in self.FEEDS:
            try:
                xml = await get_text(client, feed)
            except Exception as e:
                log.warning("wwr: %s: %s", feed, e)
                continue
            for item in re.findall(r"<item>(.*?)</item>", xml, re.S):
                try:
                    def tag(name: str, item: str = item) -> str:
                        m = re.search(rf"<{name}>(.*?)</{name}>", item, re.S)
                        v = (m.group(1) if m else "").strip()
                        v = re.sub(r"^<!\[CDATA\[(.*)\]\]>$", r"\1", v, flags=re.S)
                        return _html.unescape(v).strip()
                    raw_title = tag("title")
                    company, _, title = raw_title.partition(":")
                    if not title:
                        company, title = "", raw_title
                    title = title.strip()
                    if not title_matches(title, spec):
                        continue
                    link = tag("link") or tag("guid")
                    if link in jobs:
                        continue
                    region = tag("region")
                    if not remote_ok_for_india(region):
                        continue
                    posted = None
                    if pd := tag("pubDate"):
                        try:
                            posted = datetime.strptime(pd, "%a, %d %b %Y %H:%M:%S %z").isoformat()
                        except ValueError:
                            pass
                    if too_old(posted, spec.max_age_days):
                        continue
                    desc = html_to_text(tag("description"))
                    jobs[link] = Job(
                        source=self.name, external_id=link.rstrip("/").rsplit("/", 1)[-1], company=company.strip(),
                        title=title, url=link, location=region or "Anywhere", remote=True, description=desc,
                        posted_at=posted, visa=visa_note(desc), extra={"category": tag("category")},
                    )
                except Exception as e:
                    log.warning("wwr: item failed: %s", e)
        return list(jobs.values())


# --------------------------------------------------------------------------------------------------
# Himalayas
# --------------------------------------------------------------------------------------------------

class HimalayasSource(Source):
    """Himalayas search API: keyword `q`, plus `country=India` (jobs open to India) or `worldwide=true`."""

    name = "himalayas"

    def __init__(self, queries: list[str] | None = None, max_pages: int = 10):
        self.queries = queries or ["AI engineer", "machine learning engineer", "LLM", "generative AI", "applied scientist"]
        self.max_pages = max_pages

    async def fetch(self, client: httpx.AsyncClient, spec: SearchSpec) -> list[Job]:
        jobs: dict[str, Job] = {}
        for q in self.queries:
            for geo in ("country=India", "worldwide=true"):
                for page in range(1, self.max_pages + 1):
                    url = f"https://himalayas.app/jobs/api/search?q={quote_plus(q)}&{geo}&page={page}"
                    try:
                        data = await get_json(client, url)
                    except Exception as e:
                        log.warning("himalayas: %s: %s", url, e)
                        break
                    rows = data.get("jobs") or []
                    for it in rows:
                        try:
                            self._add(it, spec, jobs, geo)
                        except Exception as e:
                            log.warning("himalayas: row failed: %s", e)
                    if len(rows) < 20:
                        break
                    await asyncio.sleep(0.3)
        return list(jobs.values())

    def _add(self, it: dict, spec: SearchSpec, jobs: dict[str, Job], geo: str) -> None:
        title = it.get("title") or ""
        url = it.get("guid") or it.get("applicationLink") or ""
        if not url or url in jobs or not title_matches(title, spec):
            return
        posted = ts_to_iso(it.get("pubDate"))
        if too_old(posted, spec.max_age_days):
            return
        restr = it.get("locationRestrictions") or []
        if restr and "India" not in restr and geo != "worldwide=true":
            return
        loc = "Worldwide" if not restr else ("India-eligible remote" if "India" in restr else ", ".join(restr[:8]))
        desc = html_to_text(it.get("description"))
        sal = ""
        if it.get("minSalary"):
            sal = f"{it.get('currency', '')} {it.get('minSalary')}-{it.get('maxSalary') or ''} {it.get('salaryPeriod') or ''}".strip()
        jobs[url] = Job(
            source=self.name, external_id=url.rstrip("/").rsplit("/", 1)[-1], company=it.get("companyName") or "",
            title=title, url=url, apply_url=it.get("applicationLink") or url, location=loc, remote=True,
            description=desc, ats="himalayas", posted_at=posted, salary=sal, visa=visa_note(desc),
            extra={"seniority": it.get("seniority"), "employment_type": it.get("employmentType"),
                   "timezones": it.get("timezoneRestrictions")},
        )


# --------------------------------------------------------------------------------------------------
# Arbeitnow (EU; filtered to visa-sponsoring employers)
# --------------------------------------------------------------------------------------------------

class ArbeitnowSource(Source):
    name = "arbeitnow"

    def __init__(self, max_pages: int = 20):
        self.max_pages = max_pages

    async def fetch(self, client: httpx.AsyncClient, spec: SearchSpec) -> list[Job]:
        jobs: list[Job] = []
        for page in range(1, self.max_pages + 1):
            try:
                data = await get_json(client, f"https://www.arbeitnow.com/api/job-board-api?visa_sponsorship=true&page={page}")
            except Exception as e:
                log.warning("arbeitnow: page %d: %s", page, e)
                break
            rows = data.get("data") or []
            for it in rows:
                try:
                    title = it.get("title") or ""
                    if not title_matches(title, spec):
                        continue
                    posted = ts_to_iso(it.get("created_at"))
                    if too_old(posted, spec.max_age_days):
                        continue
                    desc = html_to_text(it.get("description"))
                    slug = it.get("slug") or ""
                    url = it.get("url") or f"https://www.arbeitnow.com/view/{slug}"
                    jobs.append(Job(
                        source=self.name, external_id=slug, company=it.get("company_name") or "", title=title,
                        url=url, location=it.get("location") or "", remote=bool(it.get("remote")),
                        description=desc, ats="arbeitnow", posted_at=posted,
                        visa=visa_note(desc) or "Listed under Arbeitnow visa_sponsorship=true",
                        extra={"tags": it.get("tags"), "job_types": it.get("job_types")},
                    ))
                except Exception as e:
                    log.warning("arbeitnow: row failed: %s", e)
            if not (data.get("links") or {}).get("next"):
                break
        return jobs


# --------------------------------------------------------------------------------------------------
# Jobicy
# --------------------------------------------------------------------------------------------------

class JobicySource(Source):
    name = "jobicy"

    async def fetch(self, client: httpx.AsyncClient, spec: SearchSpec) -> list[Job]:
        jobs: dict[str, Job] = {}
        for url in ("https://jobicy.com/api/v2/remote-jobs?count=100&industry=data-science",
                    "https://jobicy.com/api/v2/remote-jobs?count=100&industry=engineering",
                    "https://jobicy.com/api/v2/remote-jobs?count=100&geo=apac",
                    "https://jobicy.com/api/v2/remote-jobs?count=100"):
            try:
                data = await get_json(client, url)
            except Exception as e:
                log.warning("jobicy: %s: %s", url, e)
                continue
            for it in data.get("jobs") or []:
                try:
                    jid = str(it.get("id"))
                    title = _html.unescape(it.get("jobTitle") or "")
                    if jid in jobs or not title_matches(title, spec):
                        continue
                    geo = it.get("jobGeo") or ""
                    if not remote_ok_for_india(geo):
                        continue
                    posted = it.get("pubDate")
                    if too_old(posted, spec.max_age_days):
                        continue
                    desc = html_to_text(it.get("jobDescription"))
                    sal = ""
                    if it.get("salaryMin"):
                        sal = f"{it.get('salaryCurrency', '')} {it.get('salaryMin')}-{it.get('salaryMax', '')} {it.get('salaryPeriod', '')}".strip()
                    jobs[jid] = Job(
                        source=self.name, external_id=jid, company=it.get("companyName") or "", title=title,
                        url=it.get("url") or "", location=geo or "Anywhere", remote=True, description=desc,
                        posted_at=posted, salary=sal, visa=visa_note(desc),
                        extra={"level": it.get("jobLevel"), "industry": it.get("jobIndustry")},
                    )
                except Exception as e:
                    log.warning("jobicy: row failed: %s", e)
            await asyncio.sleep(0.5)
        return list(jobs.values())


# --------------------------------------------------------------------------------------------------
# Working Nomads
# --------------------------------------------------------------------------------------------------

class WorkingNomadsSource(Source):
    name = "workingnomads"

    async def fetch(self, client: httpx.AsyncClient, spec: SearchSpec) -> list[Job]:
        jobs: list[Job] = []
        try:
            data = await get_json(client, "https://www.workingnomads.com/api/exposed_jobs/")
        except Exception as e:
            log.warning("workingnomads: %s", e)
            return jobs
        for it in data or []:
            try:
                title = it.get("title") or ""
                if not title_matches(title, spec):
                    continue
                loc = it.get("location") or ""
                if not remote_ok_for_india(loc):
                    continue
                posted = it.get("pub_date")
                if too_old(posted, spec.max_age_days):
                    continue
                url = it.get("url") or ""
                desc = html_to_text(it.get("description"))
                jobs.append(Job(
                    source=self.name, external_id=url.rstrip("/").rsplit("/", 1)[-1], company=it.get("company_name") or "",
                    title=title, url=url, location=loc or "Anywhere", remote=True, description=desc,
                    posted_at=posted, visa=visa_note(desc), extra={"category": it.get("category_name"), "tags": it.get("tags")},
                ))
            except Exception as e:
                log.warning("workingnomads: row failed: %s", e)
        return jobs


# --------------------------------------------------------------------------------------------------
# Hirist (India tech jobs) — public JSON backend used by hirist.tech's own frontend
# --------------------------------------------------------------------------------------------------

def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9\s-]", "", s.lower())
    return re.sub(r"[\s-]+", "-", s).strip("-")


class HiristSource(Source):
    """hirist.tech listing by tag id: 203=Machine Learning, 898=Artificial Intelligence, 131480=LLM.

    Listing is public; the per-job detail endpoint (description) is public too. Applying needs a hirist
    login, hence ats="hirist" unless the posting carries an external applyUrl.
    """

    name = "hirist"
    API = "https://gladiator.hirist.tech"

    def __init__(self, keyword_ids: tuple[int, ...] = (203, 898, 131480), max_pages: int = 20,
                 fetch_details: bool = True, max_details: int = 1_000_000):
        self.keyword_ids = keyword_ids
        self.max_pages = max_pages
        self.fetch_details = fetch_details
        self.max_details = max_details

    async def fetch(self, client: httpx.AsyncClient, spec: SearchSpec) -> list[Job]:
        jobs: dict[str, Job] = {}
        headers = {"Origin": "https://www.hirist.tech", "Referer": "https://www.hirist.tech/"}
        for kid in self.keyword_ids:
            for page in range(self.max_pages):
                url = f"{self.API}/job/keyword/?query={kid}&page={page}&industry=&keywordId={kid}&size=20"
                try:
                    data = await get_json(client, url, headers=headers)
                except Exception as e:
                    log.warning("hirist: %s: %s", url, e)
                    break
                for it in data.get("data") or []:
                    try:
                        self._add(it, spec, jobs)
                    except Exception as e:
                        log.warning("hirist: row failed: %s", e)
                if not data.get("hasMore"):
                    break
                await asyncio.sleep(0.4)
        if self.fetch_details:
            sem = asyncio.Semaphore(4)

            async def detail(j: Job) -> None:
                async with sem:
                    try:
                        d = (await get_json(client, f"{self.API}/job/detail?jobcode={j.external_id}", headers=headers)).get("data") or {}
                        j.description = html_to_text(d.get("introText") or d.get("jobJdContent") or "") or j.description
                        j.visa = visa_note(j.description)
                        if d.get("jobDetailUrl"):
                            j.url = d["jobDetailUrl"]
                        if d.get("applyUrl"):
                            j.apply_url = d["applyUrl"]
                            j.ats = ats_from_url(d["applyUrl"])
                        else:
                            j.apply_url = j.url
                    except Exception as e:
                        log.warning("hirist: detail %s failed: %s", j.external_id, e)

            await asyncio.gather(*(detail(j) for j in list(jobs.values())[: self.max_details]))
        return list(jobs.values())

    def _add(self, it: dict, spec: SearchSpec, jobs: dict[str, Job]) -> None:
        jid = str(it.get("id"))
        full_title = (it.get("title") or "").strip()
        company = ((it.get("companyData") or {}).get("companyName") or "").strip()
        title = full_title
        if company and full_title.lower().startswith(company.lower() + " - "):
            title = full_title[len(company) + 3:].strip()
        elif " - " in full_title and (not company or "hirist" in company.lower()):
            company, title = (x.strip() for x in full_title.split(" - ", 1))
        if jid in jobs or not title_matches(title, spec):
            return
        posted = ts_to_iso(it.get("createdTimeMs") or it.get("createdTime"))
        if too_old(posted, spec.max_age_days):
            return
        locs = [l["name"] for l in (it.get("locations") or it.get("location") or []) if isinstance(l, dict) and l.get("name")]
        wfh = bool(it.get("workFromHome"))
        url = f"https://www.hirist.tech/j/{_slug(full_title)}-{jid}"
        tags = [t["name"] for t in it.get("tags") or [] if isinstance(t, dict) and t.get("name")]
        sal = ""
        if not it.get("hideSal") and it.get("maxSal"):
            sal = f"INR {it.get('minSal')}-{it.get('maxSal')} LPA"
        jobs[jid] = Job(
            source=self.name, external_id=jid, company=company, title=title, url=url,
            location=", ".join(locs) + (" (Remote)" if wfh else ""), remote=True if wfh else None,
            description="Skills: " + ", ".join(tags), ats="hirist", posted_at=posted, salary=sal,
            extra={"exp_min": it.get("min"), "exp_max": it.get("max"), "tags": tags},
        )


SOURCES: list[Source] = [
    HNWhoIsHiringSource(),
    RemoteOKSource(),
    RemotiveSource(),
    WeWorkRemotelySource(),
    HimalayasSource(),
    ArbeitnowSource(),
    # JobicySource() works but returned 0 India-eligible AI/ML rows in testing (its AI roles are
    # US/EU/LATAM-restricted); add it back here if that changes.
    WorkingNomadsSource(),
    HiristSource(),
]
