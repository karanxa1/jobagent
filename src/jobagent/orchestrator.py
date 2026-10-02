"""discover -> triage -> apply, with N parallel browser workers and per-site limits."""
from __future__ import annotations

import asyncio
import json
import re
import logging
import os
import random
import time
from collections import defaultdict

import httpx
import yaml

from jobagent import browsers
from jobagent.applier import Applier
from jobagent.config import Config, load_profile
from jobagent.db import DB
from jobagent.ledger import Ledger
from jobagent.llm import Clef, Luna
from jobagent.models import Job, Status
from jobagent.otp import make_otp_provider
from jobagent.profile import resume_text, todos
from jobagent.sources import default_sources
from jobagent.sources.base import UA, SearchSpec
from jobagent.triage import rescore, retriage_missing, triage_pending

log = logging.getLogger(__name__)

# One Python event loop can't drive ~40 browser-use agents (CDP keepalives time out and every browser drops at
# once), so scripts/start.sh runs apply.processes daemons. Shard 0 also discovers, triages, sends email and
# watches for leaked browsers; every shard applies. Workers and per-site limits are split between shards.
SHARD = int(os.environ.get("JOBAGENT_SHARD", "0"))
SHARDS = max(1, int(os.environ.get("JOBAGENT_SHARDS", "1")))


def _share(n: int) -> int:
    """This shard's part of a limit that applies to all shards together (0 = another shard owns it)."""
    return n // SHARDS + (1 if SHARD < n % SHARDS else 0)


async def discover(cfg: Config, db: DB, only: list[str] | None = None) -> int:
    spec = SearchSpec(locations=cfg.get("search.locations", SearchSpec().locations),
                      max_age_days=cfg.get("search.max_age_days", 45))
    if kw := cfg.get("search.keywords"):
        spec.keywords = kw
    learned_path = cfg.root / "learned_slugs.yaml"
    learned = yaml.safe_load(learned_path.read_text()) if learned_path.exists() else {}
    learned = learned or {}
    src_cfg = dict(cfg.get("sources", {}) or {})
    src_cfg["extra_slugs"] = {k: list(set(v) | set((src_cfg.get("extra_slugs") or {}).get(k, [])))
                              for k, v in learned.items()}
    sources = [s for s in default_sources(src_cfg) if not only or s.name in only]
    async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=30, follow_redirects=True) as client:
        async def run(src):
            try:
                jobs = await src.fetch(client, spec)
            except Exception as e:  # noqa: BLE001 - a broken source must not stop the others
                log.warning("source %s crashed: %s", src.name, e)
                return src.name, 0, 0
            return src.name, len(jobs), db.upsert_jobs(jobs)

        results = await asyncio.gather(*(run(s) for s in sources))

    # Learn new ATS boards from apply links seen anywhere (VC boards, LinkedIn, YC...): next cycle the
    # ATS sources read those companies' full boards directly.
    from jobagent.sources.ats import AI_COMPANY_SLUGS, discover_slugs_from_urls

    urls = [r[0] for r in db._x("SELECT apply_url FROM jobs").fetchall()]
    added = 0
    for ats, slugs in discover_slugs_from_urls(urls).items():
        known = set(AI_COMPANY_SLUGS.get(ats, [])) | set(learned.get(ats, []))
        new_slugs = sorted(slugs - known)
        if new_slugs:
            learned[ats] = sorted(set(learned.get(ats, [])) | set(new_slugs))
            added += len(new_slugs)
    if added:
        learned_path.write_text(yaml.safe_dump(learned, sort_keys=True))
        log.info("learned %d new ATS company boards -> %s", added, learned_path.name)
    for name, found, new in results:
        log.info("source %-22s found %5d  new %5d", name, found, new)
    return sum(r[2] for r in results)


SITE_HOSTS = {"linkedin": ["linkedin.com"], "naukri": ["naukri.com"], "wellfound": ["wellfound.com"],
              "instahyre": ["instahyre.com"], "yc": ["ycombinator.com", "workatastartup.com"],
              "hirist": ["hirist.tech"], "cutshort": ["cutshort.io"],
              "google": ["microsoft.com"]}  # Microsoft Careers only offers Google / LinkedIn / Microsoft sign-in


