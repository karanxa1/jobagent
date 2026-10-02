"""Naukri.com discovery (no login).

What works (verified Oct 2026):
  - Direct httpx calls to https://www.naukri.com/jobapi/v3/search with the usual headers
    (appid: 109, systemid: Naukri, clientid: d3skt0p) are rejected with
    406 {"message":"recaptcha required"} -- the site's JS adds a per-request `nkparam` token.
  - The Playwright *headless-shell* build gets HTTP 403 from Akamai.
  - Full Chromium in new-headless mode (`channel="chromium"`) loads the SEO search pages fine, and the
    page's own XHR to /jobapi/v3/search returns clean JSON (20 jobs/page, with `applyRedirectUrl` when
    the job applies on the company site).

So this source drives a headless Chromium through SEO search URLs
(`/{keyword}-jobs-in-{location}[-{page}]`, remote = `/{keyword}-jobs?wfhType=2`) and captures the
JSON responses -- no DOM scraping, no login. Applying on Naukri itself needs a logged-in account, so
those jobs get ats="naukri"; jobs that redirect out get apply_url=<company URL> and ats from its host.
"""
from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urlencode

import httpx

from jobagent.models import Job
from jobagent.sources.aggregators import ats_from_url, html_to_text, too_old, ts_to_iso, visa_note
from jobagent.sources.base import SearchSpec, Source, title_matches

log = logging.getLogger(__name__)

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


