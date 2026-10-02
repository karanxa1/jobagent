"""Fetch email verification codes / magic links for in-flight applications.

Providers (config `otp.provider`):
  - gmail_web: the user's own Gmail web session in a dedicated Chrome profile (`jobagent gmail-login`), read over
               authenticated HTTP from one headless browser (see gmail_web.py). No app password, no OAuth.
  - imap:      Gmail IMAP with an app password (GMAIL_APP_PASSWORD in .env).
  - mcp:       any Gmail MCP server (stdio) exposing a search tool and a read tool; names are configurable.
  - bridge:    requests go into jobs.db (`mail_requests`) and an external helper answers them: a Claude Code
               session with the Gmail connector, or `jobagent otp-relay` (gmail_web / imap, no Claude needed).
  - none

Several browser workers can be waiting on codes at the same time, so every lookup is scoped by the company name
(or the employer name the form uses, `sender_hint`) and by the time the code was requested, the NEWEST matching
email wins, and an email handed to one company is never handed to a different one.

wait_for_code() returns the code, the full link (want="link"), "NOTE: <what the company's email says>" when the
company's email is there but holds no code/link (e.g. "an account already exists"), or "" when nothing arrived.
"""
from __future__ import annotations

import asyncio
import email
import email.utils
import html
import imaplib
import logging
import os
import re
import time
from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from html.parser import HTMLParser

from jobagent.config import Config

log = logging.getLogger(__name__)

WINDOW_S = 300  # accept mail received from requested_at minus 5 minutes
LINK_RE = re.compile(r"https?://[^\s\"'<>]+")
VERIFY_RE = re.compile(r"verif|code|otp|one[- ]time|confirm|security|passcode|\bpin\b|log ?in|sign[- ]?in|magic|"
                       r"reset|activat|password|access|validate|authenticat", re.I)
# sender domains of hiring platforms: their mail is about the employer named inside it
ATS_DOMAINS = ("greenhouse-mail.io", "greenhouse.io", "myworkday.com", "workday.com", "myworkdayjobs.com", "lever.co",
               "ashbyhq.com", "smartrecruiters.com", "smartrecruitersmail.com", "oracle.com", "oraclecloud.com",
               "taleo.net", "icims.com", "successfactors.com", "successfactors.eu", "sap.com", "workable.com",
               "workablemail.com", "jobvite.com", "bamboohr.com", "recruitee.com", "teamtailor.com", "breezy.hr",
               "jazzhr.com", "applytojob.com", "personio.de", "personio.com", "rippling.com", "dover.com",
               "zohorecruit.com", "zohorecruit.in", "keka.com", "darwinbox.in", "darwinbox.com", "eightfold.ai",
               "phenompeople.com", "avature.net", "cornerstoneondemand.com", "ultipro.com", "ukg.com", "paylocity.com",
               "adp.com", "pinpointhq.com", "gem.com", "wellfound.com", "instahyre.com", "naukri.com")
ATS_NAMES = "Greenhouse, Workday, Lever, Ashby, SmartRecruiters, Oracle/Taleo, iCIMS, SuccessFactors, Workable, Jobvite"
_SUFFIXES = {"inc", "llc", "ltd", "limited", "pvt", "private", "plc", "gmbh", "corp", "corporation", "co", "company",
             "technologies", "technology", "tech", "labs", "group", "holdings", "global", "careers", "the", "us", "usa",
             "india", "uk", "hq", "software", "systems", "solutions", "services"}
# a delimited token that looks like a one-time code (exactness is checked against the email text afterwards)
_CODE_AFTER = re.compile(r"(?:code|passcode|pin|otp)\s*(?:is|:|-)?\s*[:#]?\s*([A-Za-z0-9!+/=?*\-]{4,12})(?=$|[\s.,;)\]])",
                         re.I)


# ---- small text helpers ----------------------------------------------------------------------------------------

