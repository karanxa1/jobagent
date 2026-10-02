from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Config:
    raw: dict
    root: Path = ROOT

    # ---- convenience accessors -------------------------------------------------------------
    def get(self, dotted: str, default=None):
        cur = self.raw
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def path(self, dotted: str, default: str) -> Path:
        p = Path(os.path.expandvars(os.path.expanduser(str(self.get(dotted, default) or default))))
        return p if p.is_absolute() else self.root / p

    @property
    def resume_path(self) -> Path:
        return self.path("resume", "resume.pdf")

    @property
    def db_path(self) -> Path:
        return self.path("db", "jobs.db")

    @property
    def artifacts_dir(self) -> Path:
        d = self.path("artifacts_dir", "artifacts")
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def ledger_path(self) -> Path:
        return self.path("ledger", "APPLIED_JOBS.md")

    @property
    def storage_state_path(self) -> Path:
        """Cookies/localStorage exported from the login browser, shared by all parallel workers."""
        return self.path("logins.storage_state", "browser_profiles/storage_state.json")

    @property
    def login_profile_dir(self) -> Path:
        return self.path("logins.profile_dir", "browser_profiles/login")


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(path: str | Path | None = None) -> Config:
    """config.yaml, overlaid with config.local.yaml (gitignored: your endpoints, models, limits) if present, then
    with the YAML file named by $JOBAGENT_CONFIG if set."""
    load_dotenv(ROOT / ".env")
    p = Path(path) if path else ROOT / "config.yaml"
    raw = (yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}) or {}
    local = p.with_name("config.local.yaml")
    if local.exists():
        raw = _merge(raw, yaml.safe_load(local.read_text(encoding="utf-8")) or {})
    # one more overlay for a single run / test (inherited by the daemons `jobagent start` spawns)
    if extra := os.environ.get("JOBAGENT_CONFIG"):
        raw = _merge(raw, yaml.safe_load(Path(os.path.expanduser(extra)).read_text(encoding="utf-8")) or {})
    return Config(raw=raw)


def load_profile(cfg: Config) -> dict:
    p = cfg.path("profile", "profile.yaml")
    if not p.exists():
        raise FileNotFoundError(f"{p} not found. Run `jobagent profile` to generate it from your resume.")
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def _chrome_candidates(system: str, env: dict) -> list[Path]:
    """Usual Google Chrome (then Chromium) install locations for this OS, most preferred first."""
    if system == "darwin":
        apps = [Path("/Applications"), Path(env.get("HOME") or Path.home()) / "Applications"]
        return [a / app / "Contents" / "MacOS" / exe for app, exe in
                (("Google Chrome.app", "Google Chrome"), ("Chromium.app", "Chromium")) for a in apps]
    if system == "win32":
        bases = [env.get(k) for k in ("PROGRAMFILES", "PROGRAMW6432", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        return [Path(b) / "Google" / "Chrome" / "Application" / "chrome.exe" for b in bases if b] + \
               [Path(b) / "Chromium" / "Application" / "chrome.exe" for b in bases if b]
    return [Path("/opt/google/chrome/chrome")]


def find_chrome(system: str | None = None, env: dict | None = None, which=shutil.which) -> str | None:
    """Locate an installed Google Chrome / Chromium, or None (browser-use then falls back to its own Chromium)."""
    system = system or sys.platform
    env = {k.upper(): v for k, v in (os.environ if env is None else env).items()}
    if not system.startswith(("darwin", "win32")):  # Linux / BSD: prefer what is on PATH
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            if found := which(name):
                return found
    for c in _chrome_candidates("darwin" if system.startswith("darwin") else
                                "win32" if system.startswith("win32") else "linux", env):
        if c.is_file():
            return str(c)
    if system.startswith("win32"):
        return which("chrome") or which("chrome.exe")
    return None


def chrome_path(cfg: Config) -> str | None:
    """apply.chrome_path if set (an explicit path always wins, even if it does not exist), else auto-detected."""
    explicit = str(cfg.get("apply.chrome_path") or "").strip().strip('"')
    if explicit:
        return str(Path(os.path.expandvars(os.path.expanduser(explicit))))
    return find_chrome()
