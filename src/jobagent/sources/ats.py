"""Direct ATS job-board APIs: Greenhouse, Lever, Ashby, Workable.

All four expose public, unauthenticated JSON listing every *live* posting of a company, so
these are the freshest, most reliable sources we have. Each source takes a list of company
slugs (the `{slug}` in boards.greenhouse.io/{slug}, jobs.lever.co/{slug}, ...).

Also home to small helpers shared by the other source modules (yc.py, portfolio.py):
`html_to_text`, `ats_from_url`, `discover_slugs_from_urls`, `get_json`.
"""
from __future__ import annotations

import asyncio
import html as htmllib
import logging
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from jobagent.models import Job
from jobagent.sources.base import UA, SearchSpec, Source, title_matches

log = logging.getLogger(__name__)

DESC_LIMIT = 6000
CONCURRENCY = 8
TIMEOUT = httpx.Timeout(30.0, connect=15.0)
HEADERS = {"User-Agent": UA, "Accept": "application/json, text/plain, */*"}


# --------------------------------------------------------------------------- helpers

def html_to_text(raw: str | None, limit: int = DESC_LIMIT) -> str:
    """HTML (possibly entity-escaped, as Greenhouse returns it) -> compact plain text."""
    if not raw:
        return ""
    s = raw
    if "&lt;" in s and "<" not in s[:200]:
        s = htmllib.unescape(s)
    try:
        from selectolax.parser import HTMLParser

        tree = HTMLParser(s)
        for tag in ("script", "style"):
            for n in tree.css(tag):
                n.decompose()
        for n in tree.css("br"):
            n.replace_with("\n")
        for n in tree.css("p, div, li, h1, h2, h3, h4, h5, h6, tr, ul, ol"):
            n.insert_after("\n")
        text = (tree.body or tree.root).text(separator="") if (tree.body or tree.root) else s
    except Exception:  # pragma: no cover - selectolax is a dependency, but be safe
        text = re.sub(r"<[^>]+>", " ", s)
    text = htmllib.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return text[:limit]


def ats_from_url(url: str | None) -> str:
    """Classify an apply URL by the ATS that hosts the application form."""
    if not url:
        return ""
    u = url.lower()
    host = urlparse(u).netloc
    if "greenhouse.io" in host or "gh_jid=" in u:
        return "greenhouse"
    if "lever.co" in host:
        return "lever"
    if "ashbyhq.com" in host or "ashby_jid=" in u:
        return "ashby"
    if "workable.com" in host:
        return "workable"
    if "smartrecruiters.com" in host:
        return "smartrecruiters"
    if "myworkdayjobs.com" in host or "workday" in host:
        return "workday"
    if "linkedin.com" in host:
        return "linkedin"
    if "workatastartup.com" in u or "account.ycombinator.com" in host or "ycombinator.com" in host:
        return "workatastartup"
    return "other"


def iso_from_ms(ms) -> str | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except Exception:
        return None


def iso_from_s(s) -> str | None:
    try:
        return datetime.fromtimestamp(int(s), tz=timezone.utc).isoformat()
    except Exception:
        return None


def guess_remote(*texts: str | None) -> bool | None:
    blob = " ".join(t for t in texts if t).lower()
    if not blob:
        return None
    if "remote" in blob or "anywhere" in blob or "work from home" in blob:
        return True
    return None


def pretty_slug(slug: str) -> str:
    return unquote(slug).replace("-", " ").replace("_", " ").strip().title()


async def get_json(client: httpx.AsyncClient, url: str, *, method: str = "GET", retries: int = 2,
                   retry_statuses: tuple[int, ...] = (429, 500, 502, 503, 504), backoff: float = 1.5, **kw):
    """GET/POST returning parsed JSON, or None on 404. Retries network errors and `retry_statuses`
    with exponential backoff; other HTTP errors raise immediately."""
    headers = {**HEADERS, **kw.pop("headers", {})}
    timeout = kw.pop("timeout", TIMEOUT)
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = await client.request(method, url, headers=headers, timeout=timeout, **kw)
        except httpx.TransportError as e:
            last = e
            if attempt < retries:
                await asyncio.sleep(backoff * 2 ** attempt)
            continue
        if r.status_code == 404:
            return None
        if r.status_code in retry_statuses and attempt < retries:
            ra = r.headers.get("retry-after", "")
            await asyncio.sleep(float(ra) if ra.isdigit() else backoff * 2 ** attempt)
            continue
        r.raise_for_status()
        try:
            return r.json()
        except ValueError as e:
            raise RuntimeError(f"non-JSON response from {url}: {r.text[:120]!r}") from e
    raise last if last else RuntimeError(f"failed: {url}")


