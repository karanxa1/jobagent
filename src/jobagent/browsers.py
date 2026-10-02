"""Make sure no agent Chrome outlives its application or the daemon.

Every browser this process launches is registered by PID. After each application the PID (and its whole
process tree) must be gone; if browser-use's own kill() left anything behind, it's force-killed. On daemon
shutdown (SIGTERM / SIGINT / normal exit) every registered browser is killed. `sweep_orphans()` also catches
agent Chromes left by a previous process that died without cleaning up (kill -9, crash, power loss).
"""
from __future__ import annotations

import atexit
import functools
import logging
import os
import re
import signal
from pathlib import Path

import psutil

from jobagent.config import ROOT

log = logging.getLogger(__name__)

# browser-use copies every profile into a temp dir with this prefix and launches Chrome on the copy
TEMP_PROFILE_PREFIX = "browser-use-user-data-dir-"
# The executable must be Chrome/Chromium itself: matching on substrings of the whole command line would also hit
# unrelated processes (e.g. a shell whose command line merely mentions the path). Matched against the lowercased
# file name with any .exe stripped: "Google Chrome", "chrome", "chromium-browser", "google-chrome-stable",
# "Google Chrome for Testing", "headless_shell" (but not chromedriver / chrome_crashpad_handler).
_CHROME_EXE = re.compile(r"^(google[ -])?(chrome|chromium)(?![\w])(.*)?$|^(chrome-)?headless[_-]shell$")
_FLAG_SPLIT = re.compile(r"\s+(?=--)")


def _norm(path) -> str:
    """Comparable form of a path on any OS: no quotes, forward slashes, no trailing slash, lowercase."""
    s = str(path).strip().strip("\"'").replace("\\", "/")
    s = re.sub(r"/+", "/", s)
    return s.rstrip("/").lower()


def _tokens(cmdline: list[str]) -> list[str]:
    """argv as psutil gives it. When a process rewrote its title (or the OS only gives one string), argv is a
    single string: split it before each ' --flag' so paths with spaces ("Program Files", "Google Chrome") survive."""
    out: list[str] = []
    for arg in cmdline:
        out.extend(t for t in _FLAG_SPLIT.split(arg) if t) if " --" in arg else out.append(arg)
    return out


def _exe_name(arg0: str) -> str:
    name = _norm(arg0).rsplit("/", 1)[-1]
    return name[:-4] if name.endswith(".exe") else name


