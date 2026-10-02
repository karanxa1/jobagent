"""Read Gmail through the user's own logged-in browser session: no app password, no Google Cloud OAuth, no Claude.

`jobagent gmail-login` opens plain Chrome (no automation: Google refuses sign-in in automated browsers) on a
dedicated profile (otp.gmail_web.profile_dir). After that, ONE headless Chrome keeps that profile open and fetches
mail over authenticated HTTP from inside the browser context (its cookies):

  - Atom feed  https://mail.google.com/mail/u/N/feed/atom  : unread inbox mail (subject, preview, sender, time, id).
    Read-only: fetching it never changes the read state.
  - "Show original"  ?view=om&permmsgid=msg-f:<id>          : the raw RFC822 message, for exact codes and links.
    Fallback: the print view (?view=pt), which shows the message as simple HTML.
  - Search: the Gmail web UI at #search/<query> in a page, reading the result rows from the DOM. Finds mail that
    was already read (e.g. opened on a phone) or skipped the inbox. Listing search results doesn't mark anything read.

If the session expires (redirect to accounts.google.com / 401), it logs one clear error, returns nothing, closes
the browser (so `jobagent gmail-login` can use the profile) and re-checks every few minutes.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import socket
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from jobagent.config import Config
from jobagent.otp import VERIFY_RE, OTPProvider, from_ats, gmail_profile_dir, html_to_text, mentions, mime_message, \
    name_variants

log = logging.getLogger(__name__)

KEYCHAIN_ARGS = ["--use-mock-keychain", "--password-store=basic", "--enable-automation"]


class SessionExpired(RuntimeError):
    pass


def profile_in_use(profile_dir: Path) -> bool:
    """True if a live Chrome holds this profile (its SingletonLock points at a running pid on this host)."""
    d = Path(profile_dir)
    if os.name == "nt":
        lf = d / "lockfile"
        if not lf.exists():
            return False
        try:
            fd = os.open(lf, os.O_RDWR)
            os.close(fd)
            return False
        except OSError:
            return True
    try:
        target = os.readlink(d / "SingletonLock")
    except OSError:
        return False
    host, _, pid = target.rpartition("-")
    if host and host != socket.gethostname():
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True


def _dt(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat((s or "").strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_atom(xml: str) -> list[dict]:
    """Gmail Atom feed -> [{id (hex message id), from, subject, snippet, date}] (newest first)."""
    ns = "{http://purl.org/atom/ns#}"
    out = []
    try:
        root = ET.fromstring(xml)
        entries = root.findall(f"{ns}entry") or root.findall("entry")
    except ET.ParseError:
        return out
    g = lambda e, tag: (e.findtext(f"{ns}{tag}") or e.findtext(tag) or "")  # noqa: E731
    for e in entries:
        link = e.find(f"{ns}link") if e.find(f"{ns}link") is not None else e.find("link")
        href = html.unescape(link.get("href", "")) if link is not None else ""
        mid = re.search(r"message_id=([0-9a-fA-F]+)", href)
        tag = re.search(r":(\d+)$", g(e, "id"))
        hexid = mid.group(1).lower() if mid else (format(int(tag.group(1)), "x") if tag else "")
        if not hexid:
            continue
        author = e.find(f"{ns}author") if e.find(f"{ns}author") is not None else e.find("author")
        name = mail = ""
        if author is not None:
            name = author.findtext(f"{ns}name") or author.findtext("name") or ""
            mail = author.findtext(f"{ns}email") or author.findtext("email") or ""
        out.append({"id": hexid, "from": f"{name} <{mail}>".strip(), "subject": html.unescape(g(e, "title")),
                    "snippet": html.unescape(g(e, "summary")), "date": _dt(g(e, "issued") or g(e, "modified"))})
    out.sort(key=lambda m: m["date"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return out


def parse_original(page: str) -> dict | None:
    """The 'Show original' page -> parsed message, or None if the page doesn't hold one."""
    m = re.search(r'<pre[^>]*id="?raw_message_text"?[^>]*>(.*?)</pre>', page, re.S)
    if not m:
        return None
    raw = html.unescape(m.group(1))
    if "\n\n" not in raw.replace("\r\n", "\n"):
        return None
    return mime_message(raw.encode("utf-8", "surrogateescape"))


