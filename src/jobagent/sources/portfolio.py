"""VC portfolio job boards.

Reverse-engineered backends (all public, read-only, no login):

* getro    - Getro-hosted boards (jobs.accel.com, jobs.techstars.com, ...). The board's Next.js page carries
             the network id in __NEXT_DATA__ (props.pageProps.network.id); jobs come from
             POST https://api.getro.com/api/v2/collections/{id}/search/jobs
             {"hitsPerPage": 20, "page": n, "query": "...", "filters": {"searchable_locations": ["India"]}}
* consider - Consider-hosted boards (jobs.sequoiacap.com, careers.peakxv.com, ...). Page HTML has
             window.serverInitialData = {..., "csrfToken": ..., "board": {"id": "sequoia-capital"}}; jobs come from
             POST {host}/api-boards/search-jobs with header x-csrf-token (+ session cookie)
             {"meta": {"size": 100, "sequence": <cursor>}, "board": {"id": ..., "isParent": true},
              "query": {"titlePrefix": "...", "locations": ["India"], "remoteOnly": true}}
             Any Consider host (incl. consider.com) serves any board id, which is how boards without
             their own Consider domain (Greylock, Nexus) are reached.
* a16z     - jobs.a16z.com is a custom Next.js (app router) site. First page is server-rendered into the RSC
             payload ("initialData"); more pages come from the `loadMorePublicJobs` server action:
             POST /jobs, header `next-action: <id>` (id scraped from the JS chunk that references it),
             body [filters, {"page": n, "limit": 100}], response is RSC text with the JSON on line "1:".
* index    - indexventures.com/startup-jobs queries an Elasticsearch domain directly from the browser using
             read-only credentials published in the page (ES_GLOBALS.url).
* atslinks - static page that just links to companies' ATS boards (conviction.com/jobs): we extract the
             Greenhouse/Lever/Ashby/Workable slugs and read those boards through the ATS APIs.
"""
from __future__ import annotations

import asyncio
import html as htmllib
import json
import logging
import re
from urllib.parse import quote, urljoin

import httpx

from jobagent.models import Job
from jobagent.sources.ats import (
    TIMEOUT,
    AshbySource,
    GreenhouseSource,
    LeverSource,
    WorkableSource,
    ats_from_url,
    discover_slugs_from_urls,
    get_json,
    guess_remote,
    html_to_text,
    iso_from_s,
)
from jobagent.sources.base import UA, SearchSpec, Source, title_matches

log = logging.getLogger(__name__)

CONSIDER_API_HOST = "https://consider.com"

# Boards on the same platform share one backend (and one rate limit: Getro answers 429, Consider's WAF 405),
# so concurrency is capped per platform across all PortfolioSource instances, not per board.
PLATFORM_CONCURRENCY = {"consider": 2, "getro": 3, "a16z": 3, "index": 3, "atslinks": 4}
RETRY_STATUSES = (403, 405, 429, 500, 502, 503, 504)
_platform_sems: dict[tuple[int, str], asyncio.Semaphore] = {}


def _platform_sem(platform: str) -> asyncio.Semaphore:
    key = (id(asyncio.get_running_loop()), platform)
    if key not in _platform_sems:
        _platform_sems[key] = asyncio.Semaphore(PLATFORM_CONCURRENCY.get(platform, 3))
    return _platform_sems[key]