@functools.lru_cache(maxsize=1)
def _profile_dirs() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(worker profile dirs, login profile dirs) of this install, normalised: the defaults under ROOT plus
    logins.worker_profiles / logins.profile_dir from the config."""
    workers, login = {_norm(ROOT / "browser_profiles" / "workers")}, {_norm(ROOT / "browser_profiles" / "login")}
    try:
        from jobagent.config import load_config

        cfg = load_config()
        workers.add(_norm(cfg.path("logins.worker_profiles", "browser_profiles/workers")))
        login.add(_norm(cfg.login_profile_dir))
    except Exception:  # noqa: BLE001
        pass
    return tuple(sorted(workers)), tuple(sorted(login))


def _under(path: str, dirs) -> bool:
    return any(path == d or path.startswith(d + "/") for d in dirs)


def _user_data_dirs(tokens: list[str]) -> list[str]:
    return [_norm(t.split("=", 1)[1]) for t in tokens if t.startswith("--user-data-dir=")]


def is_agent_chrome(cmdline: list[str] | None) -> bool:
    """A Chrome/Chromium *main* process whose --user-data-dir is a browser-use temp profile or one of this
    install's worker profiles. Never the user's own Chrome (no / another --user-data-dir) or the login profile."""
    if not cmdline:
        return False
    tokens = _tokens(cmdline)
    if not _CHROME_EXE.match(_exe_name(tokens[0])) or any(t.startswith("--type=") for t in tokens):
        return False  # not Chrome, or a renderer / GPU / utility child (dies with its main process)
    workers, login = _profile_dirs()
    for udd in _user_data_dirs(tokens):
        if _under(udd, login):  # the `jobagent login` window: the user's, never ours to kill
            return False
        # "/browser-use-user-data-dir-": a path component, so trailing junk (single-string argv) can't hide it
        if f"/{TEMP_PROFILE_PREFIX}" in f"/{udd}" or _under(udd, workers):
            return True
    return False


def uses_profile(cmdline: list[str] | None, profile_dir) -> bool:
    """A Chrome main process running on exactly this --user-data-dir (e.g. the `jobagent login` window)."""
    if not cmdline:
        return False
    tokens = _tokens(cmdline)
    targets = {_norm(profile_dir), _norm(Path(profile_dir).resolve())}
    return (bool(_CHROME_EXE.match(_exe_name(tokens[0]))) and not any(t.startswith("--type=") for t in tokens)
            and any(u in targets for u in _user_data_dirs(tokens)))


def is_daemon_cmdline(cmdline: list[str] | None) -> bool:
    """`jobagent run --daemon` in any form: the console script (jobagent / jobagent.exe / jobagent-script.py),
    `python -m jobagent.cli`, or a launcher in front of them (`uv run jobagent ...`, `caffeinate -i uv run ...`).
    argv elements only: a shell whose -c string merely mentions it does not count."""
    if not cmdline or "--daemon" not in cmdline or "run" not in cmdline:
        return False
    for arg in cmdline:
        name = _exe_name(arg)
        for suffix in (".py", "-script"):
            name = name.removesuffix(suffix)
        if name in ("jobagent", "jobagent.cli"):
            return True
    return False


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


def _is_daemon_proc(p: psutil.Process) -> bool:
    try:
        cmdline = p.cmdline()
    except (psutil.Error, OSError, SystemError):
        return False
    # the precise argv check, plus the old substring test (it also counts some false owners, which only ever
    # makes us kill less)
    cmd = " ".join(cmdline)
    return is_daemon_cmdline(cmdline) or ("jobagent" in cmd and "--daemon" in cmd)


def _owned_by_other_daemon(pid: int) -> bool:
    """True if this Chrome descends from a live `jobagent run --daemon` process other than this one: a sibling
    shard, or a daemon started from any other install directory. Those browsers are in use: never kill them."""
    try:
        for parent in psutil.Process(pid).parents():
            if parent.pid == os.getpid():
                return False
            if _is_daemon_proc(parent):
                return True
    except psutil.Error:
        pass
    return False


def orphan_agent_chrome_pids() -> list[int]:
    """Agent Chromes no live daemon owns (left by a crash / kill -9 / power loss). Safe to kill from any process
    that is not itself a daemon: browsers of running daemons (any install) are excluded."""
    return [pid for pid in _agent_chrome_pids() if not _owned_by_other_daemon(pid)]


def kill_orphans(dry_run: bool = False) -> list[int]:
    """Kill orphaned agent Chromes (see orphan_agent_chrome_pids); returns their main pids."""
    pids = orphan_agent_chrome_pids()
    if not dry_run:
        for pid in pids:
            kill_tree(pid, timeout=3)
    return pids


def live_daemon_pids() -> list[int]:
    """Every process running `jobagent run --daemon` (from any directory), launcher wrappers included."""
    out = []
    for p in psutil.process_iter():
        try:
            if p.pid != os.getpid() and is_daemon_cmdline(p.cmdline()):
                out.append(p.pid)
        except Exception:  # noqa: BLE001
            continue
    return out


def descendants(pid: int) -> set[int]:
    return {p.pid for p in _tree(pid)}


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
    """Kill every browser this process launched on exit or on a termination signal. Only signals this OS has:
    SIGHUP is POSIX-only; Windows delivers Ctrl+C / Ctrl+Break as SIGINT / SIGBREAK (a TerminateProcess, e.g.
    `jobagent stop` or Task Manager, runs no handler at all: stop kills the whole process tree instead)."""
    atexit.register(kill_all_registered)

    def handler(signum, _frame):
        kill_all_registered()
        if os.name == "nt":  # os.kill(own pid, SIGINT) would just TerminateProcess; exit the same way, logged
            logging.shutdown()
            os._exit(128 + signum)
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for name in ("SIGTERM", "SIGINT", "SIGHUP", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):  # not supported here / not the main thread
                pass
