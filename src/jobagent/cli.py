from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from jobagent.config import load_config
from jobagent.db import DB
from jobagent.ledger import Ledger
from jobagent.models import Job, Status

app = typer.Typer(add_completion=False, help="Find AI engineer roles and apply to them with parallel browser agents.")
con = Console()

LOGIN_SITES = {
    "linkedin": "https://www.linkedin.com/login",
    "naukri": "https://www.naukri.com/nlogin/login",
    "wellfound": "https://wellfound.com/login",
    "instahyre": "https://www.instahyre.com/login/",
    "yc": "https://account.ycombinator.com/?continue=https%3A%2F%2Fwww.workatastartup.com%2F",
    "hirist": "https://www.hirist.tech/login",
    "cutshort": "https://cutshort.io/login",
    # Google: lets the agent use "Sign in with Google" on employer portals (Tracxn etc.)
    "google": "https://accounts.google.com/ServiceLogin",
}


def _setup(verbose: bool = False):
    if con.is_terminal:
        logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(message)s",
                            handlers=[RichHandler(console=con, show_path=False, rich_tracebacks=True)])
    else:  # background daemon: one line per event so logs are greppable
        logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "azure", "browser_use", "cdp_use", "openai", "mcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    cfg = load_config()
    return cfg, DB(cfg.db_path)


@app.command()
def profile():
    """Generate profile.yaml from the resume (review it and fill the TODOs afterwards)."""
    from jobagent.profile import build_profile, todos

    cfg, _ = _setup()
    out = asyncio.run(build_profile(cfg))
    missing = todos(yaml.safe_load(out.read_text()))
    con.print(f"[green]wrote {out}[/]")
    if missing:
        con.print("[yellow]Fill these in before applying:[/] " + ", ".join(missing))


