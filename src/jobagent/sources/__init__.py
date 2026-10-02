"""Source registry.

`default_sources()` returns every job source we have. Add a new one by either
  * appending a factory to `_FACTORIES` below, or
  * creating a module listed in `PLUGIN_MODULES` that exposes `SOURCES: list[Source]`
    (imported lazily; a missing module is simply skipped).
"""
from __future__ import annotations

import importlib
import logging
from collections.abc import Callable

from jobagent.sources.base import SearchSpec, Source, title_matches  # noqa: F401  (re-exported)

log = logging.getLogger(__name__)

# Modules owned by other contributors (logged-in browser sources). Each must expose SOURCES: list[Source].
PLUGIN_MODULES: list[str] = [
    "jobagent.sources.linkedin",
    "jobagent.sources.naukri",
    "jobagent.sources.wellfound",
    "jobagent.sources.instahyre",
    "jobagent.sources.aggregators",
]


def _ats(cfg: dict) -> list[Source]:
    from jobagent.sources.ats import AI_COMPANY_SLUGS, all_ats_sources

    slugs = {k: list(v) for k, v in AI_COMPANY_SLUGS.items()}
    for ats, extra in (cfg.get("extra_slugs") or {}).items():  # e.g. {"greenhouse": ["acme"]}
        slugs[ats] = list(dict.fromkeys(slugs.get(ats, []) + list(extra)))
    return all_ats_sources(slugs)


def _yc(cfg: dict) -> list[Source]:
    from jobagent.sources.yc import YCSource

    return [YCSource(**(cfg.get("yc") or {}))]


def _portfolio(cfg: dict) -> list[Source]:
    from jobagent.sources.portfolio import PORTFOLIO_BOARDS, all_portfolio_sources

    boards = PORTFOLIO_BOARDS
    if cfg.get("portfolio_boards"):  # restrict to these board names
        wanted = {b.lower() for b in cfg["portfolio_boards"]}
        boards = [b for b in boards if b[0].lower() in wanted]
    return all_portfolio_sources(boards, **(cfg.get("portfolio") or {}))


def _plugins(cfg: dict) -> list[Source]:
    out: list[Source] = []
    for mod in PLUGIN_MODULES:
        try:
            m = importlib.import_module(mod)
        except ImportError:
            continue
        except Exception as e:  # a broken plugin must not take the registry down
            log.warning("source plugin %s failed to import: %s", mod, e)
            continue
        out.extend(getattr(m, "SOURCES", []) or [])
    return out


_FACTORIES: list[Callable[[dict], list[Source]]] = [_ats, _yc, _portfolio, _plugins]


def default_sources(spec_cfg: dict | None = None) -> list[Source]:
    """All sources. `spec_cfg` (all keys optional):
        disable: [source names]          e.g. ["yc", "a16z", "linkedin"]
        only:    [source names]
        extra_slugs: {ats: [slugs]}      added to AI_COMPANY_SLUGS
        portfolio_boards: [board names]  subset of PORTFOLIO_BOARDS
        portfolio: {...}                 kwargs for PortfolioSource (global_pages, india_pages, remote_pages, ...)
        yc: {...}                        kwargs for YCSource (max_companies, fetch_descriptions, ...)
    """
    cfg = spec_cfg or {}
    sources: list[Source] = []
    for factory in _FACTORIES:
        try:
            sources.extend(factory(cfg))
        except Exception as e:
            log.warning("source factory %s failed: %s", getattr(factory, "__name__", factory), e)
    if cfg.get("only"):
        only = set(cfg["only"])
        sources = [s for s in sources if s.name in only]
    if cfg.get("disable"):
        off = set(cfg["disable"])
        sources = [s for s in sources if s.name not in off]
    return sources