class _Text(HTMLParser):
    """HTML -> readable text + the href of every link (entities unescaped, so URLs are exact)."""
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "td", "section", "pre"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.links: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head", "title"):
            self._skip += 1
        if tag in self.BLOCK:
            self.out.append("\n")
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if href.startswith(("http://", "https://")):
                self.links.append(href.strip())

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head", "title") and self._skip:
            self._skip -= 1
        if tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)


def html_to_text(markup: str) -> tuple[str, list[str]]:
    p = _Text()
    try:
        p.feed(markup)
        p.close()
    except Exception:  # noqa: BLE001 - malformed HTML: keep what was parsed
        pass
    text = re.sub(r"[ \t ‌͏]+", " ", "".join(p.out))
    return re.sub(r"\n\s*\n+", "\n\n", text).strip(), p.links


def mime_message(raw: bytes | str) -> dict:
    """RFC822 -> {from, subject, date, body, links}; body is the text/plain part (else the HTML as text)."""
    msg = email.message_from_bytes(raw) if isinstance(raw, bytes) else email.message_from_string(raw)
    plain, htm = "", ""
    for part in msg.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True) or b""
        text = payload.decode(part.get_content_charset() or "utf-8", "replace")
        if ctype == "text/plain":
            plain += text
        else:
            htm += text
    links: list[str] = []
    html_text = ""
    if htm:
        html_text, links = html_to_text(htm)
    body = plain.strip() or html_text
    links += [u for u in LINK_RE.findall(plain) if u not in links]
    try:
        date = email.utils.parsedate_to_datetime(msg["Date"]) if msg["Date"] else None
        if date and date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        date = None
    hdr = lambda k: str(make_header(decode_header(msg[k] or ""))) if msg[k] else ""  # noqa: E731
    return {"from": hdr("From"), "subject": hdr("Subject"), "date": date, "body": body, "links": links,
            "message_id": msg["Message-ID"] or ""}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (s or "").lower())).strip()


def name_variants(name: str) -> list[str]:
    """'Bluevine - US' -> ['bluevine us', 'bluevine']; 'Acme Technologies Pvt Ltd' -> [..., 'acme']."""
    out = []
    for piece in [name, *re.split(r"\s[-|(/]\s?|\s\(|,", name or "")]:
        n = _norm(piece)
        if not n:
            continue
        out.append(n)
        core = " ".join(w for w in n.split() if w not in _SUFFIXES)
        if core:
            out.append(core)
    return [v for v in dict.fromkeys(out) if len(v) >= 3]


def mentions(names: list[str], text: str) -> bool:
    t = f" {_norm(text)} "
    t_squashed = t.replace(" ", "")
    for v in names:
        if f" {v} " in t or (len(v) >= 5 and v.replace(" ", "") in t_squashed):
            return True
    return False


def from_ats(sender: str) -> bool:
    s = (sender or "").lower()
    dom = s.rsplit("@", 1)[-1].strip("> ")
    return any(dom == d or dom.endswith("." + d) for d in ATS_DOMAINS)


def _blob(m: dict) -> str:
    links = [u for u in dict.fromkeys(m.get("links") or [])
             if not re.search(r"unsubscribe|privacy|/terms|\.(png|jpe?g|gif)(\?|$)|preferences|facebook\.com|"
                              r"twitter\.com|linkedin\.com/company|instagram\.com|youtube\.com", u, re.I)][:40]
    date = m.get("date")
    head = f"From: {m.get('from', '')}\nSubject: {m.get('subject', '')}\n"
    if date:
        head += f"Date: {date.astimezone(timezone.utc):%Y-%m-%d %H:%M:%S} UTC\n"
    body = (m.get("body") or m.get("snippet") or "")[:12000]
    return head + "\n" + body + ("\n\nLinks in the email:\n" + "\n".join(links) if links else "")