# (name, url, platform). For Consider boards without their own domain the url is "consider:<board-id>".
PORTFOLIO_BOARDS: list[tuple[str, str, str]] = [
    # custom
    ("a16z", "https://jobs.a16z.com", "a16z"),
    ("Index Ventures", "https://www.indexventures.com/startup-jobs", "index"),
    ("Conviction", "https://www.conviction.com/jobs", "atslinks"),
    # Consider
    ("Sequoia", "https://jobs.sequoiacap.com", "consider"),
    ("Peak XV", "https://careers.peakxv.com", "consider"),
    ("Lightspeed", "https://jobs.lsvp.com", "consider"),
    ("Kleiner Perkins", "https://jobs.kleinerperkins.com", "consider"),
    ("Bessemer", "https://jobs.bvp.com", "consider"),
    ("Greylock", "consider:greylock-partners", "consider"),
    ("Nexus Venture Partners", "consider:nexus-venture-partners", "consider"),
    ("GV", "https://jobs.gv.com", "consider"),
    ("Felicis", "https://jobs.felicis.com", "consider"),
    ("Battery Ventures", "https://jobs.battery.com", "consider"),
    ("NEA", "https://careers.nea.com", "consider"),
    ("CRV", "https://jobs.crv.com", "consider"),
    ("Initialized", "https://jobs.initialized.com", "consider"),
    ("Notion Capital", "https://jobs.notion.vc", "consider"),
    ("Chiratae", "https://careers.chiratae.com", "consider"),
    ("USV", "https://jobs.usv.com", "consider"),
    # Getro
    ("Accel", "https://jobs.accel.com", "getro"),
    ("Techstars", "https://jobs.techstars.com", "getro"),
    ("General Catalyst", "https://jobs.generalcatalyst.com", "getro"),
    ("Khosla Ventures", "https://jobs.khoslaventures.com", "getro"),
    ("Founders Fund", "https://foundersfund.getro.com", "getro"),
    ("Antler", "https://careers.antler.co", "getro"),
    ("Speedinvest", "https://careers.speedinvest.com", "getro"),
    ("Insight Partners", "https://jobs.insightpartners.com", "getro"),
    ("Thrive Capital", "https://jobs.thrivecap.com", "getro"),
    ("8VC", "https://jobs.8vc.com", "getro"),
    ("Menlo Ventures", "https://jobs.menlovc.com", "getro"),
    ("Sapphire Ventures", "https://jobs.sapphireventures.com", "getro"),
    ("3one4 Capital", "https://jobs.3one4capital.com", "getro"),
    ("Redpoint", "https://careers.redpoint.com", "getro"),
    ("Lux Capital", "https://jobs.luxcapital.com", "getro"),
    ("Atomico", "https://careers.atomico.com", "getro"),
    ("SV Angel", "https://jobs.svangel.com", "getro"),
    ("NFX", "https://jobs.nfx.com", "getro"),
    ("DCVC", "https://jobs.dcvc.com", "getro"),
]


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _queries(spec: SearchSpec) -> list[str]:
    seen, out = set(), []
    for k in spec.keywords:
        q = " ".join(k.replace(",", " ").split())
        if q and q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    return out


def _wants_india(spec: SearchSpec) -> bool:
    return any("india" in l.lower() or l.lower() in ("pune", "bengaluru", "bangalore") for l in spec.locations) or not spec.locations


def _wants_remote(spec: SearchSpec) -> bool:
    return any("remote" in l.lower() for l in spec.locations) or not spec.locations