# ---- slug discovery -------------------------------------------------------------

_RESERVED = {"embed", "api", "v1", "j", "jobs", "job", "careers", "apply", "boards", "job_app", "search", "en", "www", ""}

_SLUG_PATTERNS: list[tuple[str, re.Pattern]] = [
    # boards.greenhouse.io/{slug}, job-boards.greenhouse.io/{slug}  (EU boards use a different API host; skipped)
    ("greenhouse", re.compile(r"https?://(?:boards|job-boards)\.greenhouse\.io/([^/?#]+)", re.I)),
    ("lever", re.compile(r"https?://jobs\.lever\.co/([^/?#]+)", re.I)),
    ("ashby", re.compile(r"https?://jobs\.ashbyhq\.com/([^/?#]+)", re.I)),
    ("workable", re.compile(r"https?://apply\.workable\.com/([^/?#]+)", re.I)),
    ("workable", re.compile(r"https?://([a-z0-9-]+)\.workable\.com", re.I)),
]


def discover_slugs_from_urls(urls: Iterable[str]) -> dict[str, set[str]]:
    """Extract ATS company slugs from apply/posting URLs.

    >>> discover_slugs_from_urls(["https://jobs.lever.co/acme/123", "https://boards.greenhouse.io/embed/job_app?for=foo"])
    {'lever': {'acme'}, 'greenhouse': {'foo'}}
    """
    out: dict[str, set[str]] = {}
    for url in urls:
        if not url:
            continue
        for ats, pat in _SLUG_PATTERNS:
            m = pat.search(url)
            if not m:
                continue
            slug = unquote(m.group(1)).strip()
            if ats == "workable" and slug.lower() in {"apply", "www", "jobs", "resumes"}:
                continue
            if slug.lower() in _RESERVED:
                # greenhouse embed: boards.greenhouse.io/embed/job_app?for={slug}&token=...
                q = parse_qs(urlparse(url).query)
                slug = (q.get("for") or [""])[0]
                if not slug:
                    continue
            out.setdefault(ats, set()).add(slug.lower() if ats != "ashby" else slug)
            break
    return out


# --------------------------------------------------------------------------- sources

class _SlugSource(Source):
    """Shared plumbing: fetch every slug concurrently, never raise, title-filter, dedupe."""

    name = "ats"

    def __init__(self, slugs: Iterable[str], concurrency: int = CONCURRENCY):
        self.slugs = list(dict.fromkeys(s for s in slugs if s))
        self.concurrency = concurrency

    async def fetch(self, client: httpx.AsyncClient, spec: SearchSpec) -> list[Job]:
        sem = asyncio.Semaphore(self.concurrency)

        async def one(slug: str) -> list[Job]:
            async with sem:
                try:
                    return await self.fetch_company(client, slug, spec)
                except Exception as e:
                    log.warning("%s: %s failed: %s", self.name, slug, e)
                    return []

        results = await asyncio.gather(*(one(s) for s in self.slugs))
        seen: set[str] = set()
        jobs: list[Job] = []
        for batch in results:
            for j in batch:
                if j.key not in seen:
                    seen.add(j.key)
                    jobs.append(j)
        return jobs

    async def fetch_company(self, client: httpx.AsyncClient, slug: str, spec: SearchSpec) -> list[Job]:
        raise NotImplementedError


