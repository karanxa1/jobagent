"""Smoke-test the HTTP job sources (ATS boards, YC, VC portfolio boards).

    uv run python scripts/smoke_sources.py                 # all sources
    uv run python scripts/smoke_sources.py --only yc a16z  # a subset (by Source.name)
    uv run python scripts/smoke_sources.py --verify-slugs  # re-check AI_COMPANY_SLUGS are alive

Read-only: only GETs/POSTs against public listing APIs, never applies to anything.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import logging
import sys
import time

import httpx

from jobagent.sources import default_sources
from jobagent.sources.ats import AI_COMPANY_SLUGS, discover_slugs_from_urls, get_json
from jobagent.sources.base import UA, SearchSpec, Source

INDIA = ("india", "bengaluru", "bangalore", "pune", "hyderabad", "mumbai", "gurgaon", "gurugram", "delhi", "noida", "chennai")


async def run_one(client: httpx.AsyncClient, src: Source, spec: SearchSpec, sem: asyncio.Semaphore):
    async with sem:
        t = time.time()
        try:
            jobs = await src.fetch(client, spec)
            err = None
        except Exception as e:  # fetch() must never raise - flag it loudly if one does
            jobs, err = [], f"{type(e).__name__}: {e}"
        return src, jobs, time.time() - t, err


MINE = ("jobagent.sources.ats", "jobagent.sources.yc", "jobagent.sources.portfolio")


async def smoke(only: list[str] | None, parallel: int, include_all: bool = False) -> int:
    spec = SearchSpec()
    sources = [s for s in default_sources() if not s.needs_login]
    if not include_all:  # the HTTP sources built here (plugins like naukri/instahyre have their own tests)
        sources = [s for s in sources if type(s).__module__ in MINE]
    if only:
        sources = [s for s in sources if s.name in set(only)]
    sem = asyncio.Semaphore(parallel)
    limits = httpx.Limits(max_connections=64, max_keepalive_connections=32)
    async with httpx.AsyncClient(headers={"User-Agent": UA}, follow_redirects=True, limits=limits) as client:
        results = await asyncio.gather(*(run_one(client, s, spec, sem) for s in sources))

    total, empty, all_urls = 0, [], []
    print("\n" + "=" * 100)
    for src, jobs, dt, err in results:
        total += len(jobs)
        india = sum(any(k in (j.location or "").lower() for k in INDIA) for j in jobs)
        remote = sum(bool(j.remote) for j in jobs)
        ats = collections.Counter(j.ats for j in jobs).most_common(4)
        print(f"\n## {src.name:<24} {len(jobs):>5} jobs  india={india:<4} remote={remote:<4} {dt:6.1f}s  ats={ats}")
        if err:
            print(f"   !! fetch raised: {err}")
        if not jobs:
            empty.append(src.name)
        for j in jobs[:3]:
            print(f"   - {j.title[:60]:<60} | {j.company[:22]:<22} | {(j.location or '')[:34]:<34} | {j.ats:<14} | {j.apply_url[:70]}")
        all_urls += [j.apply_url for j in jobs]
    print("\n" + "=" * 100)
    print(f"TOTAL {total} jobs from {len(results)} sources; empty: {empty or 'none'}")
    new = discover_slugs_from_urls(all_urls)
    known = {k: {s.lower() for s in v} for k, v in AI_COMPANY_SLUGS.items()}
    fresh = {k: sorted(s for s in v if s.lower() not in known.get(k, set())) for k, v in new.items()}
    print("slugs seen in apply URLs not yet in AI_COMPANY_SLUGS:", {k: len(v) for k, v in fresh.items()})
    return 1 if empty else 0


async def verify_slugs() -> int:
    urls = {
        "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{}/jobs",
        "lever": "https://api.lever.co/v0/postings/{}?mode=json",
        "ashby": "https://api.ashbyhq.com/posting-api/job-board/{}",
        "workable": "https://apply.workable.com/api/v1/widget/accounts/{}",
    }
    sem = asyncio.Semaphore(12)
    dead: list[str] = []
    async with httpx.AsyncClient(headers={"User-Agent": UA}) as client:
        async def check(ats: str, slug: str):
            async with sem:
                try:
                    d = await get_json(client, urls[ats].format(slug))
                except Exception:
                    d = None
                n = len(d) if isinstance(d, list) else len((d or {}).get("jobs") or [])
                if n == 0:
                    dead.append(f"{ats}:{slug}")
        await asyncio.gather(*(check(a, s) for a, ss in AI_COMPANY_SLUGS.items() for s in ss))
    n = sum(len(v) for v in AI_COMPANY_SLUGS.values())
    print(f"{n - len(dead)}/{n} slugs alive. dead/empty: {sorted(dead) or 'none'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="source names to run")
    ap.add_argument("--parallel", type=int, default=6, help="sources fetched concurrently")
    ap.add_argument("--verify-slugs", action="store_true")
    ap.add_argument("--all", action="store_true", help="also run plugin sources (non-login ones)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if a.verify_slugs:
        return asyncio.run(verify_slugs())
    return asyncio.run(smoke(a.only, a.parallel, a.all))


if __name__ == "__main__":
    sys.exit(main())