def logged_in_hosts(cfg: Config) -> set[str]:
    f = cfg.storage_state_path.with_name("logged_in_sites.json")
    if not (f.exists() and cfg.storage_state_path.exists()):
        return set()
    return {h for site in json.loads(f.read_text()) for h in SITE_HOSTS.get(site, [])}


class _NetWatch(logging.Handler):
    """Remembers when any browser last hit a network outage (the Mac's Wi-Fi dropped): applications running
    through one fail for reasons that have nothing to do with the job."""
    last = 0.0
    PAT = re.compile(r"ERR_INTERNET_DISCONNECTED|ERR_NETWORK_CHANGED|ERR_NAME_NOT_RESOLVED|ERR_NETWORK_IO_SUSPENDED")

    def emit(self, record):
        try:
            if self.PAT.search(record.getMessage()):
                _NetWatch.last = time.time()
        except Exception:  # noqa: BLE001
            pass


logging.getLogger("browser_use").addHandler(_NetWatch())


SITE_FLAG = re.compile(r"possible[- ]spam|flagged (it |this |the (application|submission) )?(as|for) (possible )?spam|marked as spam|spam[- ](application|submission|error|filter|detect\w*)|(detected|identified|rejected) as spam|account (has been )?(restricted|suspended|"
                       r"blocked)|temporarily restricted|too many (applications|requests)|unusual activity", re.I)


COMPANY_CAP = re.compile(r"maximum number of applications|application limit|already applied to (the )?maximum|"
                         r"applications are limited to|limit(ed)? (of )?\d+ applications|"
                         r"too many applications (to|for|with) (this|our) (company|organi[sz]ation)", re.I)


TRIP_HOURS = 6


class HostLimiter:
    """Per-site concurrency + daily caps (shared across all workers)."""

    def __init__(self, cfg: Config, db: DB):
        self.conc = cfg.get("apply.host_concurrency", {}) or {}
        self.caps = cfg.get("apply.host_daily_cap", {}) or {}
        self.gaps = cfg.get("apply.host_min_gap_seconds", {}) or {}
        self.login_hosts = set(cfg.get("apply.login_hosts", []) or [])
        self.logged_in = logged_in_hosts(cfg)
        self.assist_only = set(cfg.get("apply.assist_only_hosts", []) or [])
        self.manual = set(cfg.get("apply.manual_hosts", []) or [])
        self.otp_hosts = set(cfg.get("apply.otp_hosts", []) or [])
        self.no_otp = cfg.get("otp.provider", "none") == "none"
        self.headless = not cfg.get("apply.assist", False)  # i.e. nobody is at the screen to solve captchas
        self.active: dict[str, int] = defaultdict(int)
        self.trip_file = cfg.db_path.with_name("paused_hosts.json")
        self.tripped: dict[str, float] = {}  # host -> paused until (epoch seconds)
        try:
            self.tripped = json.loads(self.trip_file.read_text()) if self.trip_file.exists() else {}
        except Exception:  # noqa: BLE001
            self.tripped = {}
        self.last_start: dict[str, float] = {}
        self.db = db

    def trip(self, host: str, why: str) -> None:
        """A site flagged us as spam / restricted the account: stop using it for the rest of this run."""
        if self.tripped.get(host, 0) < time.time():
            self.tripped[host] = time.time() + TRIP_HOURS * 3600
            log.error("SITE FLAGGED US: pausing %s for %dh (survives restarts). Reason: %s", host, TRIP_HOURS, why[:200])
            try:  # persisted so a daemon restart doesn't walk straight back into the block
                saved = json.loads(self.trip_file.read_text()) if self.trip_file.exists() else {}
                saved[host] = time.time() + TRIP_HOURS * 3600
                self.trip_file.write_text(json.dumps(saved, indent=1))
            except Exception as e:  # noqa: BLE001
                log.warning("could not persist paused host %s: %s", host, e)

    def started(self, host: str) -> None:
        self.active[host] += 1
        self.last_start[host] = time.monotonic()

    def saturated(self) -> set[str]:
        hosts = set(self.active) | set(self.caps)
        out = {h for h in hosts if self.active[h] >= _share(self.conc.get(h, self.conc.get("default", 4)))}
        # space out applications per site: bursts are what trigger captchas and account flags
        now = time.monotonic()
        out |= {h for h, t in self.last_start.items()
                if now - t < self.gaps.get(h, self.gaps.get("default", 0)) * SHARDS * random.uniform(1.0, 1.5)}
        out |= {h for h, cap in self.caps.items() if self.db.applied_today(h) >= cap}
        # boards that need an account are pointless until `jobagent login <site>` has saved a session for them
        out |= self.login_hosts - self.logged_in
        # sites whose bot protection blocks automated browsers: only `jobagent assist` (you present) touches them
        if self.headless:
            out |= self.assist_only
        out |= {h for h, until in self.tripped.items() if until > time.time()}  # paused sites (expire on time)
        out |= self.manual  # sites that reject any automated submission: you apply from MANUAL_APPLY.md
        # sites that always email a security code: hold them until an inbox provider is configured
        if self.no_otp:
            out |= self.otp_hosts
        return out


