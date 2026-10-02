"""Build profile.yaml (the agent's single source of truth for form answers) from the resume."""
from __future__ import annotations

from pathlib import Path

import yaml
from pypdf import PdfReader

from jobagent.config import Config
from jobagent.llm import Luna

TODO = "TODO"

# Fields the resume can't tell us. The agent flags any required question that hits a TODO.
MANUAL = {
    "work_authorization": {
        "country_of_citizenship": "India",
        "authorized_to_work_in_india": True,
        "requires_visa_sponsorship_outside_india": True,
        "willing_to_relocate": True,
        "open_to_remote": True,
    },
    "availability": {
        "notice_period_days": TODO,          # e.g. 30
        "earliest_start_date": TODO,         # e.g. "Immediately" / "2026-11-01"
        "currently_student": TODO,           # true/false
        "graduation_date": TODO,             # e.g. "2027-05"
        "total_years_experience": TODO,      # e.g. 2
    },
    "compensation": {
        "current_ctc_inr_lpa": TODO,
        "expected_ctc_inr_lpa": TODO,
        "expected_usd_per_year_remote": TODO,
        "negotiable": True,
    },
    "eeo": {  # voluntary self-identification; "Decline to self-identify" is always acceptable
        "gender": "Decline to self-identify",
        "pronouns": "Decline to self-identify",
        "race_ethnicity": "Decline to self-identify",
        "veteran_status": "I am not a protected veteran",
        "disability": "I don't wish to answer",
    },
    "standard_answers": {
        "how_did_you_hear": "Company careers page",
        "previously_worked_here": False,
        "has_non_compete": False,
        "background_check_ok": True,
        "over_18": True,
    },
    # Your answers to questions the agent couldn't answer (see unanswered_questions.yaml).
    # Keys are the question text (or a close paraphrase); these override everything else.
    "custom_answers": {},
}

EXTRACT_PROMPT = """Extract the candidate's details from this resume into JSON with exactly these keys:
{"personal": {"first_name","last_name","full_name","email","phone","phone_country_code","city","state","country",
  "linkedin","github","website"},
 "headline": str,
 "summary": str (2-3 sentences, first person, factual),
 "current_title": str, "current_company": str,
 "experience": [{"title","company","start","end","location","highlights":[str]}],
 "education": [{"degree","field","school","start","end"}],
 "certifications": [str],
 "projects": [{"name","url","summary"}],
 "skills": {"languages":[str],"ai":[str],"backend":[str],"frontend":[str],"infra":[str]}}
Use full URLs (https://...). Use "" for anything not present. Do not infer or embellish."""


def resume_text(path: Path) -> str:
    return "\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages).replace("\t", " ")


async def build_profile(cfg: Config) -> Path:
    text = resume_text(cfg.resume_path)
    extracted = await Luna(cfg).chat_json("You convert resumes to structured JSON. Respond with JSON only.",
                                          f"{EXTRACT_PROMPT}\n\nRESUME:\n{text}", max_tokens=32000)
    profile = {**extracted, **MANUAL, "resume_path": str(cfg.resume_path)}
    out = cfg.path("profile", "profile.yaml")
    header = ("# Generated from your resume by `jobagent profile`. Review it, then fill every TODO.\n"
              "# The agent answers application questions ONLY from this file + the resume.\n")
    out.write_text(header + yaml.safe_dump(profile, sort_keys=False, allow_unicode=True, width=110))
    return out


def todos(profile: dict, prefix: str = "") -> list[str]:
    out = []
    for k, v in profile.items():
        if isinstance(v, dict):
            out += todos(v, f"{prefix}{k}.")
        elif v == TODO:
            out.append(prefix + k)
    return out