@app.command()
def login(sites: list[str] = typer.Argument(None, help=f"any of {list(LOGIN_SITES)} (default: all)"),
          export_only: bool = typer.Option(False, "--export-only",
                                           help="don't open Chrome: just export the cookies already in the login "
                                                "profile (e.g. the window was closed by something else)")):
    """Log in to job boards / Google once in your normal Chrome; cookies are shared with all parallel workers.

    The login window is plain Google Chrome with no automation attached (Google refuses sign-in in automated
    browsers). After you quit it, the cookies are exported to storage_state.json, which every worker loads."""
    import json as _json
    import time as _time

    from playwright.sync_api import sync_playwright

    from jobagent import browsers
    from jobagent.config import chrome_path
    from jobagent.osutil import IS_MAC

    import psutil

    cfg, _ = _setup()
    chosen = sites or list(LOGIN_SITES)
    if bad := [s for s in chosen if s not in LOGIN_SITES]:
        con.print(f"[red]unknown sites {bad}; choose from {list(LOGIN_SITES)}[/]")
        raise typer.Exit(1)
    prof = cfg.login_profile_dir
    chrome = chrome_path(cfg)
    if not chrome or not Path(chrome).exists():
        con.print(f"[red]Google Chrome not found{f' at {chrome}' if chrome else ''}. Install it, or set "
                  "apply.chrome_path in config.yaml / config.local.yaml.[/]")
        raise typer.Exit(1)

    def login_chrome_pids() -> list[int]:
        out = []
        for pr in psutil.process_iter():
            try:
                if browsers.uses_profile(pr.cmdline(), prof):
                    out.append(pr.pid)
            except Exception:  # noqa: BLE001
                continue
        return out

    if export_only:
        if not prof.exists():
            con.print(f"[red]{prof} does not exist: run `jobagent login` first.[/]")
            raise typer.Exit(1)
    else:
        prof.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen([chrome, f"--user-data-dir={prof}", "--no-first-run", "--no-default-browser-check",
                                 "--new-window", *[LOGIN_SITES[s] for s in chosen]],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        how = "quit that Chrome (Cmd+Q)" if IS_MAC else "close that Chrome window (every window of it)"
        con.print(f"A Chrome window opened with {', '.join(chosen)}. Log in on every tab with the email in "
                  f"profile.yaml\n(for Google: finish any 2-step prompt), then [bold]{how}[/].")
        while proc.poll() is None:
            _time.sleep(1)
    # the launched process may have handed off to a Chrome already running on this profile: wait for that too
    waited = 0
    while login_chrome_pids():
        if waited % 30 == 0:
            con.print("waiting for the login Chrome to exit..." if not export_only else
                      "[yellow]a Chrome is still running on the login profile; close it to export.[/]")
        _time.sleep(1)
        waited += 1
    _time.sleep(2)  # let Chrome flush its cookie database
    with sync_playwright() as p:
        # Playwright's default --use-mock-keychain (macOS) / --password-store=basic (Linux) make Chrome unable to
        # decrypt the real cookies, and Chrome then DELETES them; use the OS keychain so the login survives the
        # export (Windows uses DPAPI for the same user either way)
        ctx = p.chromium.launch_persistent_context(str(prof), headless=True, executable_path=chrome,
                                                   ignore_default_args=["--use-mock-keychain", "--password-store=basic"])
        state = ctx.storage_state()
        ctx.close()
    if not state.get("cookies"):
        con.print("[red]No cookies were saved: nothing to export (did you log in in that Chrome window?). "
                  "Existing sessions left untouched.[/]")
        raise typer.Exit(1)
    cfg.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.storage_state_path.write_text(_json.dumps(state), encoding="utf-8")
    try:
        cfg.storage_state_path.chmod(0o600)  # POSIX: owner-only; Windows: only clears read-only (harmless)
    except OSError:
        pass
    done_file = cfg.storage_state_path.with_name("logged_in_sites.json")
    done = set(_json.loads(done_file.read_text(encoding="utf-8"))) if done_file.exists() else set()
    done_file.write_text(_json.dumps(sorted(done | set(chosen))), encoding="utf-8")
    n = len(state.get("cookies", []))
    con.print(f"[green]saved {n} cookies -> {cfg.storage_state_path}; logged in: {sorted(done | set(chosen))}[/]\n"
              "Restart the daemon (jobagent stop, then jobagent start) so workers pick them up.")


@app.command("gmail-auth")
def gmail_auth():
    """One-time OAuth for the Gmail MCP server used for OTP codes."""
    cfg, _ = _setup()
    c = cfg.get("otp.mcp", {})
    keys = Path.home() / ".gmail-mcp" / "gcp-oauth.keys.json"
    if not keys.exists():
        con.print(f"[red]Put your Google OAuth client JSON (Desktop app, Gmail API enabled) at {keys} first.[/]")
        raise typer.Exit(1)
    from jobagent.osutil import resolve_exe

    subprocess.run([resolve_exe(c.get("command", "npx")), *c.get("args", []), "auth"], check=False)


@app.command("gmail-login")
def gmail_login(check: bool = typer.Option(False, "--check", help="don't open Chrome; only test the saved session")):
    """Sign in to Gmail once in plain Chrome, for otp.provider gmail_web / `jobagent otp-relay`.

    Uses its own profile (otp.gmail_web.profile_dir), never exported to the workers' cookie file: the session only
    ever lives in this one browser profile. Afterwards a headless Chrome on the same profile reads codes from it."""
    import time as _time

    from jobagent.config import chrome_path
    from jobagent.gmail_web import GmailWebOTP, profile_in_use
    from jobagent.osutil import IS_MAC
    from jobagent.otp import gmail_profile_dir

    cfg, _ = _setup()
    prof = gmail_profile_dir(cfg)
    chrome = chrome_path(cfg)
    if not chrome or not Path(chrome).exists():
        con.print(f"[red]Google Chrome not found{f' at {chrome}' if chrome else ''}. Install it, or set "
                  "apply.chrome_path in config.yaml / config.local.yaml.[/]")
        raise typer.Exit(1)
    if profile_in_use(prof):
        con.print(f"[red]{prof} is in use by another Chrome: a running daemon / `jobagent otp-relay` holds the Gmail "
                  "session (it lets go by itself within ~3 minutes once the session has expired), or a gmail-login "
                  "window is still open. Stop it or close that window, then run this again.[/]")
        raise typer.Exit(1)
    if not check:
        prof.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen([chrome, f"--user-data-dir={prof}", "--no-first-run", "--no-default-browser-check",
                                 "--new-window", "https://mail.google.com/mail/u/0/"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        how = "quit that Chrome (Cmd+Q)" if IS_MAC else "close that Chrome window (every window of it)"
        con.print("A Chrome window opened at Gmail. Sign in with the address the applications use (finish any "
                  f"2-step prompt) and wait for your inbox to show, then [bold]{how}[/].")
        while proc.poll() is None:
            _time.sleep(1)
        while profile_in_use(prof):  # it may have handed off to a Chrome already running on this profile
            _time.sleep(1)
        _time.sleep(2)  # let Chrome flush its cookie database

    async def probe():
        otp = GmailWebOTP(cfg, None)
        if not await otp._connect():
            return None
        try:
            feed = await otp.gw.feed()
            ok_full = bool(feed) and bool(await otp.gw.message(feed[0]["id"]))
            return len(feed), ok_full, bool(otp.gw.ik)
        finally:
            await otp.gw.stop()

    res = asyncio.run(probe())
    if res is None:
        con.print("[red]Gmail is not signed in on that profile. Run `jobagent gmail-login` and sign in.[/]")
        raise typer.Exit(1)
    n, ok_full, ik = res
    con.print(f"[green]Gmail session OK[/]: {n} unread in the inbox feed"
              + (f"; full message read {'OK' if ok_full else '[yellow]FAILED[/] (codes in subjects/previews still work)'}"
                 if n else "") + ("" if ik else " [yellow](no account key found; search may be limited)[/]")
              + "\nSet otp.provider: gmail_web in config.yaml (or run `jobagent otp-relay` next to a bridge daemon).")


@app.command("otp-relay")
def otp_relay(via: str = typer.Option("gmail_web", "--via", help="gmail_web (your Gmail browser session) or imap "
                                                                 "(GMAIL_APP_PASSWORD)"),
              once: bool = typer.Option(False, "--once", help="answer what is pending now, then exit"),
              wait: int = typer.Option(120, "--wait", help="seconds to wait for each email before answering failed"),
              db_path: str = typer.Option(None, "--db", help="jobs.db to serve (default: config db)")):
    """Keep Gmail open and answer verification-code requests from the bridge queue (otp.provider: bridge).

    Any number of daemons / processes on this machine using `otp.provider: bridge` and the same jobs.db then share
    one Gmail session, like the Claude Code helper in docs/claude-helpers.md, but without Claude."""
    import os as _os

    from jobagent.llm import Luna, make_decider
    from jobagent.otp import IMAPGmail, serve_bridge

    cfg, _ = _setup()
    if db_path:
        cfg.raw["db"] = str(Path(db_path).expanduser().resolve())
    if via not in ("gmail_web", "imap"):
        con.print("[red]--via must be gmail_web or imap[/]")
        raise typer.Exit(1)
    if via == "imap" and not _os.environ.get("GMAIL_APP_PASSWORD"):
        con.print("[red]--via imap needs GMAIL_APP_PASSWORD in .env (and otp.imap.user in config).[/]")
        raise typer.Exit(1)

    async def go():
        from jobagent.gmail_web import GmailWebOTP

        luna = Luna(cfg)
        clef = make_decider(cfg, luna)
        provider = GmailWebOTP(cfg, luna, clef) if via == "gmail_web" else IMAPGmail(cfg, luna, clef)
        await provider.start()
        logging.getLogger(__name__).info("otp-relay up: %s, db %s", via, cfg.db_path)
        try:
            return await serve_bridge(cfg, provider, once=once, wait_seconds=wait)
        finally:
            await provider.stop()
            await clef.aclose()

    stats = asyncio.run(go())
    con.print(stats)


@app.command()
def discover(source: list[str] = typer.Option(None, "--source", "-s")):
    """Fetch jobs from every source into the local DB."""
    from jobagent.orchestrator import discover as _discover

    cfg, db = _setup()
    con.print(f"[green]{asyncio.run(_discover(cfg, db, source))} new jobs[/]")


@app.command()
def triage():
    """Score new jobs with clef-flash and queue the good ones."""
    from jobagent.llm import make_decider
    from jobagent.triage import triage_pending

    cfg, db = _setup()

    async def go():
        clef = make_decider(cfg)
        try:
            return await triage_pending(cfg, db, clef)
        finally:
            await clef.aclose()

    seen, kept = asyncio.run(go())
    con.print(f"triaged {seen}, queued {kept}")

    async def go2():
        from jobagent.triage import retriage_missing
        clef = make_decider(cfg)
        try:
            return await retriage_missing(cfg, db, clef)
        finally:
            await clef.aclose()

    con.print(f"re-checked {asyncio.run(go2())} queued jobs for AI-first role type")


@app.command("rescore")
def rescore_cmd():
    """Re-apply config.yaml triage thresholds to already-scored jobs (no model calls)."""
    from jobagent.triage import rescore

    cfg, db = _setup()
    up, down = rescore(cfg, db)
    con.print(f"requeued {up}, dropped {down}")


@app.command()
def apply(workers: int = typer.Option(None, "--workers", "-w"), limit: int = typer.Option(None, "--limit", "-n"),
          dry_run: bool = typer.Option(False, "--dry-run", help="fill forms but don't submit"),
          headful: bool = typer.Option(False, "--headful", help="show the browsers")):
    """Apply to queued jobs with parallel browsers."""
    from jobagent.llm import Luna, make_decider
    from jobagent.orchestrator import apply_queue

    cfg, db = _setup()
    if dry_run:
        cfg.raw.setdefault("apply", {})["submit"] = False
    if headful:
        cfg.raw.setdefault("apply", {})["headless"] = False
    db.requeue_stale()

    async def go():
        clef = make_decider(cfg)
        try:
            return await apply_queue(cfg, db, Luna(cfg), clef, workers, limit)
        finally:
            await clef.aclose()

    con.print(asyncio.run(go()))


@app.command("apply-url")
def apply_url(url: str, company: str = typer.Option(..., "--company", "-c"), title: str = typer.Option("AI Engineer", "--title", "-t"),
              dry_run: bool = typer.Option(True, "--dry-run/--submit"), headful: bool = typer.Option(True, "--headful/--headless")):
    """Apply to one specific posting (defaults to a visible, dry run: good for testing)."""
    from jobagent.applier import Applier
    from jobagent.config import load_profile
    from jobagent.llm import Luna, make_decider
    from jobagent.otp import make_otp_provider
    from jobagent.profile import resume_text

    cfg, db = _setup()
    cfg.raw.setdefault("apply", {}).update(submit=not dry_run, headless=not headful)
    job = Job(source="manual", external_id=url, company=company, title=title, url=url)
    if Ledger(cfg.ledger_path).has(job):
        con.print(f"[yellow]Already in {cfg.ledger_path.name}; not applying again.[/]")
        raise typer.Exit(0)
    db.upsert_jobs([job])

    async def go():
        luna, clef = Luna(cfg), make_decider(cfg)
        otp = make_otp_provider(cfg, luna, clef)
        await otp.start()
        try:
            res = await Applier(cfg, luna, clef, otp, load_profile(cfg), resume_text(cfg.resume_path)).apply(job)
        finally:
            await otp.stop()
            await clef.aclose()
        db.finish(job.key, res.status, {"summary": res.summary, "screenshot": res.screenshot, **res.answers})
        if res.status == Status.APPLIED:
            Ledger(cfg.ledger_path).record(job, res.summary[:120])
        return res

    res = asyncio.run(go())
    con.print(f"[bold]{res.status}[/] {res.summary}\nscreenshot: {res.screenshot}")


@app.command()
def run(daemon: bool = typer.Option(False, "--daemon", help="loop forever on daemon.interval_minutes"),
        workers: int = typer.Option(None, "--workers", "-w"), no_discover: bool = False):
    """Full pipeline: discover -> triage -> apply (once, or forever with --daemon)."""
    from jobagent import orchestrator

    cfg, db = _setup()
    if daemon:
        asyncio.run(orchestrator.daemon(cfg, db, workers))
    else:
        con.print(asyncio.run(orchestrator.run_once(cfg, db, do_discover=not no_discover, workers=workers)))


@app.command()
def status(show: str = typer.Option(None, "--show", help="list jobs in this status, e.g. needs_human, applied")):
    """Counts per status, or the jobs in one status."""
    cfg, db = _setup()
    if not show:
        t = Table("status", "jobs")
        for k, v in sorted(db.counts().items(), key=lambda kv: -kv[1]):
            t.add_row(k, str(v))
        con.print(t)
        return
    t = Table("company", "title", "summary", "apply url", show_lines=True)
    for r in db.by_status(show, 100):
        t.add_row(r["company"], r["title"], (json.loads(r["result"] or "{}").get("summary") or "")[:160], r["apply_url"])
    con.print(t)


@app.command()
def retry(statuses: list[str] = typer.Argument(None, help="default: failed needs_human")):
    """Put failed / needs_human jobs back in the queue (after you fix logins or answer questions)."""
    cfg, db = _setup()
    n = db.requeue(statuses or [Status.FAILED, Status.NEEDS_HUMAN], cfg.get("apply.max_attempts", 2) + 1)
    con.print(f"requeued {n}")


@app.command()
def assist(limit: int = typer.Option(20, "--limit", "-n")):
    """Re-run captcha-blocked applications in a visible Chrome; you solve the captcha, the agent does the rest."""
    from jobagent.llm import Luna, make_decider
    from jobagent.orchestrator import apply_queue

    cfg, db = _setup()
    rows = [r for r in db.by_status(Status.NEEDS_HUMAN, 1000)
            if re.search(r"captcha|cloudflare|verif|bot[- ]?check|human (verification|check)|challenge|unusual activity",
                         json.loads(r["result"] or "{}").get("summary") or "", re.I)][:limit]
    if not rows:
        con.print("no captcha-blocked applications")
        return
    for r in rows:
        db._x("UPDATE jobs SET status=?, score=COALESCE(score,0)+100 WHERE key=?", (Status.QUEUED, r["key"]))
    cfg.raw.setdefault("apply", {}).update(headless=False, workers=1, assist=True)
    con.print(f"[bold]{len(rows)} captcha jobs.[/] A Chrome window opens for each; solve the captcha when you hear the chime.")

    async def go():
        clef = make_decider(cfg)
        try:
            return await apply_queue(cfg, db, Luna(cfg), clef, 1, len(rows))
        finally:
            await clef.aclose()

    con.print(asyncio.run(go()))


@app.command()
def manual(limit: int = typer.Option(100, "--limit", "-n"), letters: bool = typer.Option(True, "--letters/--no-letters")):
    """Write MANUAL_APPLY.md: best queued jobs on sites that block automation, with a tailored cover letter each."""
    from jobagent.applier import Applier
    from jobagent.config import load_profile
    from jobagent.llm import Luna, make_decider
    from jobagent.otp import NoOTP
    from jobagent.profile import resume_text

    cfg, db = _setup()
    hosts = set(cfg.get("apply.manual_hosts", []) or [])
    ledger = Ledger(cfg.ledger_path)
    rows = [r for r in db.jobs_with_status(Status.QUEUED) if r["host"] in hosts]
    rows = [r for r in rows if not ledger.has(Job(**json.loads(r["data"])))][:limit]
    out = cfg.root / "MANUAL_APPLY.md"

    async def go():
        luna, clef = Luna(cfg), make_decider(cfg)
        ap = Applier(cfg, luna, clef, NoOTP(cfg, luna), load_profile(cfg), resume_text(cfg.resume_path))
        sem = asyncio.Semaphore(16)

        async def one(r):
            job = Job(**json.loads(r["data"]))
            if not letters:
                return job, ""
            async with sem:
                return job, await ap._cover_letter(job)

        try:
            return await asyncio.gather(*(one(r) for r in rows))
        finally:
            await clef.aclose()

    items = asyncio.run(go())
    lines = ["# Apply manually", "",
             "These sites reject automated submissions, so apply from your normal browser. Upload "
             f"`{cfg.resume_path.name}`. When done, add the job to APPLIED_JOBS.md (or run `jobagent mark-applied <url>`).", ""]
    for i, (job, letter) in enumerate(items, 1):
        lines += [f"## {i}. {job.company}: {job.title}", f"{job.location}  ", f"**Apply:** {job.apply_url}", ""]
        if letter:
            lines += ["<details><summary>Cover letter</summary>", "", letter, "", "</details>", ""]
    out.write_text("\n".join(lines))
    con.print(f"[green]wrote {out} ({len(items)} jobs)[/]")


@app.command("mark-applied")
def mark_applied(url: str):
    """Record a job you applied to by hand (adds it to APPLIED_JOBS.md and the DB)."""
    from jobagent.ledger import normalize_url

    cfg, db = _setup()
    rows = [r for r in db._x("SELECT * FROM jobs").fetchall()
            if normalize_url(r["apply_url"]) == normalize_url(url) or normalize_url(r["url"]) == normalize_url(url)]
    if not rows:
        con.print("[red]job not found in DB[/]")
        raise typer.Exit(1)
    job = Job(**json.loads(rows[0]["data"]))
    Ledger(cfg.ledger_path).record(job, "applied manually")
    db.finish(job.key, Status.APPLIED, {"summary": "applied manually"})
    con.print(f"[green]recorded {job.company}: {job.title}[/]")


@app.command()
def inbox(days: int = 7):
    """Classify recruiter replies (interview / assessment / rejection) and attach them to applied jobs."""
    from jobagent.inbox import scan
    from jobagent.llm import Luna, make_decider
    from jobagent.otp import make_otp_provider

    cfg, db = _setup()

    async def go():
        luna, clef = Luna(cfg), make_decider(cfg)
        otp = make_otp_provider(cfg, luna, clef)
        await otp.start()
        try:
            return await scan(db, clef, otp, days)
        finally:
            await otp.stop()
            await clef.aclose()

    t = Table("kind", "company", "subject", "from")
    for r in sorted(asyncio.run(go()), key=lambda r: r["kind"]):
        t.add_row(r["kind"], r["company"], r["subject"][:70], r["from"][:40])
    con.print(t)


@app.command("bridge-watch")
def bridge_watch(poll: float = 2.0, send_emails: bool = True):
    """Print one JSON line per new inbox request (for a helper with Gmail access to fulfill)."""
    import time as _t

    cfg, db = _setup()
    seen: set[int] = set()
    while True:
        for r in db._x("SELECT * FROM mail_requests WHERE status='pending' ORDER BY id").fetchall():
            if r["id"] not in seen:
                seen.add(r["id"])
                print(json.dumps({"id": r["id"], "kind": r["kind"], "company": r["company"], "hint": r["sender_hint"],
                                  "want": r["want"], "requested_at": r["requested_at"]}), flush=True)
        if send_emails:
            for r in db._x("SELECT id, company, to_addr FROM outbox WHERE status='pending' ORDER BY id").fetchall():
                if -r["id"] not in seen:
                    seen.add(-r["id"])
                    print(json.dumps({"outbox_id": r["id"], "kind": "send_email", "company": r["company"],
                                      "to": r["to_addr"]}), flush=True)
        _t.sleep(poll)


@app.command("bridge-wait")
def bridge_wait(timeout: int = 900):
    """Block until inbox requests are pending, print them as JSON lines, exit. (For a helper agent's loop.)"""
    import time as _t

    cfg, db = _setup()
    deadline = _t.monotonic() + timeout
    while _t.monotonic() < deadline:
        rows = db._x("SELECT * FROM mail_requests WHERE status='pending' ORDER BY id").fetchall()
        if rows:
            for r in rows:
                print(json.dumps({"id": r["id"], "company": r["company"], "hint": r["sender_hint"], "want": r["want"],
                                  "requested_at_utc": __import__("datetime").datetime.utcfromtimestamp(
                                      r["requested_at"]).strftime("%Y-%m-%dT%H:%M:%SZ")}), flush=True)
            return
        _t.sleep(2)
    print("NO_REQUESTS")


@app.command("bridge-answer")
def bridge_answer(request_id: int, value: str = typer.Argument(""), fail: bool = False):
    """Answer an inbox request: the code / link, or --fail if no matching email."""
    cfg, db = _setup()
    db._x("UPDATE mail_requests SET status=?, result=?, updated_at=datetime('now') WHERE id=?",
          ("failed" if fail or not value else "done", value, request_id))
    con.print("ok")


@app.command()
def outbox(show_id: int = typer.Argument(0)):
    """List pending application emails, or print one in full (for sending)."""
    cfg, db = _setup()
    if show_id:
        r = db._x("SELECT * FROM outbox WHERE id=?", (show_id,)).fetchone()
        print(json.dumps(dict(r), indent=1))
        return
    t = Table("id", "company", "to", "subject", "status")
    for r in db._x("SELECT * FROM outbox ORDER BY id DESC LIMIT 50").fetchall():
        t.add_row(str(r["id"]), r["company"], r["to_addr"], r["subject"], r["status"])
    con.print(t)


@app.command("outbox-next")
def outbox_next(timeout: int = 540):
    """Block until a pending application email exists; print it as one JSON line (for the email subagent)."""
    import time as _t

    cfg, db = _setup()
    deadline = _t.monotonic() + timeout
    while _t.monotonic() < deadline:
        r = db._x("SELECT * FROM outbox WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
        if r:
            db._x("UPDATE outbox SET status='sending' WHERE id=?", (r["id"],))
            print(json.dumps({"id": r["id"], "company": r["company"], "to": r["to_addr"], "subject": r["subject"],
                              "body": r["body"], "resume_url": cfg.get("resume_url", "")}))
            return
        _t.sleep(3)
    print("NO_EMAILS")


@app.command("outbox-failed")
def outbox_failed(outbox_id: int, error: str = "send failed"):
    """Put an email back as failed (it won't be retried automatically)."""
    cfg, db = _setup()
    db._x("UPDATE outbox SET status='failed', error=? WHERE id=?", (error, outbox_id))
    con.print("ok")


@app.command("outbox-sent")
def outbox_sent(outbox_id: int):
    """Mark an outbox email as sent (job -> applied, added to APPLIED_JOBS.md)."""
    cfg, db = _setup()
    db.mark_email_sent(outbox_id, Ledger(cfg.ledger_path))
    con.print("ok")


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def start(ctx: typer.Context,
          processes: int = typer.Option(None, "--processes", "-p", help="daemon shards (default: apply.processes)"),
          stagger: float = typer.Option(20, help="seconds between shard 0 and the others (shard 0 requeues stale jobs)")):
    """Run the daemon in the background (macOS / Linux / Windows). Extra args go to `jobagent run --daemon`."""
    from jobagent import daemonctl
    from jobagent.osutil import IS_WIN

    cfg = load_config()
    res = daemonctl.start(cfg, list(ctx.args), stagger_s=stagger, processes=processes, echo=con.print)
    if res.already:
        con.print(f"already running (pid {', '.join(map(str, res.already))})")
        return
    log = daemonctl.log_file(cfg)
    if res.died:
        con.print(f"[red]daemon(s) {res.died} exited right away; last lines of {log}:[/]")
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()[-25:] if log.exists() else []
        con.print("\n".join(lines), markup=False, highlight=False)
        raise typer.Exit(1)
    follow = f"Get-Content '{log}' -Wait -Tail 50" if IS_WIN else f"tail -f '{log}'"
    con.print(f"started {len(res.pids)} daemon(s): {' '.join(map(str, res.pids))}; follow with: {follow}")


@app.command()
def stop(dry_run: bool = typer.Option(False, "--dry-run", help="only show what would be killed")):
    """Stop every daemon shard (whole process trees) and every orphaned agent Chrome. Your own Chrome, the login
    window and the browsers of daemons still running elsewhere are never touched."""
    from jobagent import daemonctl

    cfg = load_config()
    res = daemonctl.stop(cfg, echo=con.print, dry_run=dry_run)
    if not res.stopped and not dry_run:
        con.print("no daemon was running")
    if not dry_run:
        con.print(f"killed {len(res.orphans_killed)} orphaned agent browser(s)")


@app.command("daemon-status")
def daemon_status():
    """Is the background daemon running, and how many agent browsers are alive."""
    from jobagent import daemonctl

    st = daemonctl.status(load_config())
    if not st["pids"]:
        con.print(f"not running (no {st['pid_file']})" if not st["untracked_here"] else "pid file missing")
    for pid, alive in st["pids"].items():
        con.print(f"daemon pid {pid}: {'[green]running[/]' if alive else '[red]not running[/]'}")
    if st["untracked_here"]:
        con.print(f"[yellow]daemon(s) running from this install but not in the pid file: {st['untracked_here']}[/]")
    con.print(f"agent browsers: {st['agent_browsers_total']} on this machine, {st['agent_browsers_ours']} owned by "
              f"this install's daemon(s), {st['agent_browsers_orphaned']} orphaned (no live daemon)")
    for pid, cwd in st["other_daemons"]:
        con.print(f"other jobagent daemon: pid {pid} in {cwd}")
    con.print(f"log: {st['log_file']}")


def main():
    import os
    import sys

    if os.name == "nt" and not sys.flags.utf8_mode:
        # Windows defaults file I/O to the ANSI code page (cp1252...): non-ASCII names, cover letters and ledger
        # lines would crash or garble. Re-run in UTF-8 mode; Ctrl+C reaches the child directly (same console).
        import signal

        child = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "jobagent.cli", *sys.argv[1:]],
                                 env={**os.environ, "PYTHONUTF8": "1"})
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        sys.exit(child.wait())
    app()


if __name__ == "__main__":
    main()
