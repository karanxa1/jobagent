"""Wellfound (formerly AngelList Talent) discovery from its public SEO role pages.

  /role/l/{role}/{location}   e.g. /role/l/machine-learning-engineer/india
  /role/r/{role}              remote roles, e.g. /role/r/ai-engineer
  ?page=N                     20 startups per page (each with up to ~3 highlighted job listings)

The pages are server-rendered Next.js; job data sits in the Apollo cache inside __NEXT_DATA__
(`props.pageProps.apolloState.data` with `JobListingSearchResult:*` and `StartupResult:*` entries).

Fetch strategy (in order):
  1. plain httpx with a browser UA  -- works as of Oct 2026 (Cloudflare Turnstile is only armed for
     XHR/fetch calls, not for the SSR HTML)
  2. headless Chromium (Playwright, full chromium build) with a realistic UA, if httpx gets a
     challenge/403 or no __NEXT_DATA__
  3. if `user_data_dir` is given, Playwright uses that *persistent* Chrome profile instead of a fresh
     one -- point it at a profile where you have logged into wellfound.com once (and passed the
     Cloudflare check) if anonymous access ever gets fully blocked. Example:
         WellfoundSource(user_data_dir="~/.jobagent/profiles/wellfound")
     Create it once by running, then logging in by hand in the window that opens:
         uv run python -c "import asyncio; from playwright.async_api import async_playwright as P
         async def m():
             async with P() as p:
                 c = await p.chromium.launch_persistent_context('<dir>', headless=False, channel='chromium')
                 await (await c.new_page()).goto('https://wellfound.com/login'); await asyncio.sleep(300)
         asyncio.run(m())"
If everything is blocked, fetch() logs a warning and returns [].

Applying on Wellfound requires a candidate login, so jobs carry ats="wellfound".
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re

import httpx

from jobagent.models import Job
from jobagent.sources.aggregators import html_to_text, too_old, ts_to_iso, visa_note
from jobagent.sources.base import UA, SearchSpec, Source, title_matches

log = logging.getLogger(__name__)

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
_NEXT_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


class WellfoundSource(Source):
    name = "wellfound"
    needs_login = False

    def __init__(
        self,
        roles: list[str] | None = None,
        locations: list[str] | None = None,   # wellfound location slugs; "remote" -> /role/r/{role}
        max_pages: int = 20,
        user_data_dir: str | None = None,
        headless: bool = True,
        min_interval: float = 1.5,
    ):
        self.roles = roles or ["ai-engineer", "machine-learning-engineer", "artificial-intelligence-engineer",
                               "data-scientist"]
        self.locations = locations or ["india", "remote"]
        self.max_pages = max_pages
        self.user_data_dir = os.path.expanduser(user_data_dir) if user_data_dir else None
        self.headless = headless
        self.min_interval = min_interval
        self._pw = None
        self._ctx = None
        self._browser = None
        self.blocked = False

    def _url(self, role: str, loc: str, page: int) -> str:
        path = f"/role/r/{role}" if loc == "remote" else f"/role/l/{role}/{loc}"
        return f"https://wellfound.com{path}" + (f"?page={page}" if page > 1 else "")

    # ------------------------------------------------------------------ page loading
    async def _get_httpx(self, client: httpx.AsyncClient, url: str) -> str | None:
        try:
            r = await client.get(url, headers={
                "User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9"}, timeout=30, follow_redirects=False)
        except httpx.HTTPError as e:
            log.warning("wellfound: httpx %s: %s", url, e)
            return None
        if r.status_code in (301, 302, 303, 307, 308):
            return ""  # role/location combo doesn't exist -> wellfound redirects to a generic page
        if r.status_code == 200 and "__NEXT_DATA__" in r.text:
            return r.text
        log.info("wellfound: httpx blocked/empty for %s (HTTP %s)", url, r.status_code)
        return None

    async def _get_browser(self, url: str) -> str | None:
        try:
            if self._ctx is None:
                from playwright.async_api import async_playwright
                self._pw = await async_playwright().start()
                args = ["--disable-blink-features=AutomationControlled"]
                kw = dict(user_agent=BROWSER_UA, viewport={"width": 1366, "height": 900}, locale="en-US")
                if self.user_data_dir:
                    os.makedirs(self.user_data_dir, exist_ok=True)
                    self._ctx = await self._pw.chromium.launch_persistent_context(
                        self.user_data_dir, headless=self.headless, channel="chromium", args=args, **kw)
                else:
                    try:
                        self._browser = await self._pw.chromium.launch(headless=self.headless, channel="chromium", args=args)
                    except Exception:
                        self._browser = await self._pw.chromium.launch(headless=self.headless, args=args)
                    self._ctx = await self._browser.new_context(**kw)
            page = await self._ctx.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                for _ in range(10):  # give a Cloudflare interstitial a chance to clear
                    html = await page.content()
                    if "__NEXT_DATA__" in html:
                        return html
                    await page.wait_for_timeout(1500)
                return None
            finally:
                await page.close()
        except Exception as e:
            log.warning("wellfound: browser fetch %s failed: %s", url, e)
            return None

    async def _close_browser(self) -> None:
        for obj in (self._ctx, self._browser):
            try:
                if obj:
                    await obj.close()
            except Exception:
                pass
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._pw = self._ctx = self._browser = None

    # ------------------------------------------------------------------ fetch
    async def fetch(self, client: httpx.AsyncClient, spec: SearchSpec) -> list[Job]:
        jobs: dict[str, Job] = {}
        use_browser = bool(self.user_data_dir)
        try:
            for role in self.roles:
                for loc in self.locations:
                    for page in range(1, self.max_pages + 1):
                        url = self._url(role, loc, page)
                        html = None if use_browser else await self._get_httpx(client, url)
                        if html is None:
                            use_browser = True
                            html = await self._get_browser(url)
                        if not html:
                            if html is None:
                                log.warning("wellfound: could not load %s", url)
                            break
                        page_count = self._parse(html, spec, jobs, remote_page=(loc == "remote"))
                        await asyncio.sleep(self.min_interval)
                        if page >= page_count:
                            break
            if not jobs and use_browser:
                self.blocked = True
                log.warning("wellfound: blocked (no data via httpx or browser). Pass user_data_dir=<logged-in "
                            "Chrome profile> to WellfoundSource to use a session that has passed Cloudflare.")
        except Exception as e:
            log.warning("wellfound: fetch aborted: %s", e)
        finally:
            await self._close_browser()
        return list(jobs.values())

    def _parse(self, html: str, spec: SearchSpec, jobs: dict[str, Job], remote_page: bool) -> int:
        """Add jobs from one page into `jobs`; returns the page count reported by the page."""
        m = _NEXT_RE.search(html)
        if not m:
            return 0
        try:
            data = json.loads(m.group(1))
            ap = data["props"]["pageProps"]["apolloState"]["data"]
        except Exception as e:
            log.warning("wellfound: bad __NEXT_DATA__: %s", e)
            return 0
        page_count = 1
        for k, v in (ap.get("ROOT_QUERY", {}).get("talent") or {}).items():
            if k.startswith("seoLandingPageJobSearchResults") and isinstance(v, dict):
                page_count = int(v.get("pageCount") or 1)
        for key, st in ap.items():
            if not key.startswith("StartupResult:"):
                continue
            company = st.get("name") or ""
            for ref in st.get("highlightedJobListings") or []:
                jl = ap.get(ref.get("__ref", "")) if isinstance(ref, dict) else None
                if not jl:
                    continue
                try:
                    self._add(jl, st, company, spec, jobs, remote_page)
                except Exception as e:
                    log.warning("wellfound: listing parse failed: %s", e)
        return page_count

    def _add(self, jl: dict, st: dict, company: str, spec: SearchSpec, jobs: dict[str, Job], remote_page: bool) -> None:
        jid = str(jl.get("id"))
        title = jl.get("title") or ""
        if jid in jobs or not title_matches(title, spec):
            return
        posted = ts_to_iso(jl.get("liveStartAt"))
        if too_old(posted, spec.max_age_days):
            return
        rc = jl.get("remoteConfig") or {}
        kind = (rc.get("kind") or "").upper()
        remote = bool(jl.get("remote")) or "REMOTE" in kind  # REMOTE, ONSITE_OR_REMOTE, ...
        locs = list(jl.get("locationNames") or [])
        accepted = list(jl.get("acceptedRemoteLocationNames") or [])
        loc_txt = ", ".join(locs)
        if remote:
            loc_txt = (loc_txt + "; " if loc_txt else "") + "Remote" + (f" ({', '.join(accepted)})" if accepted else "")
        elif kind == "HYBRID" or rc.get("wfhFlexible"):
            loc_txt += " (hybrid/WFH-flexible)" if loc_txt else "Hybrid"
        desc = html_to_text(jl.get("description"))
        slug = jl.get("slug") or ""
        url = f"https://wellfound.com/jobs/{jid}" + (f"-{slug}" if slug else "")
        jobs[jid] = Job(
            source=self.name, external_id=jid, company=company, title=title, url=url,
            location=loc_txt, remote=True if remote else (False if kind == "ONSITE" else None),
            description=desc, ats="wellfound", posted_at=posted, salary=jl.get("compensation") or "",
            visa=visa_note(desc),
            extra={"company_slug": st.get("slug"), "company_url": f"https://wellfound.com/company/{st.get('slug')}",
                   "high_concept": st.get("highConcept"), "company_size": st.get("companySize"),
                   "job_type": jl.get("jobType"), "remote_kind": kind, "accepted_remote": accepted,
                   "years_min": jl.get("yearsExperienceMin"), "from_remote_page": remote_page,
                   "ats_source": jl.get("atsSource")},
        )


SOURCES: list[Source] = [WellfoundSource()]
