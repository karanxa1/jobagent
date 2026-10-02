"""Start / stop / inspect the background daemon the same way on macOS, Linux and Windows.

`start` spawns apply.processes detached `jobagent run --daemon` shards (JOBAGENT_SHARD / JOBAGENT_SHARDS env),
appends their output to logs/agent.log and writes one pid per line to logs/agent.pid. `stop` kills every pid in
agent.pid with its whole process tree, then any agent Chrome no live daemon owns. Browsers of daemons that are
still running (sibling shards, or another install's daemon) are never touched.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from jobagent import browsers
from jobagent.config import Config
from jobagent.osutil import IS_WIN, detached_popen_kwargs


def logs_dir(cfg: Config) -> Path:
    return cfg.root / "logs"


def pid_file(cfg: Config) -> Path:
    return logs_dir(cfg) / "agent.pid"


def log_file(cfg: Config) -> Path:
    return logs_dir(cfg) / "agent.log"


def read_pids(cfg: Config) -> list[int]:
    f = pid_file(cfg)
    if not f.exists():
        return []
    out = []
    for line in f.read_text(encoding="utf-8", errors="replace").split():
        if line.strip().isdigit():
            out.append(int(line))
    return out


def _cwd(p: psutil.Process) -> Path | None:
    try:
        return Path(p.cwd()).resolve()
    except (psutil.Error, OSError):
        return None


def _is_our_daemon(pid: int, cfg: Config) -> bool:
    """pid is alive, is a jobagent daemon (or its launcher), and is not running from another install: guards
    against a stale agent.pid whose pid the OS has since given to an unrelated process (common on Windows)."""
    try:
        p = psutil.Process(pid)
        if not browsers._is_daemon_proc(p):
            return False
        cwd = _cwd(p)
        return cwd is None or cwd == cfg.root.resolve()
    except psutil.Error:
        return False


def running_pids(cfg: Config) -> list[int]:
    return [pid for pid in read_pids(cfg) if _is_our_daemon(pid, cfg)]


def _parent_is_daemon(p: psutil.Process) -> bool:
    """A launcher wrapper's child (uv run -> python): count the top-most daemon process only."""
    try:
        parent = p.parent()
        return bool(parent) and browsers.is_daemon_cmdline(parent.cmdline())
    except (psutil.Error, OSError, SystemError):  # e.g. pid 1 / system processes refuse cmdline access
        return False


def _daemons_here(cfg: Config) -> list[int]:
    """Top-level live daemons started from this install (cwd = install dir), even if agent.pid lost track of
    them. Launcher wrappers count once: a daemon whose parent is also a daemon command line is skipped."""
    root, out = cfg.root.resolve(), []
    for pid in browsers.live_daemon_pids():
        try:
            p = psutil.Process(pid)
            if _cwd(p) == root and not _parent_is_daemon(p):
                out.append(pid)
        except psutil.Error:
            continue
    return out


def _spawn(cmd: list[str], env: dict, cwd: Path, log) -> subprocess.Popen:
    kw = detached_popen_kwargs()
    common = dict(cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, close_fds=True)
    if IS_WIN:
        # `uv run` / console-script launchers may put us in a Job Object that is killed when they exit: break away
        # from it if the job allows that, otherwise start normally.
        try:
            return subprocess.Popen(cmd, **common, creationflags=kw["creationflags"] | subprocess.CREATE_BREAKAWAY_FROM_JOB)
        except OSError:
            pass
    return subprocess.Popen(cmd, **common, **kw)


def daemon_command(extra_args: list[str] | None = None) -> list[str]:
    """How to run `jobagent run --daemon` with this interpreter. POSIX: via the venv's console script, so it shows
    up as `.../jobagent run --daemon` (what `pgrep -f "jobagent run --daemon"` finds). Windows: `python -m
    jobagent.cli` (the jobagent.exe launcher would add a second process in front of Python)."""
    script = Path(sys.executable).with_name("jobagent")
    base = [sys.executable, str(script)] if not IS_WIN and script.is_file() else [sys.executable, "-m", "jobagent.cli"]
    return [*base, "run", "--daemon", *(extra_args or [])]


@dataclass
class StartResult:
    pids: list[int] = field(default_factory=list)
    already: list[int] = field(default_factory=list)
    orphans_killed: list[int] = field(default_factory=list)
    died: list[int] = field(default_factory=list)