def exact_code(code: str, blob: str) -> str:
    """The code exactly as written in the email (case, symbols), or '' if the email doesn't contain it."""
    code = (code or "").strip().strip("\"'`")
    if not code:
        return ""
    if re.search(rf"(?<![A-Za-z0-9]){re.escape(code)}(?![A-Za-z0-9])", blob):
        return code
    m = re.search(rf"(?<![A-Za-z0-9]){re.escape(code)}(?![A-Za-z0-9])", blob, re.I)
    if m:
        return m.group(0)
    squashed = code.replace(" ", "").replace("-", "")  # "123 456" in the email, "123456" from the model
    if squashed != code and re.search(rf"(?<![A-Za-z0-9]){re.escape(squashed)}(?![A-Za-z0-9])", blob):
        return squashed
    return ""


def exact_link(link: str, m: dict, blob: str) -> str:
    link = html.unescape((link or "").strip().strip("<>\"'"))
    if not link.startswith("http"):
        return ""
    links = [html.unescape(u) for u in (m.get("links") or [])]
    if link in links:
        return link
    longer = sorted((u for u in links if u.startswith(link[:60])), key=len, reverse=True)
    if longer:  # the model shortened it: use the email's own full URL
        return longer[0]
    return link if link in html.unescape(blob) else ""


def rule_code(m: dict) -> str:
    """Codes in well-known fixed templates, read without a model (Greenhouse: 'Copy and paste this code ...: X')."""
    text = f"{m.get('subject', '')}\n{m.get('body') or m.get('snippet') or ''}"
    g = re.search(r"security code field on your application:\s*(?:#\s*)?(\S+)", text)
    if g:
        return g.group(1)
    return ""


# ---- providers -------------------------------------------------------------------------------------------------

