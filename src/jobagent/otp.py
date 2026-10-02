"""Fetch email verification codes / magic links for in-flight applications.

Providers:
  - mcp:  any Gmail MCP server (stdio) exposing a search tool and a read tool; names are configurable.
  - imap: Gmail IMAP with an app password (GMAIL_APP_PASSWORD in .env).

Several browser workers can be waiting on codes at the same time, so every lookup is scoped by the
company name/domain and by the time the code was requested, and a message is handed out only once.
"""
from __future__ import annotations

import asyncio
import email
import imaplib
import json
import logging
import os
import re
import time
from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header

from jobagent.config import Config
from jobagent.llm import Clef, Luna

log = logging.getLogger(__name__)

CODE_RE = re.compile(r"(?<![\w#])(\d{4,8}|[A-Z0-9]{6,8})(?![\w])")
LINK_RE = re.compile(r"https?://[^\s\"'<>]+")


class OTPProvider:
    def __init__(self, cfg: Config, luna: Luna, clef: Clef | None = None):
        self.cfg = cfg
        self.luna = luna
        self.clef = clef
        self.used: set[str] = set()
        self.lock = asyncio.Lock()

    async def recent_messages(self, query_terms: list[str], since: datetime) -> list[dict]:
        """Return [{id, from, subject, date, body}] newest first."""
        raise NotImplementedError

    async def start(self): ...
    async def stop(self): ...

    async def wait_for_code(self, company: str, sender_hint: str = "", requested_at: float | None = None,
                            timeout: int = 180, want: str = "code") -> str:
        """Poll until a matching email arrives. want = 'code' | 'link'. Returns '' on timeout."""
        since = datetime.fromtimestamp((requested_at or time.time()) - 90, tz=timezone.utc)
        terms = [t for t in {company, sender_hint} if t]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msgs = await self.recent_messages(terms, since)
            except Exception as e:  # noqa: BLE001
                log.warning("otp provider error: %s", e)
                msgs = []
            for m in msgs:
                if m["id"] in self.used:
                    continue
                blob = f"From: {m['from']}\nSubject: {m['subject']}\n\n{m['body']}"
                if not re.search(r"verif|code|otp|one[- ]time|confirm|security|passcode|login|sign in|magic",
                                 blob, re.I):
                    continue
                value = await self._extract(blob, company, want)
                if value:
                    async with self.lock:
                        if m["id"] in self.used:
                            continue
                        self.used.add(m["id"])
                    log.info("otp for %s found in %r", company, m["subject"])
                    return value
            await asyncio.sleep(6)
        return ""

    async def _extract(self, blob: str, company: str, want: str) -> str:
        if self.clef:
            # clef-flash (~40ms) decides "is this the email we're waiting for"; a regex pulls the code.
            try:
                a = await self.clef.ask("clef-flash", blob, {
                    "match": {"type": "noul", "instructions": f"Is this an email verification code / sign-in link "
                              f"email sent for a job application or account at {company!r} (or its hiring platform)?"},
                    "kind": {"type": "choice", "instructions": "What does the email contain?",
                             "criteria": {"code": "A numeric or alphanumeric one-time code",
                                          "link": "A verification / magic sign-in link", "neither": None}},
                })
                if a["match"]["noul"] < 0.3:
                    return ""
                if a["match"]["noul"] > 0.7 and want == "code" and a["kind"]["choice"] == "code":
                    body = blob.split("\n\n", 1)[-1]
                    codes = [c for c in CODE_RE.findall(body) if not re.fullmatch(r"(19|20)\d\d", c)]
                    if len(set(codes)) == 1:
                        return codes[0]
            except Exception as e:  # noqa: BLE001
                log.debug("clef otp filter failed: %s", e)
        res = await self.luna.chat_json(
            "You extract verification codes and verification links from emails. Respond in JSON.",
            f"An application to {company!r} is waiting for an email {want}. Is this email that {want}? "
            f'Return {{"match": bool, "code": str, "link": str}} (empty strings when absent).\n\n{blob}',
            max_tokens=8000,
        )
        if not res.get("match"):
            return ""
        return (res.get("link") if want == "link" else res.get("code")) or res.get("code") or res.get("link") or ""


