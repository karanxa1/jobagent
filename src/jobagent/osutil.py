"""Small OS-specific helpers so the rest of jobagent runs the same on macOS, Linux and Windows."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys

log = logging.getLogger(__name__)

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"


def keep_awake() -> None:
    """Stop the machine from idle-sleeping while this process (the daemon) is alive.

    macOS: `caffeinate -i -w <pid>`; Linux: `systemd-inhibit ... tail --pid=<pid>` if available; Windows:
    SetThreadExecutionState on the calling thread (call it from the main thread, which lives as long as the process).
    The helper processes exit by themselves when this process does, however it dies."""
    pid = os.getpid()
    try:
        if IS_WIN:
            import ctypes

            ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
            if not ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED):
                log.warning("keep_awake: SetThreadExecutionState failed")
            return
        if IS_MAC:
            cmd = ["caffeinate", "-i", "-w", str(pid)]
        elif shutil.which("systemd-inhibit") and shutil.which("tail"):
            cmd = ["systemd-inhibit", "--what=idle:sleep", "--who=jobagent", "--why=applying to jobs",
                   "--mode=block", "tail", f"--pid={pid}", "-f", os.devnull]
        else:
            log.info("keep_awake: no caffeinate / systemd-inhibit here; the machine may sleep")
            return
        if shutil.which(cmd[0]):
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:  # noqa: BLE001 - staying awake is a nicety, never a reason to crash
        log.warning("keep_awake failed: %s", e)


def notify(title: str, msg: str) -> None:
    """Desktop notification + sound (assist mode: so you notice a captcha). Best effort on every OS."""
    safe = lambda x: str(x).replace('"', "'")[:180]
    try:
        if IS_MAC:
            subprocess.run(["osascript", "-e",
                            f'display notification "{safe(msg)}" with title "{safe(title)}" sound name "Glass"'],
                           check=False, capture_output=True, timeout=10)
        elif IS_WIN:
            import winsound

            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            log.warning("%s: %s", title, msg)
        else:
            if shutil.which("notify-send"):
                subprocess.run(["notify-send", safe(title), safe(msg)], check=False, capture_output=True, timeout=10)
            print("\a", end="", flush=True)
    except Exception:  # noqa: BLE001
        pass


def detached_popen_kwargs() -> dict:
    """Popen kwargs that start a process which outlives this one and ignores this terminal's Ctrl+C."""
    if IS_WIN:
        # CREATE_NO_WINDOW instead of DETACHED_PROCESS: a detached (console-less) process makes every console
        # program it starts (Playwright's node driver, npx MCP servers) pop up its own console window; with a
        # hidden console they inherit that instead. New process group: Ctrl+C here doesn't reach the daemon.
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def resolve_exe(name: str) -> str:
    """Full path of a command on PATH (on Windows this finds npx.cmd etc., which CreateProcess won't), else name."""
    return shutil.which(name) or name