class PortfolioSource(Source):
    """One VC portfolio job board. `platform` is auto-detected from the page when not given."""

    def __init__(self, board_name: str, board_url: str, platform: str | None = None, *,
                 global_pages: int = 25, india_pages: int = 50, remote_pages: int = 25, concurrency: int = 3):
        self.board_name = board_name
        self.board_url = board_url.rstrip("/")
        self.platform = platform
        self.name = _slugify(board_name)
        self.global_pages = global_pages
        self.india_pages = india_pages
        self.remote_pages = remote_pages
        self.concurrency = concurrency
        self.sem = asyncio.Semaphore(concurrency)

    def __repr__(self) -> str:
        return f"PortfolioSource({self.board_name!r}, {self.board_url!r}, {self.platform!r})"

    # ------------------------------------------------------------------ entry point
    async def fetch(self, client: httpx.AsyncClient, spec: SearchSpec) -> list[Job]:
        self.sem = asyncio.Semaphore(self.concurrency)  # fresh per run (bound to the running loop)
        try:
            platform = self.platform or await self._detect(client)
            self.sem = _platform_sem(platform)
            fn = {
                "getro": self._fetch_getro,
                "consider": self._fetch_consider,
                "a16z": self._fetch_a16z,
                "index": self._fetch_index,
                "atslinks": self._fetch_atslinks,
            }.get(platform)
            if not fn:
                log.warning("%s: unsupported platform %r for %s", self.name, platform, self.board_url)
                return []
            jobs = await fn(client, spec)
        except Exception as e:
            log.warning("%s: board fetch failed: %s", self.name, e)
            return []
        out, seen = [], set()
        for j in jobs:
            if j.key in seen or not title_matches(j.title, spec):
                continue
            seen.add(j.key)
            out.append(j)
        return out

    async def _detect(self, client: httpx.AsyncClient) -> str:
        if self.board_url.startswith("consider:"):
            return "consider"
        r = await client.get(self.board_url, headers={"User-Agent": UA}, timeout=TIMEOUT, follow_redirects=True)
        h = r.text
        if "jobs.a16z.com" in str(r.url):
            return "a16z"
        if "cdn.getro.com" in h or '"network":{"id"' in h:
            return "getro"
        if "serverInitialData" in h and "csrfToken" in h:
            return "consider"
        if "ES_GLOBALS" in h:
            return "index"
        return "atslinks"

    async def _page(self, client: httpx.AsyncClient, url: str, retries: int = 3) -> httpx.Response:
        for attempt in range(retries + 1):
            async with self.sem:
                try:
                    r = await client.get(url, headers={"User-Agent": UA, "Accept": "text/html,*/*"},
                                         timeout=TIMEOUT, follow_redirects=True)
                except httpx.TransportError:
                    if attempt == retries:
                        raise
                    r = None
            if r is not None and (r.status_code not in RETRY_STATUSES or attempt == retries):
                r.raise_for_status()
                return r
            await asyncio.sleep(3 * 2 ** attempt)
        raise RuntimeError("unreachable")

    # ------------------------------------------------------------------ Getro
    async def _fetch_getro(self, client: httpx.AsyncClient, spec: SearchSpec) -> list[Job]:
        r = await self._page(client, self.board_url + "/jobs")
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
        if not m:
            raise RuntimeError("no __NEXT_DATA__ on getro board")
        net = json.loads(m.group(1))["props"]["pageProps"]["network"]
        nid = net["id"]
        base = f"{r.url.scheme}://{r.url.host}"
        api = f"https://api.getro.com/api/v2/collections/{nid}/search/jobs"

        async def search(query: str, filters: dict, max_pages: int) -> list[dict]:
            hits: list[dict] = []
            for page in range(max_pages):
                async with self.sem:
                    d = await get_json(client, api, method="POST",
                                       json={"hitsPerPage": 20, "page": page, "query": query, "filters": filters},
                                       headers={"Content-Type": "application/json", "Accept": "application/json"},
                                       retries=4, retry_statuses=RETRY_STATUSES, backoff=3)
                res = (d or {}).get("results") or {}
                batch = res.get("jobs") or []
                hits.extend(batch)
                if len(batch) < 20 or len(hits) >= int(res.get("count") or 0):
                    break
            return hits

        tasks = []
        for q in _queries(spec):
            if _wants_india(spec):
                tasks.append(search(q, {"searchable_locations": ["India"]}, 3))
            if _wants_remote(spec):
                tasks.append(search(q, {"work_mode": ["remote"]}, self.remote_pages))
            tasks.append(search(q, {}, self.global_pages))
        if _wants_india(spec):  # India is small enough on most boards to also sweep without a query
            tasks.append(search("", {"searchable_locations": ["India"]}, self.india_pages))
        jobs = []
        for res in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(res, Exception):
                log.warning("%s: getro query failed: %s", self.name, res)
                continue
            for j in res:
                org = j.get("organization") or {}
                apply = j.get("url") or ""
                locs = j.get("locations") or []
                wm = (j.get("work_mode") or "").lower()
                lo, hi = j.get("compensation_amount_min_cents"), j.get("compensation_amount_max_cents")
                salary = ""
                if lo or hi:
                    salary = f"{j.get('compensation_currency') or ''} {int((lo or 0) / 100)}-{int((hi or 0) / 100)} {j.get('compensation_period') or ''}".strip()
                jobs.append(Job(
                    source=self.name,
                    external_id=str(j.get("id")),
                    company=org.get("name") or "",
                    title=(j.get("title") or "").strip(),
                    url=f"{base}/companies/{org.get('slug')}/jobs/{j.get('slug')}" if org.get("slug") and j.get("slug") else apply,
                    apply_url=apply,
                    location=" / ".join(locs),
                    remote=True if wm == "remote" else (False if wm == "on_site" else guess_remote(*locs)),
                    ats=ats_from_url(apply),
                    posted_at=iso_from_s(j.get("created_at")),
                    salary=salary,
                    extra={"board": self.board_name, "platform": "getro", "work_mode": wm,
                           "searchable_locations": j.get("searchable_locations"), "skills": j.get("skills"),
                           "seniority": j.get("seniority"), "company_slug": org.get("slug")},
                ))
        return jobs

    # ------------------------------------------------------------------ Consider
    async def _consider_session(self, client: httpx.AsyncClient) -> tuple[str, str, str]:
        """-> (api_host, csrf_token, board_id)"""
        if self.board_url.startswith("consider:"):
            board_id = self.board_url.split(":", 1)[1]
            host = CONSIDER_API_HOST
            r = await self._page(client, host + "/boards")
        else:
            r = await self._page(client, self.board_url + "/jobs")
            host = f"{r.url.scheme}://{r.url.host}"
            m = re.search(r'"board":\{"id":"([^"]+)"', r.text) or re.search(r'"fixedBoard":"([^"]+)"', r.text)
            if not m:
                raise RuntimeError("no Consider board id on page")
            board_id = m.group(1)
        tok = re.search(r'"csrfToken":"([^"]+)"', r.text)
        if not tok:
            raise RuntimeError("no Consider csrf token")
        return host, tok.group(1), board_id

    async def _fetch_consider(self, shared: httpx.AsyncClient, spec: SearchSpec) -> list[Job]:
        # Own cookie jar per board: boards served via consider.com otherwise overwrite each other's session
        # cookie, which invalidates the other board's CSRF token (HTTP 412).
        async with httpx.AsyncClient(headers=dict(shared.headers), timeout=shared.timeout,
                                     follow_redirects=True) as client:
            return await self._fetch_consider_with(client, spec)

    async def _fetch_consider_with(self, client: httpx.AsyncClient, spec: SearchSpec) -> list[Job]:
        host, token, board_id = await self._consider_session(client)
        api = host + "/api-boards/search-jobs"

        async def search(query: dict, max_pages: int) -> list[dict]:
            out, seq = [], None
            for _ in range(max_pages):
                meta = {"size": 100}
                if seq:
                    meta["sequence"] = seq
                async with self.sem:
                    d = await get_json(client, api, method="POST",
                                       json={"meta": meta, "board": {"id": board_id, "isParent": True},
                                             "query": query, "grouped": False},
                                       headers={"x-csrf-token": token, "Content-Type": "application/json"},
                                       retries=4, retry_statuses=RETRY_STATUSES, backoff=3)
                d = d or {}
                if d.get("errors"):
                    raise RuntimeError(str(d["errors"])[:200])
                batch = d.get("jobs") or []
                out.extend(batch)
                seq = (d.get("meta") or {}).get("sequence")
                if len(batch) < 100 or not seq or len(out) >= int(d.get("total") or 0):
                    break
            return out

        tasks = []
        if _wants_india(spec):
            tasks.append(search({"locations": ["India"]}, self.india_pages))
        for q in _queries(spec):
            if _wants_remote(spec):
                tasks.append(search({"titlePrefix": q, "remoteOnly": True}, self.remote_pages))
            tasks.append(search({"titlePrefix": q}, self.global_pages))
        jobs = []
        for res in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(res, Exception):
                log.warning("%s: consider query failed: %s", self.name, res)
                continue
            for j in res:
                apply = j.get("applyUrl") or j.get("url") or ""
                locs = j.get("locations") or []
                sal = j.get("salary") or {}
                salary = ""
                if sal.get("minValue") or sal.get("maxValue"):
                    salary = (f"{(sal.get('currency') or {}).get('value', '')} {sal.get('minValue') or ''}-"
                              f"{sal.get('maxValue') or ''} / {(sal.get('period') or {}).get('value', '')}").strip()
                jobs.append(Job(
                    source=self.name,
                    external_id=f"{j.get('companySlug') or j.get('companyId')}:{j.get('jobId')}",
                    company=j.get("companyName") or "",
                    title=(j.get("title") or "").strip(),
                    url=j.get("url") or apply,
                    apply_url=apply,
                    location=" / ".join(locs),
                    remote=True if j.get("remote") else guess_remote(*locs),
                    ats=ats_from_url(apply),
                    posted_at=j.get("timeStamp"),
                    salary=salary,
                    extra={"board": self.board_name, "platform": "consider", "hybrid": j.get("hybrid"),
                           "min_years_exp": j.get("minYearsExp"), "company_domain": j.get("companyDomain"),
                           "seniority": j.get("jobSeniorityIds")},
                ))
        return jobs

    # ------------------------------------------------------------------ a16z
    _a16z_action: str | None = None

    async def _a16z_action_id(self, client: httpx.AsyncClient, html_text: str) -> str | None:
        if PortfolioSource._a16z_action:
            return PortfolioSource._a16z_action
        srcs = list(dict.fromkeys(re.findall(r'<script[^>]+src="(/_next/static/chunks/[^"]+\.js)"', html_text)))
        pat = re.compile(r'createServerReference\)\("([0-9a-f]{20,})"[^)]{0,200}?"loadMorePublicJobs"')

        async def scan(src: str) -> str | None:
            try:
                r = await self._page(client, urljoin(self.board_url, src))
                m = pat.search(r.text)
                return m.group(1) if m else None
            except Exception:
                return None

        for res in await asyncio.gather(*(scan(s) for s in srcs)):
            if res:
                PortfolioSource._a16z_action = res
                return res
        return None

    @staticmethod
    def _rsc_initial_data(html_text: str) -> dict | None:
        chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', html_text, re.S)
        s = "".join(json.loads('"' + c + '"') for c in chunks)
        i = s.find('{"initialData"')
        if i < 0:
            return None
        obj, _ = json.JSONDecoder().raw_decode(s, i)
        return obj.get("initialData")

    async def _fetch_a16z(self, client: httpx.AsyncClient, spec: SearchSpec) -> list[Job]:
        page0 = await self._page(client, self.board_url + "/jobs")
        action = await self._a16z_action_id(client, page0.text)
        raw: list[dict] = []

        def filt(q: str, *, india=False, remote=False) -> dict:
            u = "$undefined"
            return {"q": q, "roles": u, "markets": u, "funding_stages": u, "companySizes": u,
                    "locations": ["country:in"] if india else u, "salaryMin": u, "postedWithinDays": u,
                    "remote": True if remote else u, "hybrid": u, "hybridOrRemote": u, "internships": u,
                    "sort": "recent"}

        async def search(f: dict, max_pages: int) -> list[dict]:
            out = []
            for page in range(max_pages):
                for attempt in range(4):
                    async with self.sem:
                        r = await client.post(self.board_url + "/jobs", timeout=TIMEOUT, content=json.dumps(
                            [f, {"companyId": "$undefined", "page": page, "limit": 100}]),
                            headers={"User-Agent": UA, "next-action": action, "accept": "text/x-component",
                                     "content-type": "text/plain;charset=UTF-8"})
                    if r.status_code not in RETRY_STATUSES or attempt == 3:
                        break
                    await asyncio.sleep(3 * 2 ** attempt)
                r.raise_for_status()
                data = None
                for line in r.text.split("\n"):
                    if line.startswith("1:{"):
                        data = json.loads(line[2:])
                        break
                if not data:
                    break
                out.extend(data.get("jobs") or [])
                if data.get("isDone") or len(data.get("jobs") or []) < 100:
                    break
            return out

        async def search_ssr(q: str) -> list[dict]:  # fallback: server-rendered first 25 results
            r = await self._page(client, f"{self.board_url}/jobs?q={quote(q)}")
            d = self._rsc_initial_data(r.text) or {}
            return d.get("jobs") or []

        tasks = []
        for q in _queries(spec):
            if action:
                if _wants_india(spec):
                    tasks.append(search(filt(q, india=True), 3))
                if _wants_remote(spec):
                    tasks.append(search(filt(q, remote=True), self.remote_pages))
                tasks.append(search(filt(q), self.global_pages))
            else:
                tasks.append(search_ssr(q))
        if not action:
            log.warning("a16z: server action id not found, falling back to first page per query")
        for res in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(res, Exception):
                log.warning("%s: a16z query failed: %s", self.name, res)
                continue
            raw.extend(res)

        jobs = []
        for j in raw:
            apply = j.get("apply_url") or ""
            salary = j.get("compensation_summary") or ""
            if not salary and (j.get("salary_min") or j.get("salary_max")):
                salary = f"{j.get('salary_currency') or ''} {j.get('salary_min') or ''}-{j.get('salary_max') or ''} {j.get('salary_period') or ''}".strip()
            jobs.append(Job(
                source=self.name,
                external_id=str(j.get("id")),
                company=j.get("company_name") or "",
                title=(j.get("title") or "").strip(),
                url=apply or f"{self.board_url}/jobs/{j.get('company_slug')}",
                apply_url=apply,
                location=j.get("location") or " / ".join(j.get("locations") or []),
                remote=bool(j.get("remote")) or None,
                description=html_to_text(j.get("description_html")),
                ats=ats_from_url(apply),
                posted_at=j.get("posted_at"),
                salary=salary,
                extra={"board": self.board_name, "platform": "a16z", "hybrid": j.get("hybrid"),
                       "workplace_type": j.get("workplace_type"), "company_slug": j.get("company_slug"),
                       "company_stage": j.get("company_stage"), "markets": j.get("company_markets")},
            ))
        return jobs

    # ------------------------------------------------------------------ Index Ventures (Elasticsearch)
    async def _fetch_index(self, client: httpx.AsyncClient, spec: SearchSpec) -> list[Job]:
        page = await self._page(client, self.board_url)
        m = re.search(r'ES_GLOBALS\s*=\s*\{\s*url:\s*"([^"]+)"', page.text)
        if not m:
            raise RuntimeError("ES_GLOBALS not found")
        es = httpx.URL(m.group(1))
        endpoint = f"{es.scheme}://{es.host}/wagtail__startup_jobs_jobmodel/_search"
        auth = (es.username, es.password) if es.username else None
        text_fields = ["title^13", "get_synonyms^11", "job_category_name^2", "job_company_title^3"]
        loc_fields = ["job_geolocations_country_long", "job_geolocations_display_name", "job_georegions_name",
                      "job_geolocations_address"]

        async def search(q: str, loc: str | None, max_pages: int) -> list[dict]:
            must: list[dict] = [{"term": {"_django_content_type": "startup_jobs.JobModel"}}]
            if q:
                must.append({"query_string": {"query": q.replace("/", " "), "fields": text_fields,
                                              "default_operator": "and"}})
            if loc:
                must.append({"query_string": {"query": loc, "fields": loc_fields}})
            out = []
            for p in range(max_pages):
                body = {"size": 100, "from": p * 100, "track_total_hits": True, "query": {"bool": {"must": must}}}
                async with self.sem:
                    r = await client.post(endpoint, json=body, auth=auth, timeout=TIMEOUT,
                                          headers={"User-Agent": UA, "Referer": "https://www.indexventures.com/"})
                r.raise_for_status()
                hits = r.json()["hits"]
                out.extend(h["_source"] for h in hits["hits"])
                total = hits["total"]["value"] if isinstance(hits["total"], dict) else hits["total"]
                if len(hits["hits"]) < 100 or len(out) >= total:
                    break
            return out

        tasks = []
        for q in _queries(spec):
            if _wants_remote(spec):
                tasks.append(search(q, "Remote", self.remote_pages))
            tasks.append(search(q, None, self.global_pages))
        if _wants_india(spec):
            tasks.append(search("", "India", self.india_pages))
        jobs = []
        for res in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(res, Exception):
                log.warning("%s: index query failed: %s", self.name, res)
                continue
            for s in res:
                apply = s.get("job_application_url_filter") or ""
                locs = s.get("job_geolocations_display_name") or s.get("job_geolocations_address") or []
                regions = s.get("job_georegions_name") or []
                jobs.append(Job(
                    source=self.name,
                    external_id=str(s.get("pk")),
                    company=s.get("job_company_title") or "",
                    title=(s.get("title") or "").strip(),
                    url=apply or self.board_url,
                    apply_url=apply,
                    location=" / ".join(list(locs) + [r for r in regions if r not in locs]),
                    remote=guess_remote(*locs, *regions),
                    description=html_to_text(s.get("cleaned_job_description")),
                    ats=ats_from_url(apply),
                    extra={"board": self.board_name, "platform": "index", "sector": s.get("job_company_sector"),
                           "stage": s.get("job_company_stage_filter"), "functions": s.get("job_functions")},
                ))
        return jobs

    # ------------------------------------------------------------------ static page linking to ATS boards
    async def _fetch_atslinks(self, client: httpx.AsyncClient, spec: SearchSpec) -> list[Job]:
        r = await self._page(client, self.board_url)
        urls = re.findall(r'https?://[^\s"\'<>`]+', htmllib.unescape(r.text))
        slugs = discover_slugs_from_urls(urls)
        log.info("%s: discovered ATS slugs %s", self.name, {k: len(v) for k, v in slugs.items()})
        sources = [GreenhouseSource(slugs.get("greenhouse", [])), LeverSource(slugs.get("lever", [])),
                   AshbySource(slugs.get("ashby", [])), WorkableSource(slugs.get("workable", []))]
        jobs = []
        for res in await asyncio.gather(*(s.fetch(client, spec) for s in sources if s.slugs), return_exceptions=True):
            if isinstance(res, Exception):
                log.warning("%s: ats fetch failed: %s", self.name, res)
                continue
            for j in res:
                j.extra = {**j.extra, "board": self.board_name, "platform": "atslinks", "via": j.source}
                j.external_id = f"{j.source}:{j.external_id}"
                j.source = self.name
                jobs.append(j)
        return jobs


def all_portfolio_sources(boards: list[tuple[str, str, str]] | None = None, **kw) -> list[Source]:
    return [PortfolioSource(name, url, platform, **kw) for name, url, platform in (boards or PORTFOLIO_BOARDS)]