async def apply_queue(cfg: Config, db: DB, luna: Luna, clef: Clef, workers: int | None = None,
                      limit: int | None = None) -> dict[str, int]:
    profile = load_profile(cfg)
    if missing := todos(profile):
        log.warning("profile.yaml still has TODOs %s: questions needing them will be flagged for you", missing)
    otp = make_otp_provider(cfg, luna, clef)
    try:
        await otp.start()
    except Exception as e:  # noqa: BLE001
        log.warning("OTP provider failed to start (%s): applications needing email codes will be flagged", e)
        from jobagent.otp import NoOTP
        otp = NoOTP(cfg, luna)
    applier = Applier(cfg, luna, clef, otp, profile, resume_text(cfg.resume_path))
    ledger = Ledger(cfg.ledger_path)
    already = lambda r: ledger.has(Job(**json.loads(r["data"])))
    limiter = HostLimiter(cfg, db)
    skip_ats = set(cfg.get("apply.skip_ats", []) or [])
    workers = -(-(workers or cfg.get("apply.workers", 8)) // SHARDS)  # apply.workers is the total over all shards
    limit = limit if limit is not None else (cfg.get("apply.max_per_run", 0) or 10**9)
    stats: dict[str, int] = defaultdict(int)
    started = 0
    lock = asyncio.Lock()

    async def worker(wid: int):
        nonlocal started
        idle = 0
        while True:
            async with lock:
                if started >= limit:
                    return
                # RAM budget: keep apply.min_free_gb spare, plus room for the browser this claim would start
                import psutil
                avail_gb = psutil.virtual_memory().available / 2**30
                need_gb = cfg.get("apply.min_free_gb", 2) + cfg.get("apply.new_browser_gb", 1.0)
                if avail_gb < need_gb:
                    row = None
                    if idle % 6 == 0:
                        log.info("[w%d] holding: %.1f GB RAM free, need %.1f GB to start another browser",
                                 wid, avail_gb, need_gb)
                else:
                    row = db.claim_next(limiter.saturated(), skip_ats, already, cfg.get("apply.max_attempts", 2) + 1)
                if row:
                    started += 1
                    limiter.started(row["host"])
            if not row:
                # other workers may still free a saturated host; give up after a few empty polls
                idle += 1
                # per-site gaps make most polls come back empty while ramping up: a worker that quit after a minute
                # of that capped parallelism at whatever got a job early (~8). Stay while others are working.
                if idle > 60 or not any(limiter.active.values()):
                    log.info("[w%d] idle exit after %d empty polls (active=%s)", wid, idle,
                             {h: n for h, n in limiter.active.items() if n})
                    return
                if idle % 6 == 0:
                    log.info("[w%d] nothing claimable for %ds; blocked hosts: %s", wid, idle * 10,
                             sorted(h for h in limiter.saturated() if "." in h)[:12])
                await asyncio.sleep(10)
                continue
            idle = 0
            job = Job(**json.loads(row["data"]))
            log.info("[w%d] applying: %s | %s (%s)", wid, job.company, job.title, job.apply_url)
            t0 = time.time()  # not `started`: that name is the pass-wide job counter (nonlocal)
            try:
                # hard cap per application: one hung page must not stall the whole pass (the rest of the workers
                # exit when idle, so a stuck job used to block new work for an hour). Cancelling runs the applier's
                # finally block, which kills that job's browser.
                res = await asyncio.wait_for(applier.apply(job, wid), timeout=cfg.get("apply.job_timeout_min", 20) * 60)
            except asyncio.TimeoutError:
                log.warning("[w%d] %s timed out after %d min", wid, job.key, cfg.get("apply.job_timeout_min", 20))
                from jobagent.models import ApplyResult
                res = ApplyResult(Status.FAILED, f"timeout: application took over {cfg.get('apply.job_timeout_min', 20)} min")
            except Exception as e:  # noqa: BLE001
                log.exception("[w%d] crash on %s", wid, job.key)
                from jobagent.models import ApplyResult
                res = ApplyResult(Status.FAILED, f"crash: {e}")
            finally:
                limiter.active[row["host"]] -= 1
            if res.status == Status.QUEUED and (res.summary or "").startswith("deferred:"):
                # embeds / redirects to a paused site: give the attempt back and retry later. Pause this job board
                # too only when its own apply link lands there (preflight), not when the agent wandered over.
                if "lands on paused site" in res.summary:
                    limiter.trip(row["host"], res.summary)
                db._x("UPDATE jobs SET status=?, attempts=MAX(attempts-1,0) WHERE key=?", (Status.QUEUED, job.key))
                continue
            if res.status in (Status.FAILED, Status.NEEDS_HUMAN) and _NetWatch.last >= t0:
                # the network dropped mid-run: not the job's fault, retry it later at no cost
                log.info("[w%d] %s: network dropped during the run; requeued", wid, job.company)
                db._x("UPDATE jobs SET status=?, attempts=MAX(attempts-1,0) WHERE key=?", (Status.QUEUED, job.key))
                continue
            db.finish(job.key, res.status, {"summary": res.summary, "screenshot": res.screenshot, **res.answers})
            if res.status != Status.APPLIED and SITE_FLAG.search(res.summary or ""):
                # pause the site that flagged us: the agent may have followed the job board's link onto an ATS
                ats_hit = next((h for k, h in (("ashby", "ashbyhq.com"), ("greenhouse", "greenhouse.io"),
                                               ("lever", "lever.co"), ("workable", "workable.com"),
                                               ("smartrecruiters", "smartrecruiters.com")) if k in res.summary.lower()), None)
                limiter.trip(ats_hit or row["host"], res.summary)
            if res.status != Status.APPLIED and re.search(r"possible[- ]spam|flagged as spam", res.summary or "", re.I):
                # a per-submission spam score (Ashby): don't keep re-submitting to the same employer
                n = db.skip_company(job.company, "employer's form flagged a submission as possible spam")
                log.info("%s flagged a submission as possible spam; skipped %d more of its roles", job.company, n)
            if res.status == Status.REJECTED and re.search(r"\bITAR\b|security clearance|top[- ]secret|u\.?s\.? citizenship|"
                                                         r"u\.?s\.? person", res.summary or "", re.I):
                # defense / government employers apply these to every role: don't open the rest one by one
                n = db.skip_company(job.company, "employer requires US citizenship / ITAR / clearance")
                log.info("%s requires US citizenship/ITAR/clearance; skipped %d more of its roles", job.company, n)
            if COMPANY_CAP.search(res.summary or ""):
                # the employer caps applications per candidate: the rest of its roles would all bounce
                n = db.skip_company(job.company, "company application limit reached")
                log.info("%s caps applications per candidate; skipped %d more of its roles", job.company, n)
            if res.status == Status.APPLIED:
                ledger.record(job, res.summary[:120])
            stats[res.status] += 1
            log.info("[w%d] %s -> %s: %s", wid, job.company, res.status.upper(), res.summary[:160])

    try:
        await asyncio.gather(*(worker(SHARD * workers + i) for i in range(workers)))
    finally:
        await otp.stop()
    return dict(stats)


def send_outbox(cfg: Config, db: DB) -> int:
    """Send queued application emails over Gmail SMTP (needs GMAIL_APP_PASSWORD). Returns #sent."""
    import mimetypes
    import os
    import smtplib
    from email.message import EmailMessage
    from pathlib import Path

    from jobagent.db import now

    pw = os.environ.get("GMAIL_APP_PASSWORD")
    rows = db._x("SELECT * FROM outbox WHERE status='pending' ORDER BY id").fetchall()
    if not pw or not rows:
        return 0
    user = cfg.get("otp.imap.user")
    sent = 0
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(user, pw.replace(" ", ""))
        for r in rows:
            msg = EmailMessage()
            msg["From"], msg["To"], msg["Subject"] = user, r["to_addr"], r["subject"]
            msg.set_content(r["body"])
            if r["attachment"] and Path(r["attachment"]).exists():
                ctype = (mimetypes.guess_type(r["attachment"])[0] or "application/pdf").split("/")
                msg.add_attachment(Path(r["attachment"]).read_bytes(), maintype=ctype[0], subtype=ctype[1],
                                   filename=Path(r["attachment"]).name)
            try:
                smtp.send_message(msg)
                db.mark_email_sent(r["id"], Ledger(cfg.ledger_path))
                sent += 1
                log.info("emailed application to %s (%s)", r["to_addr"], r["company"])
            except Exception as e:  # noqa: BLE001
                db._x("UPDATE outbox SET status='failed', error=? WHERE id=?", (str(e)[:300], r["id"]))
                log.warning("email to %s failed: %s", r["to_addr"], e)
    return sent


async def run_once(cfg: Config, db: DB, *, do_discover=True, workers=None, limit=None) -> dict:
    luna, clef = Luna(cfg), Clef(cfg)
    try:
        db.requeue_stale()
        if do_discover:
            log.info("discovered %d new jobs", await discover(cfg, db))
        seen, kept = await triage_pending(cfg, db, clef)
        await retriage_missing(cfg, db, clef)
        up, down = rescore(cfg, db)  # pick up threshold changes in config.yaml
        log.info("triage: %d checked, %d queued; rescore +%d -%d", seen, kept, up, down)
        return await apply_queue(cfg, db, luna, clef, workers, limit)
    finally:
        await clef.aclose()


async def daemon(cfg: Config, db: DB, workers=None):
    """Discovery and applying run side by side: browsers never wait for a (slow) discovery pass."""
    interval = cfg.get("daemon.interval_minutes", 180) * 60
    luna, clef = Luna(cfg), Clef(cfg)
    if SHARD == 0:  # the other shards start after this, so nothing of theirs is in progress yet
        db.requeue_stale()

    async def triage_all():
        try:
            seen, kept = await triage_pending(cfg, db, clef)
            await retriage_missing(cfg, db, clef)
            up, down = rescore(cfg, db)
            log.info("triage: %d checked, %d queued; rescore +%d -%d | totals %s", seen, kept, up, down, db.counts())
        except Exception:  # noqa: BLE001
            log.exception("triage failed")

    async def discovery_loop():
        while True:
            try:
                log.info("discovery: %d new jobs", await discover(cfg, db))
            except Exception:  # noqa: BLE001
                log.exception("discovery failed")
            await triage_all()
            await asyncio.sleep(interval)

    async def outbox_loop():
        while True:
            try:
                await asyncio.to_thread(send_outbox, cfg, db)
            except Exception:  # noqa: BLE001
                log.exception("outbox send failed")
            await asyncio.sleep(60)

    async def browser_watchdog():
        """Agent Chromes must never exceed the worker count. If they do, kill orphans and say so loudly."""
        limit = (workers or cfg.get("apply.workers", 12)) + 2
        while True:
            await asyncio.sleep(60)
            n = await asyncio.to_thread(browsers.agent_browser_count)
            if n > limit:
                killed = await asyncio.to_thread(browsers.sweep_orphans)
                log.error("BROWSER LEAK: %d agent browsers running (limit %d); killed %d orphaned processes",
                          n, limit, killed)
            await asyncio.to_thread(browsers.prune_agent_tmp)

    async def apply_loop():
        while True:
            try:
                stats = await apply_queue(cfg, db, luna, clef, workers)
                log.info("apply pass done: %s | totals %s", stats, db.counts())
            except Exception:  # noqa: BLE001
                log.exception("apply pass failed")
            await asyncio.sleep(60)  # queue drained or all hosts capped: check again shortly

    browsers.install_shutdown_hooks()  # SIGTERM/SIGINT/exit: kill every browser this process launched
    log.info("daemon shard %d/%d up", SHARD, SHARDS)
    if SHARD:
        await asyncio.gather(apply_loop())
        return
    browsers.sweep_orphans()            # and any left behind by a previous run that died hard
    await triage_all()  # never apply to a job scored under older rules
    await asyncio.gather(discovery_loop(), apply_loop(), outbox_loop(), browser_watchdog())
