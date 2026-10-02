"""Y Combinator "Work at a Startup" jobs, read from www.ycombinator.com (no login).

Two public data paths:

1. Listing pages (/jobs, /jobs/role/<role>[/<location>], /jobs/location/<location>) are Inertia.js pages:
   the HTML has `data-page="{...html-escaped JSON...}"` whose props.jobPostings is the job list. These pages
   only show a sample (~20-50 jobs) so they're cheap but incomplete.
2. The company directory (www.ycombinator.com/companies) queries Algolia with a public search-only key
   (window.AlgoliaOpts in the page). We use it to enumerate companies that are hiring (isHiring) and are
   AI-ish or India/remote-friendly, then read each company's /companies/<slug>/jobs page (same Inertia
   JSON, *all* of that company's live postings). Matching jobs get their description from the job page.

Applying goes through workatastartup.com behind a YC account login -> ats="workatastartup".
"""
from __future__ import annotations

import asyncio
import html as htmllib
import json
import logging
import re
from urllib.parse import quote

import httpx

from jobagent.models import Job
from jobagent.sources.ats import DESC_LIMIT, TIMEOUT, guess_remote
from jobagent.sources.base import UA, SearchSpec, Source, title_matches

log = logging.getLogger(__name__)

YC = "https://www.ycombinator.com"

LISTING_PATHS = [
    "/jobs",
    "/jobs/role/software-engineer",
    "/jobs/role/software-engineer/remote",
    "/jobs/role/software-engineer/india",
    "/jobs/role/software-engineer/bengaluru",
    "/jobs/role/software-engineer/san-francisco",
    "/jobs/role/software-engineer/new-york",
    "/jobs/role/science",
    "/jobs/role/science/remote",
    "/jobs/location/india",
    "/jobs/location/remote",
]

AI_FACETS = ["industries:Artificial Intelligence", "tags:Artificial Intelligence", "tags:AI", "tags:Generative AI",
             "tags:Machine Learning", "tags:AI Assistant", "tags:Deep Learning", "tags:Computer Vision",
             "tags:NLP", "tags:LLM", "tags:AIOps", "tags:Conversational AI"]
INDIA_REMOTE_FACETS = ["regions:India", "regions:South Asia", "regions:Remote", "regions:Fully Remote",
                       "regions:Partly Remote"]


def _inertia_props(text: str) -> dict | None:
    m = re.search(r'data-page="([^"]*)"', text)
    if not m:
        return None
    try:
        return json.loads(htmllib.unescape(m.group(1))).get("props") or {}
    except ValueError:
        return None


