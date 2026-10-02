"""Instahyre discovery via its public JSON API (no login).

  search : GET https://www.instahyre.com/api/v1/job_search?skills=<text>&limit=35&offset=N
           -> {"meta": {total_count, next, ...}, "objects": [{id, title, locations, keywords, public_url,
               employer{company_name, ...}}, ...]}
           (a `locations=` filter is silently ignored; every Instahyre job is India-based or
            "Work From Home", so no geo filter is needed)
  detail : GET https://www.instahyre.com/api/v1/employer_public_jobs/{id}
           -> full HTML description, experience range, locations list, is_active.

The human-facing job pages (public_url) sit behind a Cloudflare challenge for plain HTTP clients, but
both API endpoints above answer plain httpx. Applying requires an Instahyre candidate login
(ats="instahyre").
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlencode

import httpx

from jobagent.models import Job
from jobagent.sources.aggregators import get_json, html_to_text, visa_note
from jobagent.sources.base import SearchSpec, Source, title_matches

log = logging.getLogger(__name__)

API = "https://www.instahyre.com/api/v1"
PAGE = 35


async def _get(client: httpx.AsyncClient, url: str, headers: dict, retries: int = 3):
    """GET JSON, backing off on 429 (Instahyre rate-limits bursts)."""
    for attempt in range(retries + 1):
        try:
            return await get_json(client, url, headers=headers)
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 429 or attempt == retries:
                raise
            await asyncio.sleep(5 * (attempt + 1))


class InstahyreSource(Source):
    name = "instahyre"

    def __init__(
        self,
        queries: list[str] | None = None,  # Instahyre "skills" search terms
        max_pages: int = 25,
        fetch_details: bool = True,
        max_details: int = 1_000_000,
        concurrency: int = 3,
    ):
        self.queries = queries or ["AI Engineer", "Machine Learning", "LLM", "Generative AI",
                                   "Artificial Intelligence", "Applied Scientist", "MLOps"]
        self.max_pages = max_pages
        self.fetch_details = fetch_details
        self.max_details = max_details
        self.concurrency = concurrency

    async def fetch(self, client: httpx.AsyncClient, spec: SearchSpec) -> list[Job]:
        jobs: dict[str, Job] = {}
        headers = {"Referer": "https://www.instahyre.com/search-jobs/"}
        for q in self.queries:
            for page in range(self.max_pages):
                url = f"{API}/job_search?" + urlencode({"skills": q, "limit": PAGE, "offset": page * PAGE})
                try:
                    data = await _get(client, url, headers)
                except Exception as e:
                    log.warning("instahyre: %s: %s", url, e)
                    break
                rows = data.get("objects") or []
                for it in rows:
                    try:
                        self._add(it, spec, jobs)
                    except Exception as e:
                        log.warning("instahyre: row failed: %s", e)
                if len(rows) < PAGE or not (data.get("meta") or {}).get("next"):
                    break
                await asyncio.sleep(1.0)
        if self.fetch_details and jobs:
            sem = asyncio.Semaphore(self.concurrency)

            async def one(j: Job) -> None:
                async with sem:
                    try:
                        d = await _get(client, f"{API}/employer_public_jobs/{j.external_id}", headers)
                    except Exception as e:
                        log.warning("instahyre: detail %s failed: %s", j.external_id, e)
                        return
                    if d.get("is_active") is False:
                        j.extra["inactive"] = True
                    desc = html_to_text(d.get("description"))
                    if desc:
                        kws = ", ".join(d.get("keywords") or [])
                        j.description = (desc + (f"\n\nSkills: {kws}" if kws else ""))[:6000]
                        j.visa = visa_note(desc)
                    if d.get("workex_min") is not None:
                        j.extra["exp"] = f"{d.get('workex_min')}-{d.get('workex_max')} yrs"
                    if d.get("job_category"):
                        j.extra["category"] = d["job_category"]

            await asyncio.gather(*(one(j) for j in list(jobs.values())[: self.max_details]))
            for k in [k for k, j in jobs.items() if j.extra.get("inactive")]:
                jobs.pop(k)
        return list(jobs.values())

    def _add(self, it: dict, spec: SearchSpec, jobs: dict[str, Job]) -> None:
        jid = str(it.get("id"))
        title = (it.get("title") or it.get("candidate_title") or "").strip()
        if not jid or jid in jobs or not title_matches(title, spec):
            return
        emp = it.get("employer") or {}
        locs = it.get("locations") or ""
        if isinstance(locs, list):
            locs = ",".join(locs)
        loc_list = [x.strip() for x in locs.split(",") if x.strip()]
        wfh = any("work from home" in x.lower() for x in loc_list)
        url = it.get("public_url") or f"https://www.instahyre.com/job-{jid}/"
        kws = ", ".join(it.get("keywords") or [])
        jobs[jid] = Job(
            source=self.name, external_id=jid, company=(emp.get("company_name") or "").strip(), title=title,
            url=url, location=", ".join(loc_list), remote=True if wfh else None,
            description=(f"Skills: {kws}\n\n" if kws else "") + (emp.get("instahyre_note") or ""),
            ats="instahyre",
            extra={"company_tagline": emp.get("company_tagline"), "employee_count": emp.get("employee_count"),
                   "keywords": it.get("keywords")},
        )


SOURCES: list[Source] = [InstahyreSource()]