class OTPProvider:
    poll_seconds = 6.0
    note_grace = 45.0  # wait this long for a code before answering with a NOTE about the company's code-less email

    def __init__(self, cfg: Config, luna, clef=None):
        self.cfg = cfg
        self.luna = luna
        self.clef = clef
        self.given: dict[str, str] = {}  # message id -> company it was handed to
        self.lock = asyncio.Lock()
        self._verdicts: dict[tuple, dict] = {}
        self.window_s = float(cfg.get("otp.lookback_minutes", WINDOW_S / 60) or WINDOW_S / 60) * 60

    # back-compat: some callers look at .used
    @property
    def used(self) -> set[str]:
        return set(self.given)

    async def recent_messages(self, query_terms: list[str], since: datetime) -> list[dict]:
        """Return [{id, from, subject, date (aware datetime or None), body, links?}] newest first."""
        raise NotImplementedError

    async def start(self): ...
    async def stop(self): ...

    def unavailable(self) -> str:
        """Non-empty reason when lookups can't work right now (e.g. Gmail session expired)."""
        return ""

    async def wait_for_code(self, company: str, sender_hint: str = "", requested_at: float | None = None,
                            timeout: int = 180, want: str = "code") -> str:
        """Poll until a matching email arrives. want = 'code' | 'link'. Returns '' on timeout."""
        since = datetime.fromtimestamp((requested_at or time.time()) - self.window_s, tz=timezone.utc)
        terms = [t for t in dict.fromkeys([company, sender_hint]) if t]
        start = time.monotonic()
        note = ""
        while True:
            if why := self.unavailable():
                log.debug("otp lookup for %s skipped: %s", company, why)
                return ""
            try:
                msgs = await self.recent_messages(terms, since)
            except Exception as e:  # noqa: BLE001
                log.warning("otp provider error: %s", e)
                msgs = []
            value, found_note = await self.pick(msgs, company, sender_hint, want, since)
            if value:
                return value
            note = found_note or note
            waited = time.monotonic() - start
            if note and (waited >= self.note_grace or waited + self.poll_seconds >= timeout):
                return f"NOTE: {note}"
            if waited + self.poll_seconds >= timeout:
                return ""
            await asyncio.sleep(self.poll_seconds)

    async def pick(self, msgs: list[dict], company: str, sender_hint: str, want: str,
                   since: datetime) -> tuple[str, str]:
        """(value, note) from the newest matching email in the window that wasn't handed to another company."""
        key = _norm(company)
        names = name_variants(company) + (name_variants(sender_hint) if sender_hint else [])
        cands = []
        for m in msgs:
            d = m.get("date")
            if d is not None and d < since:
                continue
            other = self.given.get(m["id"])
            if other is not None and other != key:
                continue  # already handed to a different company
            text = f"{m.get('from', '')}\n{m.get('subject', '')}\n{m.get('body') or m.get('snippet') or ''}"
            if not VERIFY_RE.search(text) or not (mentions(names, text) or from_ats(m.get("from", ""))):
                continue
            cands.append(m)
        cands.sort(key=lambda m: m.get("date") or datetime.max.replace(tzinfo=timezone.utc), reverse=True)
        note = ""
        for m in cands[:8]:
            v = await self.classify(m, company, sender_hint, want, names)
            value = (v.get("link") if want == "link" else v.get("code")) or v.get("code") or v.get("link") or ""
            if value:
                async with self.lock:
                    other = self.given.get(m["id"])
                    if other is not None and other != key:
                        continue
                    self.given[m["id"]] = key
                log.info("otp for %s found in %r (%s)", company, m.get("subject", ""), m["id"])
                return value, ""
            if v.get("note") and not note:
                note = v["note"]
        return "", note

    async def classify(self, m: dict, company: str, sender_hint: str, want: str, names: list[str]) -> dict:
        ck = (m["id"], _norm(company), _norm(sender_hint), want, bool(m.get("body")))
        if ck in self._verdicts:
            return self._verdicts[ck]
        blob = _blob(m)
        out: dict = {}
        # an ATS template that names a different employer ("Security code for your application to X"): no model call
        named = re.search(r"(?:application|applying|apply) (?:to|at|for|with) (.+?)\s*$", m.get("subject", ""), re.I)
        if named and from_ats(m.get("from", "")) and not mentions(names, named.group(1)):
            self._verdicts[ck] = {"match": False}
            return self._verdicts[ck]
        # fixed ATS templates naming this company: exact, no model needed
        if want == "code" and mentions(names, f"{m.get('subject', '')}\n{m.get('body') or m.get('snippet') or ''}"):
            if (c := exact_code(rule_code(m), blob)) and not self._truncated(m, c):
                out = {"match": True, "code": c}
        if not out:
            out = await self._extract(m, blob, company, sender_hint, want)
        if out.get("code") and self._truncated(m, out["code"]):
            out["code"] = ""  # only seen at the cut-off end of a preview: wait for the full body
        self._verdicts[ck] = out
        return out

    @staticmethod
    def _truncated(m: dict, code: str) -> bool:
        """A code found only in a preview snippet that ends right after it may be cut off."""
        if m.get("body"):
            return False
        text = f"{m.get('subject', '')}\n{m.get('snippet', '')}".rstrip()
        return text.endswith(code) and code not in m.get("subject", "")

    async def _extract(self, m: dict, blob: str, company: str, sender_hint: str, want: str) -> dict:
        alias = f" (the application form calls the employer {sender_hint!r}, e.g. a parent company or new brand)" \
            if sender_hint and _norm(sender_hint) != _norm(company) else ""
        try:
            res = await self.luna.chat_json(
                "You read one email and extract a verification code or link for a job application. Respond in JSON.",
                f"A browser agent applying to {company!r}{alias} is waiting for an emailed {want}.\n"
                f"Is THIS email the verification / security-code / one-time-code / sign-in / magic-link / "
                f"password-reset / account-activation email for that application, or for an account on that "
                f"employer's careers site or its hiring platform ({ATS_NAMES} ...)? An email that names a DIFFERENT "
                f"employer is not a match. Application confirmations, rejections and newsletters are not a match.\n"
                f'Return {{"match": bool, "code": str, "link": str, "note": str}}:\n'
                f"- code: the one-time code copied EXACTLY, character for character (keep upper/lower case and "
                f"symbols such as ! + / = ? * -), or \"\".\n"
                f"- link: the full verification / magic sign-in / reset URL exactly as listed under 'Links in the "
                f"email' (never shortened), or \"\".\n"
                f"- note: only when match is true but the email has neither code nor link: one short sentence on "
                f"what it tells the candidate to do (e.g. 'an account already exists; sign in or reset the "
                f"password'). Otherwise \"\".\n\n{blob}",
                max_tokens=4000)
        except Exception as e:  # noqa: BLE001
            log.warning("otp extraction failed: %s", e)
            return {}
        if not isinstance(res, dict) or not res.get("match"):
            return {"match": False}
        code = exact_code(str(res.get("code") or ""), blob)
        if res.get("code") and not code:
            log.warning("otp: model's code for %s isn't in the email verbatim; ignored", company)
        link = exact_link(str(res.get("link") or ""), m, blob)
        note = "" if code or link else re.sub(r"\s+", " ", str(res.get("note") or "")).strip()[:300]
        return {"match": True, "code": code, "link": link, "note": note}


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
        self._bodies: dict[str, dict] = {}

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
            if mid not in self._bodies:
                body = self._text(await self.session.call_tool(self.c["read_tool"],
                                                               {self.c.get("read_arg", "messageId"): mid}))
                subj = re.search(r"Subject:\s*(.*)", body)
                frm = re.search(r"From:\s*(.*)", body)
                dt = re.search(r"Date:\s*(.*)", body)
                try:
                    date = email.utils.parsedate_to_datetime(dt.group(1).strip()) if dt else None
                except (TypeError, ValueError):
                    date = None
                self._bodies[mid] = {"id": mid, "from": frm.group(1) if frm else "", "date": date,
                                     "subject": subj.group(1) if subj else "", "body": body,
                                     "links": LINK_RE.findall(body)}
            out.append(self._bodies[mid])
        return out