class GreenhouseSource(_SlugSource):
    name = "greenhouse"

    async def fetch_company(self, client, slug, spec):
        data = await get_json(client, f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
        if not data:
            log.warning("greenhouse: board %s not found", slug)
            return []
        jobs = []
        for j in data.get("jobs", []):
            title = (j.get("title") or "").strip()
            if not title_matches(title, spec):
                continue
            loc = (j.get("location") or {}).get("name") or ""
            meta = " ".join(str(m.get("value") or "") for m in (j.get("metadata") or []) if isinstance(m, dict))
            jid = str(j.get("id"))
            url = j.get("absolute_url") or f"https://job-boards.greenhouse.io/{slug}/jobs/{jid}"
            jobs.append(Job(
                source=self.name,
                external_id=f"{slug}:{jid}",
                company=j.get("company_name") or pretty_slug(slug),
                title=title,
                url=url,
                # the canonical hosted form works even when absolute_url points at a custom careers site
                apply_url=f"https://job-boards.greenhouse.io/{slug}/jobs/{jid}",
                location=loc,
                remote=guess_remote(loc, meta),
                description=html_to_text(j.get("content")),
                ats="greenhouse",
                posted_at=j.get("first_published") or j.get("updated_at"),
                extra={"slug": slug, "updated_at": j.get("updated_at"),
                       "departments": [d.get("name") for d in j.get("departments") or []]},
            ))
        return jobs


class LeverSource(_SlugSource):
    name = "lever"

    async def fetch_company(self, client, slug, spec):
        data = await get_json(client, f"https://api.lever.co/v0/postings/{slug}?mode=json")
        if data is None:
            # a handful of companies live on Lever's EU instance
            data = await get_json(client, f"https://api.eu.lever.co/v0/postings/{slug}?mode=json")
        if not data:
            return []
        jobs = []
        for j in data:
            title = (j.get("text") or "").strip()
            if not title_matches(title, spec):
                continue
            cats = j.get("categories") or {}
            locs = cats.get("allLocations") or ([cats["location"]] if cats.get("location") else [])
            loc = " / ".join(locs)
            wpt = (j.get("workplaceType") or "").lower()
            desc_parts = [j.get("descriptionPlain") or html_to_text(j.get("description"))]
            for lst in j.get("lists") or []:
                desc_parts.append(f"{lst.get('text', '')}\n{html_to_text(lst.get('content'))}")
            desc_parts.append(j.get("additionalPlain") or "")
            sal = j.get("salaryRange") or {}
            salary = ""
            if sal.get("min") or sal.get("max"):
                salary = f"{sal.get('currency', '')} {sal.get('min', '')}-{sal.get('max', '')} {sal.get('interval', '')}".strip()
            jobs.append(Job(
                source=self.name,
                external_id=f"{slug}:{j.get('id')}",
                company=pretty_slug(slug),
                title=title,
                url=j.get("hostedUrl") or "",
                apply_url=j.get("applyUrl") or j.get("hostedUrl") or "",
                location=loc,
                remote=True if wpt == "remote" else (False if wpt == "onsite" else guess_remote(loc)),
                description="\n\n".join(p for p in desc_parts if p).strip()[:DESC_LIMIT],
                ats="lever",
                posted_at=iso_from_ms(j.get("createdAt")),
                salary=salary,
                extra={"slug": slug, "workplace": wpt, "team": cats.get("team"), "commitment": cats.get("commitment"),
                       "country": j.get("country")},
            ))
        return jobs


class AshbySource(_SlugSource):
    name = "ashby"

    async def fetch_company(self, client, slug, spec):
        data = await get_json(client, f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true")
        if not data:
            return []
        jobs = []
        for j in data.get("jobs", []):
            if j.get("isListed") is False:
                continue
            title = (j.get("title") or "").strip()
            if not title_matches(title, spec):
                continue
            locs = [j.get("location") or ""] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
            loc = " / ".join(dict.fromkeys(l for l in locs if l))
            wpt = (j.get("workplaceType") or "").lower()
            comp = j.get("compensation") or {}
            jobs.append(Job(
                source=self.name,
                external_id=f"{slug}:{j.get('id')}",
                company=pretty_slug(slug),
                title=title,
                url=j.get("jobUrl") or "",
                apply_url=j.get("applyUrl") or j.get("jobUrl") or "",
                location=loc,
                remote=True if (j.get("isRemote") or wpt == "remote") else guess_remote(loc),
                description=(j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")))[:DESC_LIMIT],
                ats="ashby",
                posted_at=j.get("publishedAt"),
                salary=comp.get("compensationTierSummary") or comp.get("scrapeableCompensationSalarySummary") or "",
                extra={"slug": slug, "workplace": wpt, "department": j.get("department"),
                       "employment_type": j.get("employmentType")},
            ))
        return jobs


class WorkableSource(_SlugSource):
    name = "workable"

    async def fetch_company(self, client, slug, spec):
        data = await get_json(client, f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true")
        if not data:
            return []
        company = data.get("name") or pretty_slug(slug)
        jobs = []
        for j in data.get("jobs", []):
            title = (j.get("title") or "").strip()
            if not title_matches(title, spec):
                continue
            locs = []
            for l in j.get("locations") or []:
                locs.append(", ".join(x for x in (l.get("city"), l.get("region"), l.get("country")) if x))
            if not locs:
                locs.append(", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x))
            loc = " / ".join(dict.fromkeys(l for l in locs if l))
            tele = j.get("telecommuting")
            jobs.append(Job(
                source=self.name,
                external_id=f"{slug}:{j.get('shortcode')}",
                company=company,
                title=title,
                url=j.get("url") or j.get("shortlink") or "",
                apply_url=j.get("application_url") or j.get("url") or "",
                location=loc,
                remote=True if tele in (True, "true", "True") else guess_remote(loc, title),
                description=html_to_text(j.get("description")),
                ats="workable",
                posted_at=j.get("published_on") or j.get("created_at"),
                extra={"slug": slug, "department": j.get("department"), "experience": j.get("experience"),
                       "employment_type": j.get("employment_type")},
            ))
        return jobs


# --------------------------------------------------------------------------- slug lists
# Every slug below was verified on 2026-10-02 (HTTP 200 with >= 1 live posting). The list is a hand-picked core
# (AI labs, AI-native startups, big tech, Indian unicorns: Sarvam, Meesho, Paytm, Razorpay, CRED, Groww, Glance,
# InMobi, Atlan, ...) plus every slug discovered via discover_slugs_from_urls() on the VC portfolio boards whose
# board currently has >= 1 posting passing title_matches(). ~170 of them have India-located openings.
# Re-verify any time with:  uv run python scripts/smoke_sources.py --verify-slugs

AI_COMPANY_SLUGS: dict[str, list[str]] = {
    "greenhouse": [  # 273
        "6sense", "abnormalsecurity", "addepar1", "adyen", "affirm", "agilityrobotics", "aidocmedical", "air",
        "airbnb", "airtable", "aisquared", "aiven36", "algolia", "ampsortation", "andurilindustries",
        "anthropic", "apiiro", "appier", "applovin", "armada", "asana", "assemblyai", "atoms", "attentive",
        "axon", "baton", "bitwarden", "blackoretechnologiesinc", "blinkhealth", "bloomreach", "bluefishai",
        "bluerivertech", "bluevineus", "boldmetrics", "boxinc", "branchmetrics", "braze", "brex", "brighthire",
        "cambridgemobiletelematics", "carta", "cellanome", "celonis", "chainguard", "chaosindustries", "checkr",
        "chime", "cloudflare", "coalition", "cockroachlabs", "cognite", "coinbase", "collibra", "commercetools",
        "conga", "connecteam", "contentful", "cookunity", "coreweave", "coursera", "cresta", "cultureamp",
        "current81", "databricks", "datacamp", "datadog", "descript", "devrev", "dialpad",
        "diligentcorporation", "discord", "doctolib", "dorsia", "dremio", "dropbox", "druva", "duolingo",
        "dynotherapeutics", "earnin", "elastic", "enigmaio", "enterpret", "epicgames", "ethoslife", "eve",
        "everlaw", "exactera", "faire", "fairmarkit", "fartherfinance", "feverup", "fictiv", "figma",
        "figureai", "fivetran", "flexport", "flyzipline", "foratravel", "formationbio", "fractile", "freenome",
        "future", "gametimeunited", "garnerhealth", "generalmatter", "gitlab", "glance", "gleanwork",
        "gomotive", "gongio", "goodfire", "gostudent", "grafanalabs", "graphcore", "groww", "gusto", "gyde",
        "hackerrank", "helloheart", "heygen", "hightouch", "honeycomb", "honor", "humeai", "inceptive",
        "inflectionai", "inmobi", "instacart", "instawork", "intercom", "isomorphiclabs", "iterable",
        "iterativehealth", "judihealth", "justworks", "kardfinancialinc", "kaseya", "kikoff", "klaviyo",
        "koboldmetals", "kodiak", "komodohealth", "labelbox", "lattice", "launchdarkly", "ledgy", "life360",
        "lightmatter", "lightningai", "lilasciences", "lyft", "machindustries", "materialbank", "mavenclinic",
        "mercury", "midihealth", "mongodb", "monzo", "myheritage", "nebius", "netlify", "netskope", "neuralink",
        "nextdoor", "nuro", "observeai", "obsidiansecurity", "oklo", "okta", "omadahealth", "onetrust",
        "openteams", "oscar", "pagerduty", "pallet", "pathai", "paveakatroveinformationtechnologies", "pendo",
        "peregrinetechnologies", "phaidra", "physicsx", "pingidentity", "pinterest", "planetscale", "podium81",
        "processstreet", "prodigal", "profluent", "project44", "purestorage", "qualtrics", "quince", "rapidsos",
        "razorpaysoftwareprivatelimited", "reddit", "refurbed", "relativity", "riotgames", "rithum",
        "robinhood", "roblox", "rubrik", "ruyaai", "salesloft", "sambanovasystems", "samsara", "scaleai",
        "seatgeek", "sendbird", "sentinellabs", "sesolabor", "shifttechnology", "shopmy", "signifyd95",
        "silananotechnologies", "simpplr", "singlestore", "skildai-careers", "snorkelai", "soundcloud71",
        "sourcegraph91", "spaceium", "spacex", "speechmatics", "stabilityai", "stackblitz", "starburst",
        "stripe", "synack", "tailscale", "tanium", "taxbit", "teads1", "telnyx54", "tenableinc", "tenstorrent",
        "thoughtworks", "tide", "tigera", "togetherai", "trueanomalyinc", "truebill", "truecaller", "tulip",
        "turing", "twilio", "twinhealth", "typeface", "typeform", "udio", "ujet", "upstart", "urbancompass",
        "vannevarlabs", "veeamsoftware", "vercel", "verkada", "via", "warp", "wavemm1", "waymo", "wayve",
        "wise", "wonderschool", "workato", "workstream", "xai", "xairatherapeutics", "zenoti", "zetaglobal",
        "zocdoc", "zonecompanysoftwareconsultingllc", "zscaler"
    ],
    "lever": [  # 50
        "100ms", "anyscale", "captivateiq", "collate", "cred", "curai", "dexterity", "dreamgames", "floqast",
        "flowlife", "getzuma", "gushwork", "healx", "hive", "iru", "kavak", "kinter", "kpler", "lyrahealth",
        "matillion", "meesho", "mindtickle", "moonpay", "neighbor", "neon", "nitra", "outreach", "palantir",
        "paytm", "pigment", "pipedrive", "pocketfm", "secureframe", "shieldai", "snaplogic", "sonarsource",
        "sonatype", "spotify", "stackhawk", "sysdig", "toptal", "tryjeeves", "tutorintelligence", "valarian",
        "velo3d", "waabi", "yuno", "zeta", "zilliz", "zoox"
    ],
    "ashby": [  # 369
        "1password", "1x", "abby-care", "Abridge", "adaptivesecurity", "Addi", "AgentMail", "airbyte",
        "airspace-intelligence.com", "AKASA", "alan", "ambiencehealthcare", "ambient.ai", "andela", "andercore",
        "anrok", "anyscale", "Applied", "arena", "Arlo", "artafinance", "astronomer", "atlan", "atlys",
        "automat", "ava-labs", "backbone", "baseten", "bayesianhealth", "bedrock-robotics", "benchling",
        "bettermoney", "beyondmath", "Blackpoint Cyber", "blooming-health", "bolna", "boon", "bubble",
        "bumbleinc", "camunda", "cape", "capsa", "cartesia", "catena", "causaly", "cerebras", "Certa",
        "certifyos", "chaidiscovery", "character", "checkly", "checkout.com", "cinder", "ClassDojo", "claylabs",
        "clickhouse", "clickup", "clubhouse", "cobot", "code-metal", "coderabbit", "cogent-security",
        "cognition", "cohere", "collective", "Commure", "composio", "concourse", "condor-software", "conduct",
        "confluent", "constructor", "convex-dev", "corridor", "cowboyspace", "crucibl", "crusoe", "cruxclimate",
        "ctgt", "cursor", "cuspai", "cyberhaven", "cylake-inc", "DatologyAI", "decagon", "deepgram", "delinea",
        "Distyl", "doctronic", "doppel", "drata", "dualentry", "dust", "ease-health", "edra", "eightsleep",
        "ekarobotics", "elevenlabs", "eliseai", "ellipsis-health", "ello", "ema", "engram", "etched", "exa",
        "fabrion", "factory", "fal-ai", "feathery", "fieldguide", "fireworks", "flux", "fonoa", "frontcareers",
        "furtherai", "G2", "gainsight", "gallatin", "gamma", "gecko-robotics", "generalintuition-medal",
        "generalist", "genesis-molecular-ai", "gimlet", "gorgias", "granola", "gritt", "hackerone",
        "hadrian-automation", "handshake", "happyrobot.ai", "Harmonic", "harvey", "haus", "hebbia-ai", "helion",
        "higgsfieldai", "hightouch", "hilberts", "hinge-health", "Hippocratic AI", "Homebound", "horizon3ai",
        "iceye", "ideogram", "illumio", "infinitus", "insitro", "intelligence", "inworld-ai", "ironcladhq",
        "joinbetter", "judgmentlabs", "k-id", "kaizenlabs", "kalshi", "KAYAK", "kong", "krea", "kustomer",
        "lambda", "langchain", "lapel", "lassie", "latent", "laurel", "leandata", "legora", "leland",
        "light-inc", "Lightfield", "lightspark", "lilt-corporate", "lilt-production", "linear", "linero", "lio",
        "livekit", "llamaindex", "lovable", "lumaai", "Luminary", "marianaminerals", "matter-labs", "mercor",
        "merge", "method", "method.security", "midjourney", "midstream", "mindrobotics", "Mintlify", "mirage",
        "miro", "mistral.ai", "modal", "moment", "montecarlodata", "motherduck", "motorway", "multiverse",
        "mural", "Nash", "neko-health", "neo-tax", "neon", "nooks", "northwoodspace", "notable", "notion",
        "novellia", "nubank", "nudge", "numeric", "oneapp", "openai", "openevidence", "openloophealth",
        "openrouter", "Outsmart", "parafin", "patreon", "pennylane", "percepta", "periodic-labs", "permitflow",
        "perplexity", "petual", "phantom", "phylo", "physicalintelligence", "pika", "pinecone", "plaid",
        "pluralis-research", "poolside", "possible-finance", "prefect", "preply", "PrimeIntellect", "primer.io",
        "prosper-ai", "protege", "pryzm", "pylon", "qualified-health-pbc", "quilter", "quora", "radar", "rain",
        "ramp", "raspberry", "ravenna", "realmalliance", "Redesign Health", "reducto", "reflectionai",
        "reindeer-ai", "relace", "relationrx", "relayfi", "render", "replit", "rescale", "restream", "rillet",
        "runetech", "rwazi", "sable", "sagecare", "salient", "sapiom", "sardine", "saris-ai", "saronic",
        "sarvam", "sauna.ai", "savvy", "seamflow", "secureframe", "sentilink", "sentry", "seqera.io",
        "sequence", "Serval", "sesame", "sevenai", "sierra", "sift", "skydio", "skyflow", "slash-financial",
        "sleeper", "snowflake", "snyk", "socket", "socure", "sola", "sona", "spaitial", "specter", "sphere",
        "sprinter-health", "spruceid", "StandardBots", "strava", "stuut-ai", "sunday", "suno", "supabase",
        "Superhuman Platform Inc", "synthesia", "Tabs", "tacto", "take2", "tako", "taktile", "talkiatry",
        "tavus", "tekion", "telepatia", "temporal", "tennr", "terraai", "tessera-labs", "teya",
        "thatgamecompany", "ThinkingMachines", "thoughtworks", "thread-ai", "thumbtack", "thyme-care",
        "tilderesearch", "TonicAI", "Tools for Humanity", "town", "traba", "traversal", "trawa", "tread",
        "triomics", "trm-labs", "Trunk Tools", "twelve-labs", "uipath", "uncountable", "Unlearn",
        "unstructured", "valeriehealth", "vanta", "Vetcove", "vizcom", "vooma", "warp", "wayve", "weaviate",
        "whitecircle", "windborne-systems", "wirescreen", "Wisdom-AI", "wispr-flow", "withclutch", "withpulley",
        "workos", "writer", "xero", "Zania", "Zeromark", "zip"
    ],
    "workable": [  # 9
        "apna", "arondite", "crewai", "huggingface", "jasper", "pensar", "runware", "tetrascience", "upsmith-1"
    ],
}


def all_ats_sources(slugs: dict[str, list[str]] | None = None) -> list[Source]:
    s = slugs or AI_COMPANY_SLUGS
    return [
        GreenhouseSource(s.get("greenhouse", [])),
        LeverSource(s.get("lever", [])),
        AshbySource(s.get("ashby", [])),
        WorkableSource(s.get("workable", [])),
    ]