def start(cfg: Config, extra_args: list[str] | None = None, stagger_s: float = 20, processes: int | None = None,
          echo=print) -> StartResult:
    res = StartResult()
    if already := running_pids(cfg) or _daemons_here(cfg):
        res.already = already
        return res
    logs_dir(cfg).mkdir(parents=True, exist_ok=True)
    # agent browsers orphaned by a crash or a hard kill of an earlier run (never those of a live daemon)
    res.orphans_killed = browsers.kill_orphans()
    if res.orphans_killed:
        echo(f"killed {len(res.orphans_killed)} orphaned agent browser(s)")
    n = max(1, int(processes or cfg.get("apply.processes", 1) or 1))
    cmd = daemon_command(extra_args)
    pf = pid_file(cfg)
    pf.write_text("", encoding="utf-8")
    for i in range(n):
        env = {**os.environ, "JOBAGENT_SHARD": str(i), "JOBAGENT_SHARDS": str(n),
               "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"}
        with open(log_file(cfg), "ab") as log:
            proc = _spawn(cmd, env, cfg.root, log)
        res.pids.append(proc.pid)
        with open(pf, "a", encoding="utf-8") as f:
            f.write(f"{proc.pid}\n")
        echo(f"shard {i}/{n}: pid {proc.pid}")
        # shard 0 requeues jobs a dead run left in progress: let it do that before the others start claiming
        if i == 0 and n > 1:
            echo(f"waiting {stagger_s:.0f} s for shard 0 to requeue stale jobs before starting the others")
            time.sleep(stagger_s)
    time.sleep(2)
    res.died = [pid for pid in res.pids if not psutil.pid_exists(pid)
                or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE]
    return res


@dataclass
class StopResult:
    stopped: list[int] = field(default_factory=list)
    stale: list[int] = field(default_factory=list)
    processes_killed: int = 0
    orphans_killed: list[int] = field(default_factory=list)


def stop(cfg: Config, timeout: float = 10, echo=print, dry_run: bool = False) -> StopResult:
    res = StopResult()
    tracked = read_pids(cfg)
    untracked = [pid for pid in _daemons_here(cfg) if pid not in tracked
                 and not any(pid in browsers.descendants(t) for t in tracked)]
    for pid in untracked:
        echo(f"also stopping daemon {pid}: started from {cfg.root} but not in {pid_file(cfg).name}")
    for pid in tracked + untracked:
        if not _is_our_daemon(pid, cfg):
            if psutil.pid_exists(pid):
                echo(f"pid {pid} is not a jobagent daemon from {cfg.root} (stale pid file entry): left alone")
            res.stale.append(pid)
            continue
        if dry_run:
            tree = sorted(browsers.descendants(pid))
            echo(f"would stop {pid} and its {len(tree) - 1} descendant process(es): {tree}")
            res.stopped.append(pid)
            continue
        # the daemon and everything under it (its Chromes and their renderers); on POSIX the SIGTERM first lets the
        # daemon kill its own browsers, on Windows terminate() is a hard TerminateProcess of each process
        res.processes_killed += browsers.kill_tree(pid, timeout=timeout)
        res.stopped.append(pid)
        echo(f"stopped {pid}")
    if dry_run:
        res.orphans_killed = browsers.orphan_agent_chrome_pids()
        echo(f"would kill {len(res.orphans_killed)} orphaned agent browser(s): {res.orphans_killed}")
        return res
    pid_file(cfg).unlink(missing_ok=True)
    # Chromes that got reparented away from the tree before we walked it. Only browsers no live daemon owns:
    # other installs' daemons (and any shard we could not stop) keep theirs.
    time.sleep(0.5)
    res.orphans_killed = browsers.kill_orphans()
    return res


def status(cfg: Config) -> dict:
    pids = read_pids(cfg)
    ours = [pid for pid in pids if _is_our_daemon(pid, cfg)]
    owned_by_ours: set[int] = set()
    for pid in ours:
        owned_by_ours |= browsers.descendants(pid)
    agent = browsers._agent_chrome_pids()
    orphans = browsers.orphan_agent_chrome_pids()
    root = cfg.root.resolve()
    others = []
    for pid in browsers.live_daemon_pids():
        try:
            p = psutil.Process(pid)
            cwd = _cwd(p)
            if cwd != root and not _parent_is_daemon(p):
                others.append((pid, str(cwd)))
        except psutil.Error:
            continue
    return {
        "pid_file": str(pid_file(cfg)),
        "pids": {pid: (pid in ours) for pid in pids},
        "running": bool(ours),
        "untracked_here": [pid for pid in _daemons_here(cfg) if pid not in ours
                           and not any(pid in browsers.descendants(o) for o in ours)],
        "agent_browsers_total": len(agent),
        "agent_browsers_ours": len([b for b in agent if b in owned_by_ours]),
        "agent_browsers_orphaned": len(orphans),
        "other_daemons": others,
        "log_file": str(log_file(cfg)),
    }
