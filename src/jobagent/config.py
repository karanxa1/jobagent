from __future__ import annotations

import os
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
        p = Path(os.path.expanduser(self.get(dotted, default)))
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


def load_config(path: str | Path | None = None) -> Config:
    load_dotenv(ROOT / ".env")
    p = Path(path) if path else ROOT / "config.yaml"
    raw = yaml.safe_load(p.read_text()) if p.exists() else {}
    return Config(raw=raw or {})


def load_profile(cfg: Config) -> dict:
    p = cfg.path("profile", "profile.yaml")
    if not p.exists():
        raise FileNotFoundError(f"{p} not found. Run `jobagent profile` to generate it from your resume.")
    return yaml.safe_load(p.read_text())