def _md_to_text(md: str | None) -> str:
    if not md:
        return ""
    t = re.sub(r"^#{1,6}\s*", "", md, flags=re.M)
    t = re.sub(r"\*\*|__|`", "", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


class YCSource(Source):
    name = "yc"

    def __init__(self, *, max_companies: int = 100_000, fetch_descriptions: bool = True, max_descriptions: int = 1_000_000,
                 concurrency: int = 8, extra_paths: list[str] | None = None):
        self.max_companies = max_companies
        self.fetch_descriptions = fetch_descriptions
        self.max_descriptions = max_descriptions
        self.concurrency = concurrency
        self.paths = LISTING_PATHS + list(extra_paths or [])

    async def _get(self, client: httpx.AsyncClient, sem: asyncio.Semaphore, path: str) -> httpx.Response | None:
        async with sem:
            for attempt in range(3):
                try:
                    r = await client.get(YC + path if path.startswith("/") else path, timeout=TIMEOUT,
                                         headers={"User-Agent": UA, "Accept": "text/html"}, follow_redirects=True)
                    if r.status_code == 404:
                        return None
                    if r.status_code in (429, 500, 502, 503) and attempt < 2:
                        await asyncio.sleep(2 * (attempt + 1))
                        continue
                    r.raise_for_status()
                    return r
                except httpx.TransportError:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(1 + attempt)
        return None

    # ------------------------------------------------------------------ algolia company directory
    async def _hiring_companies(self, client: httpx.AsyncClient, sem: asyncio.Semaphore) -> list[str]:
        r = await self._get(client, sem, "/companies")
        if r is None:
            return []
        m = re.search(r"AlgoliaOpts\s*=\s*(\{.*?\})", r.text)
        if not m:
            log.warning("yc: AlgoliaOpts not found on /companies")
            return []
        opts = json.loads(m.group(1))
        url = f"https://{opts['app'].lower()}-dsn.algolia.net/1/indexes/*/queries"
        headers = {"x-algolia-application-id": opts["app"], "x-algolia-api-key": opts["key"], "User-Agent": UA}

        async def query(facet_filters: list) -> list[dict]:
            params = f"hitsPerPage=1000&query=&attributesToRetrieve=%5B%22slug%22%2C%22regions%22%2C%22team_size%22%5D&facetFilters={quote(json.dumps(facet_filters))}"
            body = {"requests": [{"indexName": "YCCompany_production", "params": params}]}
            async with sem:
                resp = await client.post(url, json=body, headers=headers, timeout=TIMEOUT)
            resp.raise_for_status()
            return resp.json()["results"][0].get("hits", [])

        india_remote, ai, ai_india_remote = await asyncio.gather(
            query([["isHiring:true"], INDIA_REMOTE_FACETS]),
            query([["isHiring:true"], AI_FACETS]),
            query([["isHiring:true"], AI_FACETS, INDIA_REMOTE_FACETS]),
        )
        # priority: AI & (India|remote)  >  India region  >  other AI  >  other remote
        india = [h for h in india_remote if {"India", "South Asia"} & set(h.get("regions") or [])]
        ordered = ai_india_remote + india + ai + india_remote
        slugs = list(dict.fromkeys(h["slug"] for h in ordered if h.get("slug")))
        return slugs[: self.max_companies]

    # ------------------------------------------------------------------ main
    def _to_job(self, p: dict, company: dict | None = None) -> Job:
        loc = p.get("location") or ""
        company = company or {}
        return Job(
            source=self.name,
            external_id=str(p.get("id")),
            company=(p.get("companyName") or company.get("name") or "").strip(),
            title=(p.get("title") or "").strip(),
            url=YC + p["url"] if (p.get("url") or "").startswith("/") else (p.get("url") or ""),
            apply_url=p.get("applyUrl") or p.get("ctaUrl") or "",
            location=loc,
            remote=guess_remote(loc),
            description=_md_to_text(p.get("description"))[:DESC_LIMIT],
            ats="workatastartup",
            salary=" / ".join(x for x in (p.get("salaryRange"), p.get("equityRange")) if x),
            visa=p.get("visa") or "",
            extra={"batch": p.get("companyBatchName") or company.get("batch_name"),
                   "company_one_liner": p.get("companyOneLiner") or company.get("one_liner"),
                   "company_url": YC + p["companyUrl"] if (p.get("companyUrl") or "").startswith("/") else p.get("companyUrl"),
                   "company_website": company.get("website"), "type": p.get("type"),
                   "role": p.get("prettyRole"), "role_type": p.get("roleSpecificType"),
                   "min_experience": p.get("minExperience"), "skills": p.get("skills"),
                   "created_ago": p.get("createdAt"), "last_active": p.get("lastActive")},
        )

    async def fetch(self, client: httpx.AsyncClient, spec: SearchSpec) -> list[Job]:
        sem = asyncio.Semaphore(self.concurrency)
        found: dict[str, Job] = {}

        def add(postings: list[dict], company: dict | None = None) -> None:
            for p in postings or []:
                try:
                    if p.get("id") is None or not title_matches(p.get("title") or "", spec):
                        continue
                    if p.get("type") and p["type"].lower() not in ("full-time", "contract", "fulltime"):
                        continue  # drop internships / part-time
                    j = self._to_job(p, company)
                    if j.external_id not in found:
                        found[j.external_id] = j
                except Exception as e:
                    log.warning("yc: bad posting %s: %s", p.get("id"), e)

        # 1. listing pages
        async def listing(path: str) -> None:
            try:
                r = await self._get(client, sem, path)
                props = _inertia_props(r.text) if r is not None else None
                if props:
                    add(props.get("jobPostings") or [])
            except Exception as e:
                log.warning("yc: listing %s failed: %s", path, e)

        await asyncio.gather(*(listing(p) for p in self.paths))

        # 2. hiring companies from the directory -> each company's full job list
        try:
            slugs = await self._hiring_companies(client, sem)
        except Exception as e:
            log.warning("yc: company directory query failed: %s", e)
            slugs = []

        async def company(slug: str) -> None:
            try:
                r = await self._get(client, sem, f"/companies/{slug}/jobs")
                props = _inertia_props(r.text) if r is not None else None
                if props:
                    add(props.get("jobPostings") or [], props.get("company") or {})
            except Exception as e:
                log.warning("yc: company %s failed: %s", slug, e)

        await asyncio.gather(*(company(s) for s in slugs))

        # 3. descriptions (+ authoritative visa/salary) from each matching job page
        if self.fetch_descriptions:
            todo = [j for j in found.values() if not j.description][: self.max_descriptions]

            async def detail(j: Job) -> None:
                try:
                    r = await self._get(client, sem, j.url)
                    props = _inertia_props(r.text) if r is not None else None
                    p = (props or {}).get("job") or {}
                    if p:
                        j.description = _md_to_text(p.get("description"))[:DESC_LIMIT]
                        j.visa = p.get("visa") or j.visa
                        c = (props or {}).get("company") or {}
                        j.extra["company_website"] = c.get("website") or j.extra.get("company_website")
                        j.extra["team_size"] = c.get("team_size")
                except Exception as e:
                    log.warning("yc: job page %s failed: %s", j.url, e)

            await asyncio.gather(*(detail(j) for j in todo))

        return list(found.values())
