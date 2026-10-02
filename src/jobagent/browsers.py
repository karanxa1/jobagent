"""Make sure no agent Chrome outlives its application or the daemon.

Every browser this process launches is registered by PID. After each application the PID (and its whole
process tree) must be gone; if browser-use's own kill() left anything behind, it's force-killed. On daemon
shutdown (SIGTERM / SIGINT / normal exit) every registered browser is killed. `sweep_orphans()` also catches
agent Chromes left by a previous process that died without cleaning up (kill -9, crash, power loss).
"""
from __future__ import annotations

import atexit
import logging
import os
import re
import signal

import psutil

log = logging.getLogger(__name__)

# Only a Chrome/Chromium executable launched with an agent profile dir counts. Matching on plain substrings
# would also hit unrelated processes (e.g. a shell whose command line merely mentions the path).
AGENT_CHROME = re.compile(
    r"^\S*(Google Chrome|Chromium|chrome|headless_shell)\b.*--user-data-dir=\S*"
    r"(browser-use-user-data-dir-|/jobs/browser_profiles/workers/)")


def is_agent_chrome(cmdline: list[str] | None) -> bool:
    if not cmdline:
        return False
    cmd = " ".join(cmdline)
    return bool(AGENT_CHROME.search(cmd)) and "--type=" not in cmd  # main browser process only


_live: dict[int, str] = {}  # chrome pid -> job key


def _tree(pid: int) -> list[psutil.Process]:
    try:
        p = psutil.Process(pid)
        return [p, *p.children(recursive=True)]
    except psutil.Error:
        return []


def kill_tree(pid: int, timeout: float = 5) -> int:
    procs = _tree(pid)
    for p in procs:
        try:
            p.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(procs, timeout=timeout)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
    return len(procs)


def session_pid(session) -> int | None:
    wd = getattr(session, "_local_browser_watchdog", None)
    try:
        return wd.browser_pid if wd else None
    except Exception:  # noqa: BLE001
        return None


def register(session, job_key: str) -> int | None:
    pid = session_pid(session)
    if pid:
        _live[pid] = job_key
    return pid


def ensure_dead(pid: int | None, job_key: str = "") -> None:
    """Called after session.kill(): anything still alive is a leak, so kill it and say so."""
    if not pid:
        return
    _live.pop(pid, None)
    if psutil.pid_exists(pid):
        n = kill_tree(pid)
        log.warning("browser for %s survived session.kill(); force-killed %d processes", job_key, n)


def kill_all_registered() -> None:
    for pid, key in list(_live.items()):
        kill_tree(pid, timeout=3)
        _live.pop(pid, None)


def _agent_chrome_pids() -> list[int]:
    """PIDs of agent Chrome main processes. Some system processes refuse cmdline access (AccessDenied or a raw
    SystemError on macOS); skip those instead of aborting the scan."""
    out = []
    for pid in psutil.pids():
        try:
            if is_agent_chrome(psutil.Process(pid).cmdline()):
                out.append(pid)
        except Exception:  # noqa: BLE001
            continue
    return out


def _owned_by_other_daemon(pid: int) -> bool:
    """True if this Chrome descends from another live `jobagent run --daemon` process (a sibling shard)."""
    try:
        for parent in psutil.Process(pid).parents():
            if parent.pid == os.getpid():
                return False
            try:
                cmd = " ".join(parent.cmdline())
            except psutil.Error:
                continue
            if "jobagent" in cmd and "--daemon" in cmd:
                return True
    except psutil.Error:
        pass
    return False


def sweep_orphans() -> int:
    """Kill agent Chromes not owned by this process (left over from a dead run). Never touches the user's Chrome."""
    mine = {c.pid for pid in _live for c in _tree(pid)}
    killed = sum(kill_tree(pid, timeout=3) for pid in _agent_chrome_pids()
                 if pid not in mine and not _owned_by_other_daemon(pid))
    if killed:
        log.warning("killed %d orphaned agent browser processes", killed)
    return killed


def prune_agent_tmp(max_age_s: float = 7200) -> None:
    """browser-use keeps per-step screenshots in $TMPDIR/browser_use_agent_*; drop the ones from finished runs."""
    import shutil
    import tempfile
    import time
    from pathlib import Path

    cutoff = time.time() - max_age_s
    for d in Path(tempfile.gettempdir()).glob("browser_use_agent_*"):
        try:
            if d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            continue


def agent_browser_count() -> int:
    return len(_agent_chrome_pids())


def install_shutdown_hooks() -> None:
    atexit.register(kill_all_registered)

    def handler(signum, _frame):
        kill_all_registered()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, handler)