class IMAPGmail(OTPProvider):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._cache: tuple[float, datetime, list[dict]] | None = None
        self._fetch_lock = asyncio.Lock()

    def _fetch(self, since: datetime) -> list[dict]:
        c = self.cfg.get("otp.imap", {})
        M = imaplib.IMAP4_SSL(c.get("host", "imap.gmail.com"))
        M.login(c["user"], os.environ["GMAIL_APP_PASSWORD"].replace(" ", ""))
        try:
            M.select("INBOX", readonly=True)  # read-only: nothing gets marked as read
            day = (since - timedelta(days=1)).strftime("%d-%b-%Y")
            _, data = M.search(None, f'(SINCE "{day}")')
            out = []
            for num in reversed(data[0].split()[-25:]):
                _, msg_data = M.fetch(num, "(BODY.PEEK[])")
                m = mime_message(msg_data[0][1])
                if m["date"] and m["date"] < since:
                    continue
                out.append({"id": m["message_id"] or num.decode(), **m})
            return out
        finally:
            M.logout()

    async def recent_messages(self, query_terms, since):
        # many concurrent waiters share one IMAP fetch every few seconds
        async with self._fetch_lock:
            if self._cache and time.monotonic() - self._cache[0] < 4 and self._cache[1] <= since:
                return [m for m in self._cache[2] if not m["date"] or m["date"] >= since]
            msgs = await asyncio.to_thread(self._fetch, since)
            self._cache = (time.monotonic(), since, msgs)
            return msgs


class BridgeOTP(OTPProvider):
    """Queue code requests in jobs.db; an external helper with inbox access answers them: `jobagent otp-relay`
    (gmail_web / imap), or a Claude session with a Gmail connector (`jobagent bridge-wait` / `bridge-answer`)."""

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


