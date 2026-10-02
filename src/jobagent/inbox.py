"""Classify replies to applications (interview / assessment / rejection / ...) with the decider (clef or the main LLM) and link them to jobs."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from jobagent.db import DB
from jobagent.llm import Decider
from jobagent.models import Status
from jobagent.otp import OTPProvider

KINDS = {
    "interview": "Invitation to interview, schedule a call, or chat with a recruiter/hiring manager",
    "assessment": "Take-home assignment, coding test, or online assessment",
    "rejection": "Not moving forward / position filled / rejected",
    "received": "Automatic confirmation that the application was received",
    "info_request": "Asks the candidate for more information or documents",
    "verification": "Verification code or sign-in link",
    "unrelated": "Not about a job application",
}


async def scan(db: DB, clef: Decider, otp: OTPProvider, days: int = 7) -> list[dict]:
    applied = db.by_status(Status.APPLIED, 5000)
    companies = sorted({r["company"] for r in applied})
    since = datetime.now(timezone.utc) - timedelta(days=days)
    msgs = await otp.recent_messages(["application", "interview", "assessment", "candidate"], since)

    async def one(m):
        blob = f"From: {m['from']}\nSubject: {m['subject']}\n\n{m['body']}"
        q = {"kind": {"type": "choice", "instructions": "What is this email?", "criteria": KINDS}}
        if companies:
            q["company"] = {"type": "choice", "instructions": "Which company is this email from/about?",
                            "criteria": {c: None for c in companies[:250]} | {"none": "None of these"}}
        a = await clef.ask("clef-flash", blob, q)
        return {"subject": m["subject"], "from": m["from"], "kind": a["kind"]["choice"],
                "confidence": a["kind"].get("confidence"),
                "company": a.get("company", {}).get("choice", "none")}

    rows = await asyncio.gather(*(one(m) for m in msgs), return_exceptions=True)
    out = [r for r in rows if isinstance(r, dict) and r["kind"] not in ("unrelated", "verification", "received")]
    for r in out:
        db._x("UPDATE jobs SET result=json_set(COALESCE(result,'{}'), '$.reply', json(?)) WHERE company=? AND status=?",
              (json.dumps({"kind": r["kind"], "subject": r["subject"]}), r["company"], Status.APPLIED))
    return out