def parse_print(page: str) -> dict | None:
    """The print view -> its LAST message (the newest in the thread): {from, subject, body, links}."""
    chunks = re.split(r'<table[^>]*class="?message"?[^>]*>', page)
    if len(chunks) < 2:
        return None
    last = chunks[-1]
    sender = re.search(r"<b>(.*?)</b>\s*(?:&lt;(.*?)&gt;)?", last, re.S)
    subj = re.search(r'<font size="?\+1"?>\s*<b>(.*?)</b>', page, re.S) or re.search(r"<title>(.*?)</title>", page, re.S)
    body, links = html_to_text(last)
    frm = ""
    if sender:
        frm = f"{html.unescape(re.sub('<[^>]+>', '', sender.group(1))).strip()} <{html.unescape(sender.group(2) or '')}>"
    return {"from": frm, "subject": html.unescape(re.sub("<[^>]+>", "", subj.group(1))).strip() if subj else "",
            "date": None, "body": body, "links": links}


SEARCH_JS = r"""() => {
  const main = [...document.querySelectorAll('div[role=main]')].find(e => e.offsetParent !== null);
  if (!main) return null;
  const rows = [...main.querySelectorAll('tr.zA')].filter(r => r.offsetParent !== null);
  if (!rows.length && !/no messages matched|didn.t match any|no results/i.test(main.innerText) && !main.querySelector('td.TC'))
    return null;
  return rows.map(tr => {
    const s = tr.querySelector('[data-legacy-thread-id]');
    const who = [...tr.querySelectorAll('span[email]')].pop();
    const d = tr.querySelector('td.xW span[title], td.xW span');
    return {thread: s ? s.getAttribute('data-legacy-thread-id') : '',
            last: s ? (s.getAttribute('data-legacy-last-non-draft-message-id') || s.getAttribute('data-legacy-last-message-id') || '') : '',
            subject: (tr.querySelector('.bog') || {}).innerText || '',
            snippet: ((tr.querySelector('.y2') || {}).innerText || '').replace(/^\s*[-–]\s*/, ''),
            from: who ? `${who.getAttribute('name') || ''} <${who.getAttribute('email') || ''}>` : '',
            date: d ? (d.getAttribute('title') || d.innerText) : '',
            unread: tr.classList.contains('zE')};
  });
}"""