def make_otp_provider(cfg: Config, luna, clef=None) -> OTPProvider:
    kind = cfg.get("otp.provider", "none")
    if kind == "gmail_web":
        # several daemon shards: only shard 0 holds the Gmail browser (and answers the bridge queue for the others)
        shard = int(os.environ.get("JOBAGENT_SHARD", "0"))
        if int(os.environ.get("JOBAGENT_SHARDS", "1")) > 1 and shard != 0:
            return BridgeOTP(cfg, luna, clef)
        from jobagent.gmail_web import GmailWebOTP, profile_in_use

        if profile_in_use(gmail_profile_dir(cfg)):
            # another jobagent process (daemon / otp-relay) holds the Gmail session: ask it through the bridge
            log.info("Gmail profile is in use by another process; asking it for codes through the bridge queue")
            return BridgeOTP(cfg, luna, clef)
        return GmailWebOTP(cfg, luna, clef)
    return {"mcp": MCPGmail, "imap": IMAPGmail, "bridge": BridgeOTP}.get(kind, NoOTP)(cfg, luna, clef)


def gmail_profile_dir(cfg: Config):
    return cfg.path("otp.gmail_web.profile_dir", "browser_profiles/gmail")


# ---- relay: answer the bridge queue from this machine's inbox (no Claude needed) --------------------------------

async def serve_bridge(cfg: Config, provider: OTPProvider, *, once: bool = False, wait_seconds: int = 120,
                       heartbeat_seconds: int = 300, poll: float = 2.0) -> dict:
    """Answer pending `mail_requests` rows (written by BridgeOTP) with `provider`, several at a time.

    Same protocol as `jobagent bridge-answer`: status 'done' + the code / link / 'NOTE: ...', or 'failed' when no
    matching email arrived within wait_seconds (the asking worker re-asks later). once=True: answer what is
    pending now, then return."""
    from jobagent.db import DB

    db = DB(cfg.db_path)
    stats = {"answered": 0, "notes": 0, "failed": 0}
    inflight: dict[int, asyncio.Task] = {}
    last_beat = 0.0

    async def answer(r) -> None:
        try:
            value = await provider.wait_for_code(r["company"] or "", r["sender_hint"] or "",
                                                 requested_at=r["requested_at"], timeout=wait_seconds,
                                                 want=r["want"] or "code")
        except Exception as e:  # noqa: BLE001
            log.warning("otp-relay: request %s (%s) failed: %s", r["id"], r["company"], e)
            value = ""
        cur = db._x("UPDATE mail_requests SET status=?, result=?, updated_at=datetime('now') "
                    "WHERE id=? AND status='pending'", ("done" if value else "failed", value, r["id"]))
        if not getattr(cur, "rowcount", 1):
            return  # the asker timed out, or another helper answered first
        if value.startswith("NOTE:"):
            stats["notes"] += 1
        stats["answered" if value else "failed"] += 1
        log.info("otp-relay: #%s %s want=%s -> %s", r["id"], r["company"], r["want"],
                 ("NOTE" if value.startswith("NOTE:") else "link" if value.startswith("http") else "code")
                 if value else "nothing (failed; the worker re-asks)")

    while True:
        rows = db._x("SELECT * FROM mail_requests WHERE status='pending' ORDER BY id").fetchall()
        for r in rows:
            if r["id"] not in inflight:
                inflight[r["id"]] = asyncio.create_task(answer(r))
        for rid in [k for k, t in inflight.items() if t.done()]:
            inflight.pop(rid)
        if once:
            if inflight:
                await asyncio.gather(*inflight.values(), return_exceptions=True)
            return stats
        if time.monotonic() - last_beat >= heartbeat_seconds:
            last_beat = time.monotonic()
            why = provider.unavailable()
            log.info("otp-relay alive: %d answered (%d NOTE), %d failed, %d in progress | inbox: %s",
                     stats["answered"], stats["notes"], stats["failed"], len(inflight), why or "ok")
        await asyncio.sleep(poll)
