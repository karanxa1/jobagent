"""Decide which discovered jobs are worth a browser worker, using the decider's calibrated probabilities
(clef-flash on Cloudflare, or the main LLM; see llm.make_decider)."""
from __future__ import annotations

import asyncio
import json
import logging

from jobagent.config import Config
from jobagent.db import DB
from jobagent.llm import Decider, confident
from jobagent.models import Status

log = logging.getLogger(__name__)

QUESTIONS_VERSION = 2  # bump when QUESTIONS change: queued jobs get re-asked the role questions

QUESTIONS = {
    "ai_role": {
        "type": "noul",
        "instructions": "Is this an AI / ML / LLM / applied-AI / AI-platform engineering role (software engineering "
                        "work building AI systems: LLM apps, agents, RAG, ML infra, model serving, applied ML)?",
        "criteria": {"true": "Hands-on AI/ML/LLM engineering role",
                     "false": "Sales, marketing, recruiting, pure research scientist with PhD requirement, "
                              "data analyst, non-AI software role, or non-engineering role"},
    },
    "eligible": {
        "type": "choice",
        "instructions": "{candidate_eligibility} Can they realistically apply?",
        "criteria": {
            "yes": "Role is in the candidate's country, or remote open to it / worldwide, or explicitly offers visa "
                   "sponsorship/relocation",
            "no": "Requires existing work authorization the candidate lacks (e.g. US citizen/green card only, "
                  "must be EU resident) or remote restricted to regions excluding the candidate's, with no sponsorship",
            "unclear": "Posting doesn't say; role is abroad with sponsorship not mentioned",
        },
    },
    "seniority": {
        "type": "score",
        "instructions": "What experience level does this role require?",
        "criteria": ["intern or new grad", "1-3 years", "3-6 years", "6+ years / staff / principal / director"],
    },
    "fit": {
        "type": "score",
        "instructions": "How well does this role match this candidate: {candidate_fit}",
        "criteria": ["poor", "weak", "decent", "strong", "excellent"],
    },
    "role_type": {
        "type": "choice",
        "instructions": "Which kind of role is this, judging by the actual day-to-day work?",
        "criteria": {
            "ai_engineer": "AI Engineer: builds products and systems on top of LLMs / foundation models: agents, "
                           "tool calling, RAG, evals, prompt/inference pipelines, AI platform or LLM infra, voice AI, "
                           "forward-deployed / applied AI engineering. Includes 'Applied AI', 'LLM', 'GenAI', "
                           "'Agent' and 'AI Platform' engineer titles.",
            "ml_engineer": "Classical ML engineering: training models, feature pipelines, recommender/ranking "
                           "systems, MLOps, computer vision model development",
            "research": "Research scientist / research engineer role centred on novel model research or a PhD",
            "data": "Data scientist, data analyst, analytics or data engineering",
            "intern": "Internship, apprenticeship, or trainee program",
            "gig_or_pool": "Not a real single job opening: hourly 'expert' AI-training / data-labeling gigs, "
                           "talent pools / general applications, or a post listing several unrelated roles",
            "not_ai": "Any other role (general SWE, sales, PM, solutions/support, strategy)",
        },
    },
    "ai_first_company": {
        "type": "noul",
        "instructions": "Is the company AI-first: is its core product built on AI / LLMs (an AI lab, AI-native "
                        "startup, or AI infrastructure company)?",
    },
    "open": {
        "type": "noul",
        "instructions": "Does the posting appear to still be open and accepting applications?",
    },
}


def questions(cfg: Config) -> dict:
    """QUESTIONS with the candidate-specific parts (where they can work, what they're good at) from config.yaml."""
    elig = cfg.get("candidate.eligibility", "") or (
        "The candidate lives in India, is an Indian citizen without foreign work authorization, and will relocate "
        "if a visa is sponsored.")
    fit = cfg.get("candidate.fit", "") or "builds production LLM systems (agents, RAG, evals, Python backends)?"
    q = json.loads(json.dumps(QUESTIONS))
    q["eligible"]["instructions"] = q["eligible"]["instructions"].replace("{candidate_eligibility}", elig)
    q["fit"]["instructions"] = q["fit"]["instructions"].replace("{candidate_fit}", fit)
    return q


def _state(row) -> dict:
    data = json.loads(row["data"])
    return {
        "title": data["title"], "company": data["company"], "location": data.get("location", ""),
        "remote": data.get("remote"), "visa": data.get("visa", ""), "salary": data.get("salary", ""),
        "description": data.get("description") or "",  # clef truncates to its 65k context itself
    }