class GmailWeb:
    """One headless Chrome on the Gmail profile. All methods raise SessionExpired when signed out."""

    def __init__(self, profile_dir: Path, chrome: str | None, account: int = 0):
        self.profile_dir = Path(profile_dir)
        self.chrome = chrome
        self.base = f"https://mail.google.com/mail/u/{account}/"
        self._pw = self.ctx = self.page = self.spage = None
        self.ua = ""
        self.ik = ""
        self._om_ok: str | None = None  # which "show original" URL form works here
        self.search_lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self.ctx is not None

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        if self.running:
            return
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        try:
            await self._launch()
            if not self.ua:  # headless Chrome says "HeadlessChrome" in its user agent; Gmail serves those differently
                ua = await self.ctx.pages[0].evaluate("navigator.userAgent") if self.ctx.pages else \
                    await (await self.ctx.new_page()).evaluate("navigator.userAgent")
                self.ua = ua.replace("HeadlessChrome", "Chrome")
                if self.ua != ua:
                    await self.ctx.close()
                    await self._launch()
        except Exception:
            await self.stop()
            raise

    async def _launch(self) -> None:
        kw = {"user_agent": self.ua} if self.ua else {}
        # the real OS keychain (not Playwright's mock one), or Chrome can't decrypt the login cookies and drops them
        self.ctx = await self._pw.chromium.launch_persistent_context(
            str(self.profile_dir), headless=True, executable_path=self.chrome, ignore_default_args=KEYCHAIN_ARGS,
            args=["--no-first-run", "--no-default-browser-check", "--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900}, **kw)
        self.page = self.spage = None

    async def stop(self) -> None:
        for closer in (self.ctx and self.ctx.close, self._pw and self._pw.stop):
            if closer:
                try:
                    await closer()
                except Exception:  # noqa: BLE001
                    pass
        self._pw = self.ctx = self.page = self.spage = None

    # ---- HTTP from inside the logged-in browser context ----
    async def get(self, url: str) -> str:
        r = await self.ctx.request.get(url, timeout=30000, max_redirects=10,
                                       headers={"User-Agent": self.ua} if self.ua else None)
        final = r.url or url
        if r.status == 401 or "accounts.google.com" in final or "/ServiceLogin" in final:
            raise SessionExpired(f"HTTP {r.status} at {final[:80]}")
        if r.status >= 400:
            raise RuntimeError(f"HTTP {r.status} for {url[:120]}")
        return await r.text()

    async def feed(self, label: str = "") -> list[dict]:
        url = self.base + "feed/atom" + (f"/{quote(label, safe='')}" if label else "")
        try:
            text = await self.get(url)
        except SessionExpired:
            if label:  # an unknown label can 401 too; only the inbox feed decides about the session
                return []
            raise
        return parse_atom(text)

    async def open_inbox(self) -> None:
        """Keep a Gmail tab open (it refreshes the session cookies) and read the account key `ik` from it."""
        if self.page is None or self.page.is_closed():
            self.page = await self.ctx.new_page()
        await self.page.goto(self.base + "#inbox", wait_until="domcontentloaded", timeout=60000)
        if "accounts.google.com" in self.page.url:
            raise SessionExpired(f"redirected to {self.page.url[:80]}")
        for _ in range(20):
            ik = await self.page.evaluate("() => (window.GLOBALS && window.GLOBALS[9]) || ''")
            if isinstance(ik, str) and re.fullmatch(r"[0-9a-f]{6,16}", ik):
                self.ik = ik
                break
            await asyncio.sleep(0.5)
        if not self.ik:
            m = re.search(r'GLOBALS\s*=\s*\[(?:[^\]]{0,600}?)"([0-9a-f]{10})"', await self.page.content())
            self.ik = m.group(1) if m else ""

    async def message(self, hexid: str) -> dict | None:
        """Full message by id: raw source ('show original'), else the print view."""
        dec = int(hexid, 16)
        ik = f"ik={self.ik}&" if self.ik else ""
        forms = {"om_perm": f"{self.base}?{ik}view=om&permmsgid=msg-f:{dec}",
                 "om_th": f"{self.base}?ui=2&{ik}view=om&th={hexid}",
                 "pt_perm": f"{self.base}?{ik}view=pt&search=all&permmsgid=msg-f:{dec}",
                 "pt_th": f"{self.base}?ui=2&{ik}view=pt&search=all&th={hexid}"}
        order = [self._om_ok] if self._om_ok else []
        order += [k for k in forms if k not in order]
        for k in order:
            try:
                page = await self.get(forms[k])
            except SessionExpired:
                raise
            except Exception as e:  # noqa: BLE001
                log.debug("gmail %s for %s failed: %s", k, hexid, e)
                continue
            m = parse_original(page) if k.startswith("om") else parse_print(page)
            if m and (m.get("body") or m.get("subject")):
                if self._om_ok != k:
                    log.info("gmail_web: reading full messages via %s", k)
                    self._om_ok = k
                return m
        return None

    async def search(self, query: str, limit: int = 25) -> list[dict]:
        """Run a Gmail search in the web UI and read the result rows (thread id, last message id, subject...)."""
        async with self.search_lock:
            if self.spage is None or self.spage.is_closed():
                self.spage = await self.ctx.new_page()
            await self.spage.goto("about:blank")
            await self.spage.goto(self.base + "#search/" + quote(query, safe=""), wait_until="domcontentloaded",
                                  timeout=60000)
            if "accounts.google.com" in self.spage.url:
                raise SessionExpired(f"redirected to {self.spage.url[:80]}")
            rows = None
            deadline = time.monotonic() + 30
            while rows is None and time.monotonic() < deadline:
                await asyncio.sleep(0.7)
                rows = await self.spage.evaluate(SEARCH_JS)
            if rows is None:
                raise RuntimeError("gmail search page didn't render results")
            await asyncio.sleep(0.8)  # rows can still be streaming in
            rows = await self.spage.evaluate(SEARCH_JS) or rows
            return [r for r in rows if r.get("last") or r.get("thread")][:limit]


class GmailWebOTP(OTPProvider):
    """OTP provider over GmailWeb. Shares one browser between any number of concurrent wait_for_code() calls."""

    def __init__(self, cfg: Config, luna, clef=None):
        super().__init__(cfg, luna, clef)
        from jobagent.config import chrome_path

        c = cfg.get("otp.gmail_web", {}) or {}
        self.poll_seconds = float(c.get("poll_seconds", 5))
        self.search_every = float(c.get("search_seconds", 30))
        if c.get("lookback_minutes"):
            self.window_s = float(c["lookback_minutes"]) * 60
        self.labels = list(c.get("feed_labels", ["", "^smartlabel_notification"]) or [""])
        self.gw = GmailWeb(gmail_profile_dir(cfg), chrome_path(cfg), int(c.get("account", 0) or 0))
        self.expired = ""
        self._feed: tuple[float, list[dict]] = (0.0, [])
        self._feed_lock = asyncio.Lock()
        self._bodies: dict[str, dict | None] = {}
        self._searches: dict[str, tuple[float, list[dict]]] = {}
        self._watch: asyncio.Task | None = None
        self._last_check = 0.0

    # ---- lifecycle ----
    async def start(self):
        await self._connect()
        self._watch = asyncio.create_task(self._watchdog())

    async def stop(self):
        if self._watch:
            self._watch.cancel()
        await self.gw.stop()

    def unavailable(self) -> str:
        return self.expired

    async def _connect(self) -> bool:
        self._last_check = time.monotonic()
        prof = self.gw.profile_dir
        if not self.gw.running:
            if profile_in_use(prof):
                return self._expire(f"the Gmail profile {prof} is open in another Chrome (gmail-login running?)")
            if not (prof / "Default").exists() and not (prof / "Local State").exists():
                return self._expire(f"no Gmail session yet in {prof}: run `jobagent gmail-login`")
            try:
                await self.gw.start()
            except Exception as e:  # noqa: BLE001
                return self._expire(f"could not start Chrome on {prof}: {e}")
        try:
            await self.gw.feed()
            await self.gw.open_inbox()
        except SessionExpired as e:
            await self.gw.stop()  # release the profile so `jobagent gmail-login` can use it
            return self._expire(f"Gmail session in {prof} is signed out ({e}). Run `jobagent gmail-login` "
                                "and sign in again; this process picks the new session up within a few minutes")
        except Exception as e:  # noqa: BLE001
            log.warning("gmail_web: session check failed (%s); will retry", e)
            return True
        if self.expired:
            log.info("gmail_web: Gmail session OK again")
        else:
            log.info("gmail_web: Gmail session OK (%s)", prof)
        self.expired = ""
        return True

    def _expire(self, why: str) -> bool:
        if self.expired != why:
            log.error("gmail_web: %s", why)
        self.expired = why
        return False

    async def _watchdog(self):
        """Re-check an expired session every 3 minutes; reload the Gmail tab every 20 (keeps cookies fresh)."""
        while True:
            await asyncio.sleep(30)
            try:
                age = time.monotonic() - self._last_check
                if (self.expired and age > 180) or age > 1200:
                    await self._connect()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("gmail_web watchdog: %s", e)

    async def _guard(self, coro):
        try:
            return await coro
        except SessionExpired as e:
            await self.gw.stop()
            self._expire(f"Gmail session in {self.gw.profile_dir} expired ({e}). Run `jobagent gmail-login`")
            raise

    # ---- mail ----
    async def _feed_entries(self) -> list[dict]:
        async with self._feed_lock:
            if time.monotonic() - self._feed[0] < max(2.0, self.poll_seconds / 2):
                return self._feed[1]
            merged: dict[str, dict] = {}
            for label in self.labels:
                try:
                    for m in await self._guard(self.gw.feed(label)):
                        merged.setdefault(m["id"], m)
                except SessionExpired:
                    raise
                except Exception as e:  # noqa: BLE001
                    log.debug("gmail feed %r failed: %s", label, e)
            entries = sorted(merged.values(), key=lambda m: m["date"] or datetime.min.replace(tzinfo=timezone.utc),
                             reverse=True)
            self._feed = (time.monotonic(), entries)
            return entries

    async def _full(self, m: dict) -> dict:
        """m with the full body (cached; one fetch per message)."""
        if m["id"] not in self._bodies:
            try:
                self._bodies[m["id"]] = await self._guard(self.gw.message(m["id"]))
            except SessionExpired:
                raise
            except Exception as e:  # noqa: BLE001
                log.debug("gmail message %s failed: %s", m["id"], e)
                return m
        full = self._bodies[m["id"]]
        if not full:
            return m
        out = {**m, "body": full.get("body") or "", "links": full.get("links") or []}
        for k in ("from", "subject", "date"):
            if full.get(k) and not out.get(k):
                out[k] = full[k]
        if full.get("date"):
            out["date"] = full["date"]
        return out

    async def _search(self, query: str) -> list[dict]:
        hit = self._searches.get(query)
        if hit and time.monotonic() - hit[0] < self.search_every:
            return hit[1]
        rows = await self._guard(self.gw.search(query))
        msgs = [{"id": (r.get("last") or r["thread"]).lower(), "from": r.get("from", ""),
                 "subject": r.get("subject", ""), "snippet": r.get("snippet", ""), "date": None} for r in rows]
        self._searches[query] = (time.monotonic(), msgs)
        if len(self._searches) > 50:
            self._searches.pop(next(iter(self._searches)))
        return msgs

    async def _recent_search(self, since_ts: int) -> list[dict]:
        now = time.monotonic()
        for q, (t, msgs) in list(self._searches.items()):
            m = re.fullmatch(r"after:(\d+) -in:sent -in:drafts", q)
            if m and int(m.group(1)) <= since_ts and now - t < self.search_every:
                return msgs
        return await self._search(f"after:{since_ts // 600 * 600} -in:sent -in:drafts")

    async def recent_messages(self, query_terms: list[str], since: datetime) -> list[dict]:
        if self.expired or not self.gw.running:
            return []
        names = [v for t in query_terms for v in name_variants(t)]
        wide = datetime.now(timezone.utc) - since > timedelta(days=1)
        msgs: dict[str, dict] = {}
        for m in await self._feed_entries():
            if m["date"] is None or m["date"] >= since:
                msgs[m["id"]] = m
        # search too: finds mail that's already read or outside the inbox; `after:` keeps it to the window. Waiters
        # share searches: any fresh one that started at or before this window will do
        since_ts = int(since.timestamp())
        try:
            if wide and query_terms:  # e.g. `jobagent inbox`: a long window, so narrow by the terms
                q = f"after:{since_ts} -in:sent -in:drafts (" + " OR ".join(f'"{t}"' for t in query_terms) + ")"
                found = await self._search(q)
            else:
                found = await self._recent_search(since_ts)
            for m in found:
                msgs.setdefault(m["id"], m)
        except SessionExpired:
            return []
        except Exception as e:  # noqa: BLE001
            log.debug("gmail search failed: %s", e)
        out = []
        for m in msgs.values():
            text = f"{m['from']}\n{m['subject']}\n{m.get('snippet', '')}"
            if not wide and not (VERIFY_RE.search(text) or mentions(names, text) or from_ats(m["from"])):
                continue  # not worth fetching: no verification wording, not this company, not an ATS
            try:
                out.append(await self._full(m))
            except SessionExpired:
                return []
        out = [m for m in out if m.get("date") is None or m["date"] >= since]
        out.sort(key=lambda m: m.get("date") or datetime.max.replace(tzinfo=timezone.utc), reverse=True)
        return out