class MCPGmail(OTPProvider):
    async def start(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        c = self.cfg.get("otp.mcp", {})
        self._stack = AsyncExitStack()
        params = StdioServerParameters(command=c["command"], args=c.get("args", []), env={**os.environ, **c.get("env", {})})
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        tools = {t.name for t in (await self.session.list_tools()).tools}
        for k in ("search_tool", "read_tool"):
            if c.get(k) and c[k] not in tools:
                raise RuntimeError(f"Gmail MCP has no tool {c[k]!r}; available: {sorted(tools)}")
        self.c = c

    async def stop(self):
        await self._stack.aclose()

    @staticmethod
    def _text(result) -> str:
        return "\n".join(getattr(b, "text", "") for b in (result.content or []))

    async def recent_messages(self, query_terms, since):
        newer = max(1, int((datetime.now(timezone.utc) - since).total_seconds() // 60) + 1)
        q = f"newer_than:{min(newer, 60 * 24)}m" if newer < 60 else f"newer_than:{newer // 60 + 1}h"
        q += " (" + " OR ".join(f'"{t}"' for t in query_terms) + " OR verification OR code)" if query_terms else ""
        res = await self.session.call_tool(self.c["search_tool"], {self.c.get("search_arg", "query"): q, "maxResults": 10})
        text = self._text(res)
        ids = re.findall(r"\bID:\s*([0-9a-f]{10,})", text) or re.findall(r'"id"\s*:\s*"([0-9a-f]{10,})"', text)
        out = []
        for mid in ids[:10]:
            if mid in self.used:
                continue
            body = self._text(await self.session.call_tool(self.c["read_tool"], {self.c.get("read_arg", "messageId"): mid}))
            subj = re.search(r"Subject:\s*(.*)", body)
            frm = re.search(r"From:\s*(.*)", body)
            out.append({"id": mid, "from": frm.group(1) if frm else "", "subject": subj.group(1) if subj else "", "body": body})
        return out


class IMAPGmail(OTPProvider):
    def _fetch(self, since: datetime) -> list[dict]:
        c = self.cfg.get("otp.imap", {})
        M = imaplib.IMAP4_SSL(c.get("host", "imap.gmail.com"))
        M.login(c["user"], os.environ["GMAIL_APP_PASSWORD"])
        try:
            M.select("INBOX", readonly=True)
            day = (since - timedelta(days=1)).strftime("%d-%b-%Y")
            _, data = M.search(None, f'(SINCE "{day}")')
            out = []
            for num in reversed(data[0].split()[-25:]):
                _, msg_data = M.fetch(num, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])
                date = email.utils.parsedate_to_datetime(msg["Date"])
                if date < since:
                    continue
                body = ""
                for part in msg.walk():
                    if part.get_content_type() in ("text/plain", "text/html") and not part.is_multipart():
                        body += part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                out.append({"id": msg["Message-ID"] or num.decode(), "from": str(make_header(decode_header(msg["From"] or ""))),
                            "subject": str(make_header(decode_header(msg["Subject"] or ""))),
                            "body": re.sub(r"<[^>]+>", " ", body)})
            return out
        finally:
            M.logout()

    async def recent_messages(self, query_terms, since):
        return await asyncio.to_thread(self._fetch, since)


class BridgeOTP(OTPProvider):
    """Queue code requests in jobs.db; an external helper with inbox access (a Claude session using a Gmail MCP
    connector, via `jobagent bridge-watch` / `jobagent bridge-answer`) finds the code and writes it back."""

    async def wait_for_code(self, company: str, sender_hint: str = "", requested_at: float | None = None,
                            timeout: int = 300, want: str = "code") -> str:
        import sqlite3

        conn = sqlite3.connect(self.cfg.db_path, isolation_level=None)
        try:
            # nothing has arrived from this company despite repeated asks: the signup / send likely failed on the
            # site's side. Say so instead of making the inbox helper search again (Workday did this dozens of times)
            recent_fail = conn.execute(
                "SELECT COUNT(*) FROM mail_requests WHERE company=? AND status='failed' AND requested_at > ?",
                (company, time.time() - 1800)).fetchone()[0]
            recent_ok = conn.execute(
                "SELECT COUNT(*) FROM mail_requests WHERE company=? AND status='done' AND requested_at > ?",
                (company, time.time() - 1800)).fetchone()[0]
            if recent_fail >= 4 and not recent_ok:
                return ("NOTE: no email from this company has reached the inbox after several requests in the last "
                        "30 minutes, so the site most likely never sent it (the account creation / request probably "
                        "failed on the page). Re-check the page for an error; otherwise flag_for_human.")
            rid = conn.execute(
                "INSERT INTO mail_requests(kind,company,sender_hint,want,requested_at,updated_at) "
                "VALUES('otp',?,?,?,?,datetime('now'))",
                (company, sender_hint, want, requested_at or time.time())).lastrowid
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                await asyncio.sleep(3)
                st, res = conn.execute("SELECT status, result FROM mail_requests WHERE id=?", (rid,)).fetchone()
                if st == "done":
                    return res or ""
                if st == "failed":
                    # the helper looked once and found nothing; the email may still be on its way. Ask again
                    # (after a pause) until our own deadline, instead of giving up on the first empty search
                    if deadline - time.monotonic() < 60:
                        return ""
                    await asyncio.sleep(45)
                    rid = conn.execute(
                        "INSERT INTO mail_requests(kind,company,sender_hint,want,requested_at,updated_at) "
                        "VALUES('otp',?,?,?,?,datetime('now'))",
                        (company, sender_hint, want, requested_at or time.time())).lastrowid
            conn.execute("UPDATE mail_requests SET status='failed', result='timeout' WHERE id=? AND status='pending'", (rid,))
            return ""
        finally:
            conn.close()


class NoOTP(OTPProvider):
    async def wait_for_code(self, *a, **k) -> str:
        return ""


def make_otp_provider(cfg: Config, luna: Luna, clef: Clef | None = None) -> OTPProvider:
    kind = cfg.get("otp.provider", "none")
    return {"mcp": MCPGmail, "imap": IMAPGmail, "bridge": BridgeOTP}.get(kind, NoOTP)(cfg, luna, clef)