def decide(cfg: Config, a: dict) -> tuple[bool, float]:
    """Turn clef's probabilities into (apply?, priority). Thresholds live here, not in the model."""
    p_ai = a["ai_role"]["noul"]
    el = a["eligible"]["probabilities"]
    sen = a["seniority"]["score"]
    fit = a["fit"]["score"] / 4
    rt = a.get("role_type")
    accept = set(cfg.get("triage.accept_role_types", ["ai_engineer"]))
    role_ok = rt is None or (rt["choice"] in accept and rt["probabilities"][rt["choice"]] >= 0.5)
    ok = (role_ok and p_ai >= cfg.get("triage.min_ai_role", 0.6)
          # only drop roles that explicitly need work authorization the candidate lacks; "sponsorship not
          # mentioned" stays in, since many companies sponsor without saying so
          and el.get("no", 0) < cfg.get("triage.max_ineligible", 0.6)
          and sen <= cfg.get("triage.max_seniority", 2.85)
          and a["open"]["noul"] >= 0.3)
    # priority: India/remote-eligible first, then unclear-location; better fit and level-appropriate first
    reach = el["yes"] + 0.4 * el.get("unclear", 0)
    ai_first = a.get("ai_first_company", {}).get("noul", 0.5)
    score = round(p_ai * reach * (0.5 + fit) * (1.0 if sen <= 2.2 else 0.6) * (0.8 + 0.4 * ai_first), 4)
    return ok, score


def rescore(cfg: Config, db: DB) -> tuple[int, int]:
    """Re-apply thresholds to already-triaged jobs (no model calls). Returns (requeued, rejected)."""
    up = down = 0
    for st in (Status.QUEUED, Status.REJECTED):
        for row in db.jobs_with_status(st):
            if not row["triage"]:
                continue
            # a result means the agent (or a company-wide skip) rejected it after looking at the real posting:
            # thresholds must never bring those back
            if st == Status.REJECTED and row["result"]:
                continue
            ok, score = decide(cfg, json.loads(row["triage"]))
            new = Status.QUEUED if ok else Status.REJECTED
            if new != st or score != row["score"]:
                db.set_triage(row["key"], new, score, json.loads(row["triage"]))
                up += new == Status.QUEUED and st == Status.REJECTED
                down += new == Status.REJECTED and st == Status.QUEUED
    return up, down


async def retriage_missing(cfg: Config, db: DB, clef: Decider) -> int:
    """Ask only the newer questions (role_type, ai_first_company) for already-queued jobs, then re-decide."""
    newq = {k: questions(cfg)[k] for k in ("role_type", "ai_first_company")}
    rows = [r for r in db.jobs_with_status(Status.QUEUED)
            if r["triage"] and json.loads(r["triage"]).get("_v", 1) < QUESTIONS_VERSION]

    async def one(row):
        try:
            extra = await clef.ask(cfg.get("cloudflare.triage_model", "clef-flash"), _state(row), newq)
        except Exception as e:  # noqa: BLE001
            log.warning("retriage failed for %s: %s", row["key"], e)
            return
        a = {**json.loads(row["triage"]), **extra, "_v": QUESTIONS_VERSION}
        ok, score = decide(cfg, a)
        db.set_triage(row["key"], Status.QUEUED if ok else Status.REJECTED, score, a)

    await asyncio.gather(*(one(r) for r in rows))
    return len(rows)


async def triage_pending(cfg: Config, db: DB, clef: Decider) -> tuple[int, int]:
    rows = db.jobs_with_status(Status.NEW)
    if not rows:
        return 0, 0
    model = cfg.get("cloudflare.triage_model", "clef-flash")
    second = cfg.get("cloudflare.verify_model", "clef")
    kept = 0
    qs = questions(cfg)

    async def one(row):
        nonlocal kept
        state = _state(row)
        try:
            a = await clef.ask(model, state, qs)
            # Cascade: clef-flash (~40ms) settles most jobs; borderline ones get the 27B clef's opinion.
            p_no = a["eligible"]["probabilities"].get("no", 0)
            # (skipped when both names resolve to the same LLM: asking it twice adds nothing)
            resolve = getattr(clef, "resolve", None)
            if (not confident(a["ai_role"]) or 0.45 < p_no < 0.75) and not (resolve and resolve(model) == resolve(second)):
                a = await clef.ask(second, state, qs)
                a["_model"] = second
        except Exception as e:  # noqa: BLE001 - leave it NEW, retry next pass
            log.warning("triage failed for %s: %s", row["key"], e)
            return
        a["_v"] = QUESTIONS_VERSION
        ok, score = decide(cfg, a)
        db.set_triage(row["key"], Status.QUEUED if ok else Status.REJECTED, score, a)
        kept += ok

    await asyncio.gather(*(one(r) for r in rows))
    log.info("triaged %d jobs, queued %d", len(rows), kept)
    return len(rows), kept