class NaukriSource(Source):
    name = "naukri"
    needs_login = False  # discovery only; applying on-platform needs login

    def __init__(
        self,
        locations: list[str] | None = None,   # naukri slugs; "remote" -> wfhType=2 (work from home)
        keywords: list[str] | None = None,    # None -> spec.keywords
        max_pages: int = 10,                  # 20 jobs per page
        job_age_days: int | None = 30,        # Naukri freshness filter (1/3/7/15/30); None -> no filter
        headless: bool = True,
        user_data_dir: str | None = None,     # optional persistent profile (not required)
        page_timeout_ms: int = 45000,
        max_details: int = 1_000_000,               # open N job pages to capture the full JD (/jobapi/v4/job/{id})
    ):
        self.locations = locations or ["india", "remote"]
        self.keywords = keywords
        self.max_pages = max_pages
        self.job_age_days = job_age_days
        self.headless = headless
        self.user_data_dir = user_data_dir
        self.page_timeout_ms = page_timeout_ms
        self.max_details = max_details

    def _urls(self, kw: str, loc: str, page: int) -> str:
        base = f"{_slug(kw)}-jobs"
        params: dict[str, str] = {}
        if loc == "remote":
            params["wfhType"] = "2"
        else:
            base += f"-in-{_slug(loc)}"
        if page > 1:
            base += f"-{page}"
        if self.job_age_days:
            params["jobAge"] = str(self.job_age_days)
        return f"https://www.naukri.com/{base}" + (f"?{urlencode(params)}" if params else "")

    async def fetch(self, client: httpx.AsyncClient, spec: SearchSpec) -> list[Job]:
        jobs: dict[str, Job] = {}
        try:
            from playwright.async_api import async_playwright
        except Exception as e:
            log.warning("naukri: playwright unavailable: %s", e)
            return []
        try:
            async with async_playwright() as p:
                ctx, browser = await self._open(p)
                try:
                    page = await ctx.new_page()
                    await page.route(re.compile(r"\.(png|jpe?g|gif|webp|svg|woff2?|ttf|mp4)(\?|$)"),
                                     lambda r: r.abort())
                    for kw in self.keywords or spec.keywords:
                        for loc in self.locations:
                            for n in range(1, self.max_pages + 1):
                                data = await self._search(page, self._urls(kw, loc, n))
                                if data is None:
                                    break
                                rows = data.get("jobDetails") or []
                                for it in rows:
                                    try:
                                        j = self._to_job(it, spec, remote_search=(loc == "remote"))
                                        if j and j.external_id not in jobs:
                                            jobs[j.external_id] = j
                                    except Exception as e:
                                        log.warning("naukri: row failed: %s", e)
                                total = int(data.get("noOfJobs") or 0)
                                if len(rows) < 20 or n * 20 >= total:
                                    break
                    for j in list(jobs.values())[: self.max_details]:
                        await self._enrich(page, j)
                finally:
                    await ctx.close()
                    if browser:
                        await browser.close()
        except Exception as e:
            log.warning("naukri: browser session failed: %s", e)
        return list(jobs.values())

    async def _open(self, p):
        args = ["--disable-blink-features=AutomationControlled"]
        kw = dict(user_agent=BROWSER_UA, viewport={"width": 1366, "height": 900}, locale="en-IN")
        if self.user_data_dir:
            ctx = await p.chromium.launch_persistent_context(self.user_data_dir, headless=self.headless,
                                                             channel="chromium", args=args, **kw)
            return ctx, None
        try:  # full chromium (new headless); the headless-shell build is 403'd by Akamai
            browser = await p.chromium.launch(headless=self.headless, channel="chromium", args=args)
        except Exception as e:
            log.warning("naukri: channel=chromium unavailable (%s); falling back to default build", e)
            browser = await p.chromium.launch(headless=self.headless, args=args)
        return await browser.new_context(**kw), browser

    async def _search(self, page, url: str) -> dict | None:
        for attempt in range(2):
            try:
                async with page.expect_response(lambda r: "/jobapi/v3/search" in r.url,
                                                timeout=self.page_timeout_ms) as info:
                    resp = await page.goto(url, wait_until="domcontentloaded", timeout=self.page_timeout_ms)
                if resp is not None and resp.status >= 400:
                    log.warning("naukri: %s -> HTTP %s", url, resp.status)
                r = await info.value
                if r.status != 200:
                    log.warning("naukri: search API HTTP %s for %s", r.status, url)
                    return None
                data = await r.json()
                await asyncio.sleep(1.0)
                return data
            except Exception as e:
                log.warning("naukri: %s attempt %d failed: %s", url, attempt + 1, e)
                await asyncio.sleep(3)
        return None

    async def _enrich(self, page, j: Job) -> None:
        """Open the job page and capture its detail JSON for the full description."""
        try:
            async with page.expect_response(lambda r: f"/jobapi/v4/job/{j.external_id}" in r.url,
                                            timeout=self.page_timeout_ms) as info:
                await page.goto(j.url, wait_until="domcontentloaded", timeout=self.page_timeout_ms)
            r = await info.value
            if r.status != 200:
                return
            d = (await r.json()).get("jobDetails") or {}
            desc = html_to_text(d.get("description"))
            if desc:
                skills = j.extra.get("skills")
                j.description = (desc + (f"\n\nSkills: {skills}" if skills else ""))[:6000]
                j.visa = visa_note(desc)
            if d.get("wfhLabel"):
                j.extra["wfh"] = d["wfhLabel"]
            if d.get("employmentType"):
                j.extra["employment_type"] = d["employmentType"]
            await asyncio.sleep(0.8)
        except Exception as e:
            log.warning("naukri: detail %s failed: %s", j.external_id, e)

    def _to_job(self, it: dict, spec: SearchSpec, remote_search: bool) -> Job | None:
        title = (it.get("title") or "").strip()
        if not title or not title_matches(title, spec):
            return None
        posted = ts_to_iso(it.get("createdDate"))
        if too_old(posted, spec.max_age_days):
            return None
        ph = {x.get("type"): x.get("label") for x in it.get("placeholders") or [] if isinstance(x, dict)}
        location = ph.get("location") or ""
        url = "https://www.naukri.com" + it["jdURL"] if (it.get("jdURL") or "").startswith("/") else (it.get("jdURL") or "")
        ext = it.get("applyRedirectUrl") or ""
        desc = html_to_text(it.get("jobDescription"))
        if it.get("tagsAndSkills"):
            desc = (desc + "\n\nSkills: " + it["tagsAndSkills"]).strip()
        sal = ph.get("salary") or ""
        if sal.lower().startswith("not disclosed"):
            sal = ""
        remote = True if (remote_search or re.search(r"\bremote\b|work from home", location, re.I)) else None
        if remote is None and re.search(r"\bhybrid\b", location, re.I):
            remote = False
        return Job(
            source=self.name, external_id=str(it.get("jobId")), company=(it.get("companyName") or "").strip(),
            title=title, url=url, apply_url=ext or url, location=location, remote=remote, description=desc,
            ats=ats_from_url(ext) if ext else "naukri", posted_at=posted, salary=sal, visa=visa_note(desc),
            extra={"experience": it.get("experienceText") or ph.get("experience"), "mode": it.get("mode"),
                   "company_apply": bool(it.get("companyApplyJob")), "skills": it.get("tagsAndSkills")},
        )


SOURCES: list[Source] = [NaukriSource()]
