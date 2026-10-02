"""Smoke-test the logged-out discovery sources with the default SearchSpec.

    uv run python scripts/smoke_sources_logged_out.py              # every source, default settings
    uv run python scripts/smoke_sources_logged_out.py --quick      # trimmed LinkedIn/Naukri/Wellfound/Instahyre
    uv run python scripts/smoke_sources_logged_out.py linkedin hn_whoishiring   # only these source names

Prints, per source: job count, elapsed time, ATS breakdown, and 3 sample jobs
(title | company | location | ats | apply_url). Never applies, submits, or logs in.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import logging
import sys
import time

import httpx

from jobagent.sources import aggregators, instahyre, linkedin, naukri, wellfound
from jobagent.sources.base import SearchSpec, Source


def build_sources(quick: bool) -> list[Source]:
    if not quick:
        return [*linkedin.SOURCES, *naukri.SOURCES, *wellfound.SOURCES, *instahyre.SOURCES, *aggregators.SOURCES]
    kws = ["AI Engineer", "LLM Engineer"]
    return [
        linkedin.LinkedInSource(keywords=kws, max_pages=1, max_details=15,
                                locations={k: linkedin.DEFAULT_LOCATIONS[k] for k in ("India", "Pune", "Remote")}),
        naukri.NaukriSource(keywords=kws, max_pages=1, max_details=5),
        wellfound.WellfoundSource(roles=["ai-engineer", "machine-learning-engineer"], max_pages=1),
        instahyre.InstahyreSource(queries=["AI Engineer", "LLM"], max_pages=1, max_details=20),
        *aggregators.SOURCES,
    ]


async def run_one(src: Source, client: httpx.AsyncClient, spec: SearchSpec) -> tuple[str, int, float]:
    t0 = time.monotonic()
    try:
        jobs = await src.fetch(client, spec)
    except Exception as e:  # sources must not raise; flag it loudly if one does
        print(f"!! {src.name} RAISED {type(e).__name__}: {e}")
        jobs = []
    dt = time.monotonic() - t0
    ats = collections.Counter(j.ats or "?" for j in jobs).most_common(6)
    remote = sum(1 for j in jobs if j.remote)
    visa = sum(1 for j in jobs if j.visa)
    print(f"\n=== {src.name}: {len(jobs)} jobs in {dt:.0f}s  (remote={remote}, visa-mention={visa}, ats={ats})")
    for j in jobs[:3]:
        print(f"    {j.title} | {j.company} | {j.location[:60]} | {j.ats or '-'} | {j.apply_url}")
    sys.stdout.flush()
    return src.name, len(jobs), dt


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("only", nargs="*", help="source names to run (default: all)")
    ap.add_argument("--quick", action="store_true", help="trimmed settings for the slow sources")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    spec = SearchSpec()
    sources = [s for s in build_sources(a.quick) if not a.only or s.name in a.only]
    results = []
    async with httpx.AsyncClient(http2=False, limits=httpx.Limits(max_connections=20)) as client:
        for s in sources:  # sequential: keeps per-site throttling honest and output readable
            results.append(await run_one(s, client, spec))
    print("\n=== summary")
    for name, n, dt in results:
        print(f"  {name:16s} {n:5d} jobs  {dt:6.0f}s")
    print(f"  {'TOTAL':16s} {sum(n for _, n, _ in results):5d}")


if __name__ == "__main__":
    asyncio.run(main())
