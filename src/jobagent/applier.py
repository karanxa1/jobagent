"""One application = one browser-use agent in its own Chrome, driven by the Azure OpenAI deployment in config.yaml."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from jobagent import browsers
from jobagent.config import Config, chrome_path
from jobagent.db import host_of
from jobagent.llm import Clef, Luna, browser_fallback_llm, browser_llm
from jobagent.models import normalize_company, ApplyResult, Job, Status
from jobagent.otp import OTPProvider
from jobagent.style import HUMAN_QUESTION, HUMAN_STYLE, clean, tells

log = logging.getLogger(__name__)


BOT_CHECK = re.compile(r"captcha|human check|human verification|verify you are human|bot|cloudflare|"
                       r"session verification|unusual activity|access is temporarily restricted|spam", re.I)
CONFIRM_QUOTE = re.compile(r"[“\"][^”\"]{0,200}\b(thank|submitted|received|success|applied)\w*[^”\"]{0,200}[”\"]", re.I)
ALREADY_APPLIED = re.compile(r"already applied|already submitted an application|already been submitted|"
                             r"you have applied (to|for) this", re.I)


class QA(BaseModel):
    question: str
    answer: str
    source: Literal["profile", "resume", "generated", "unknown"] = "profile"


class Outcome(BaseModel):
    status: Literal["applied", "ready", "needs_human", "failed", "closed", "not_eligible"]
    summary: str = Field(description="One or two sentences on what happened")
    blocker: str = Field("", description="If not applied: what stopped you (captcha, login wall, unknown question, ...)")
    questions: list[QA] = Field(default_factory=list, description="Every non-trivial form question and what you answered")


def first_name(profile: dict) -> str:
    p = profile.get("personal", {})
    return p.get("first_name") or (p.get("full_name") or "").split(" ")[0] or "Me"


def candidate_facts(profile: dict) -> dict:
    """Values the agent rules quote verbatim (phone formats, location strings, school aliases), from profile.yaml."""
    p = profile.get("personal", {})
    phone = re.sub(r"\D", "", str(p.get("phone", "")))
    cc = str(p.get("phone_country_code", "")).strip()
    cc = cc if not cc or cc.startswith("+") else "+" + cc
    city, state, country = p.get("city", ""), p.get("state", ""), p.get("country", "")
    half = len(phone) // 2
    formats = [f'"{phone}" with the country picker on {country} ({cc})', f'"{cc}{phone}"',
               f'"{cc} {phone[:half]} {phone[half:]}"', f'"0{phone}"', f'"{phone[:half]} {phone[half:]}"']
    edu = (profile.get("education") or [{}])[0] or {}
    schools = [edu.get("institution") or edu.get("school") or "", *(p.get("school_aliases") or [])]
    schools = [x for x in dict.fromkeys(schools) if x] + ([city] if city else [])
    return {
        "email": p.get("email", ""), "phone": phone, "phone_cc": cc, "phone_formats": ", ".join(formats),
        "city": city, "state": state, "country": country, "postal_code": p.get("postal_code", ""),
        "location": ", ".join(x for x in (city, state, country) if x),
        "school_names": ", ".join(f'"{x}"' for x in schools) or '"(none)"',
        "citizenship": (profile.get("work_authorization") or {}).get("country_of_citizenship", country),
    }


RULES = """\
You are applying to a job on behalf of the candidate below. Work like a careful human applicant.

HARD RULES
1. Be truthful. Use only facts from CANDIDATE PROFILE and RESUME. Never invent employers, degrees, dates,
   certifications, skills, years of experience, salary, or work authorization.
2. Personal details, work authorization, notice period, compensation, EEO answers: take them from the profile.
   If a REQUIRED question can't be answered from the profile/resume, call `answer_question` first; if it
   returns UNKNOWN, call `flag_for_human` with the exact question. Optional questions you can't answer: skip.
3. Free-text questions ("Why do you want to join?", "Describe a project ..."): call `answer_question`.
   Cover letter fields or uploads: call `write_cover_letter`, then paste the text or upload the returned file.
4. Resume upload: use the `attach_file` action (which='resume'). Prefer "upload resume" over manual entry when
   offered. Afterwards, fix any fields the ATS auto-parsed wrongly: wrong auto-filled values (city, PIN code, dates,
   school, titles) are never a reason to flag; clear each one and retype the profile value (postal code {postal_code}, city
   {city}, state {state}), and delete auto-parsed entries that aren't in the profile. If the form is inside an iframe and won't respond,
   open the iframe's URL directly.
5. Email verification code / magic link: trigger it, then call `get_email_verification` and enter the code or open the link.
6. If an account is required, sign up / sign in with the profile email and the password placeholder
   <secret>x_ats_password</secret> (type exactly that tag; it is replaced with the real password, which has
   upper/lower case, digits and a symbol and is 18 characters). If the site caps passwords below 18 characters
   (e.g. "9-16 characters"), use <secret>x_ats_password_short</secret> instead (16 characters, same mix). When
   signing in to an existing account, if one of the two is rejected, try the other before resetting. If an account already exists
   and the password is rejected, use "forgot password" once, get the reset link with
   `get_email_verification(kind='link')`, and set the password to x_ats_password. If that fails, flag_for_human.
   WORKDAY: the "Create Account" / "Sign In" / "Submit" buttons are overlay elements (a div with role=button and
   aria-label="Create Account" etc.): click THAT element, not the text inside it. Tick the terms/privacy checkbox
   first if there is one; if a click doesn't tick it, click its label text, or Tab to it and press Space, then
   confirm it shows as checked. If the click still does nothing, click into "Verify New Password" and press Enter.
   Workday "My Information": set "Phone Device Type" = Mobile, set the "Country Phone Code" dropdown FIRST (type
   "{country}", pick "{country} ({phone_cc})"), then Phone Number = {phone} (digits only, no {phone_cc}, no spaces).
   Country = {country}, then Address Line 1 / City / State ({state}) / Postal Code from the profile.
   After "Create Account" (Workday, SuccessFactors, Taleo, iCIMS...), if the page doesn't move on or shows no error:
   call `get_email_verification(kind='link')` to verify the account, then go back and "Sign In" with the same email
   and password; if it says the account already exists, just Sign In.
6b. Prefer creating / using an email + password account (rule 6). Use "Sign in with Google" / "Continue with
   Google" only when there is no email/password option at all. The browser may hold the candidate's Google session: pick {email} in the account chooser and
   allow the basic profile/email consent. Never type a Google password or 2-step code; if Google asks for either,
   or shows "couldn't sign you in", call flag_for_human with reason "google login expired". Same for "Sign in with
   LinkedIn" (use the existing LinkedIn session, never type its password).
7. {captcha_rule}
8. If the page shows the job is closed / no longer accepting applications: finish with status "closed".
   If the job requires work authorization the profile says the candidate lacks and offers no sponsorship:
   finish with status "not_eligible". Same if the posting REQUIRES fluency in a spoken language other than English
   (e.g. "French speaker", "fluent German", "native Japanese") that the profile doesn't list. Same for roles that
   explicitly require ITAR eligibility, "U.S. person" status, U.S. citizenship, or a security clearance: the
   candidate is a citizen of {citizenship}; if that doesn't meet the requirement, finish with "not_eligible". A
   generic EAR / export-control compliance statement ("must be authorized to access technology under the EAR",
   "may require an export license") is NOT a disqualifier on its own, especially for roles based in {country}: apply normally.
8b. Autocomplete / dropdown fields: type a few characters slowly and pick the matching suggestion. If the school,
   company or city isn't in the list, try other names for it (school: {school_names}), then
   pick "Other" / "Not listed" and type the full name in the follow-up field. Location: "{location}".
   School / University search boxes: use `pick_option` (question "School", option = each school name above in
   turn; then "Other"). It types real keystrokes, which these searches need.
   Location / City autocompletes: FIRST try `pick_option` (question "Location", option "{location}");
   if that fails: click the field, type only "{city}",
   WAIT ~2 seconds for suggestions (use a wait action), then press ArrowDown and Enter (or click the "{location}"
   suggestion). Typing the full string at once or pressing Enter before suggestions load
   leaves the field empty. Confirm the field shows {city} before moving on; if suggestions never load, retype it.
   Required consent / privacy checkboxes: click the label text if clicking the box itself does nothing. Accept the
   standard REQUIRED consents to process / store the application (including demographic / EEO data processing,
   privacy notice, data retention for the hiring process, being kept on file / contacted about future job openings).
   Do NOT accept, and flag instead: binding arbitration /
   jury-trial waivers, broad third-party background or data-broker checks, or anything optional and marketing-like.
   GREENHOUSE dropdowns ("Select..." boxes, react-select): do NOT use the select-dropdown action on them, it only
   changes the visible label and the form still says "required". Use the `pick_option` action (question words +
   exact option text): it types the option with real keystrokes and presses Enter, which commits the value. If the form
   shows an error after Submit, fix each listed field this way before resubmitting.
   Custom dropdowns that the dropdown action can't operate (iCIMS, Workday, Taleo widgets): click the field to open
   it, type the first letters of the option and press Enter; or focus it and use ArrowDown then Enter; or click the
   option text in the opened list. Verify the chosen value is shown before moving on.
   Phone fields with a country/flag dropdown (intl-tel-input, Greenhouse, Workday...): open the dropdown FIRST,
   type "{country}", pick "{country} ({phone_cc})", then type {phone} (digits only). A separate "Country" select:
   {country}, set with `pick_option` (question "Country", option "{country}"); the "{country} {phone_cc}" entry belongs
   to the phone picker and does NOT fill the Country field. If a country / region list has no {country}, pick the truthful catch-all
   ("Other", "Rest of World", "International", "Outside the US", "Asia", "APAC") instead of stopping.
   Required fields that only apply to people the candidate is not (employee ID of a company they never worked at,
   referrer's name when nobody referred them, current-employer badge number, etc.): type "N/A" and continue.
   If SEVERAL visibly filled fields (resume, phone, location, dropdowns) all say "required" at once, the form's
   state is out of sync with the page, and retyping won't fix it: reload the application URL ONCE and fill the fresh
   form top to bottom (phone country first, location with pick_option, dropdowns with pick_option, resume via
   attach_file), then submit once. Never put the resume in the Cover Letter field: leave it empty or attach a
   written cover letter (attach_file which='cover_letter').
   If a field shows the right text but the form still says "required" / "invalid" (common for Greenhouse phone),
   retype it with `type_like_keyboard` (real key presses) before trying other formats.
   Phone number rejected as invalid: try, in order, {phone_formats}. Clear the field fully before each try.
9. NEVER click "mailto:" links or "Apply via email" buttons (they open the desktop mail app). If the posting
   says to email an address, call `apply_by_email` with that address instead.
10. If a question refers to the job description (e.g. "type the first words of the posting"), open the
   posting URL in a new tab, read it, and answer exactly. Don't navigate to unrelated sites. Don't click ads. Don't sign up for newsletters/talent communities
   unless it is the only way to apply. Uncheck marketing opt-ins.
11. {submit_rule}
12. After submitting, wait for the confirmation page/message and quote it in the summary.
13. FIND A WAY TO APPLY. A broken route is not a reason to stop; before flag_for_human or "failed", try the
   other routes in order:
   a. Dead link / page not found / no Apply button / stuck spinner / job board says "apply on company site":
      open the company's own careers page (search "<company> careers" or try <company domain>/careers, /jobs),
      find the same role (same or very close title, still open) and apply there. Also try the posting's
      original ATS link if one is visible (Greenhouse, Lever, Workday, Ashby, SmartRecruiters, Keka, Zoho...).
   b. Apply form broken (submit does nothing, iframe dead, endless validation loop on a correct value): reload
      once, then open the form's direct URL in a new tab, then try the company careers page route above.
   c. Role not listed on the careers site but the careers/jobs page gives a hiring email (careers@, jobs@,
      hiring@, talent@, hr@ of THE COMPANY's own domain): call apply_by_email to that address. Never use a
      generic support/info/sales address or an address on a job board / recruiter platform domain.
   d. A general "submit your resume" / talent-pool form on the company's own site is acceptable ONLY when the
      specific role links to it or the careers page says that's how they hire for it.
   Limits that still hold: never invent answers (rule 1), never solve captchas (rule 7), never apply to a
   different role than this one, never pay or join paid services. If every route is exhausted, flag with a
   precise reason listing the routes you tried.
"""

INVISIBLE_CAPTCHA = ("A \"protected by reCAPTCHA\" badge / reCAPTCHA iframe / hCaptcha notice with no visible checkbox or "
                     "puzzle is NOT a blocker: it runs by itself when you press Submit. Fill the form and press Submit. "
                     "Only stop if, AFTER pressing Submit, an image/puzzle challenge appears or the form says verification "
                     "failed (then press Submit once more before flagging). ")
CAPTCHA_HEADLESS = ("CAPTCHA: you may tick a plain \"I'm not a robot\" checkbox once. If an image/puzzle challenge "
                    "appears, call `flag_for_human` with reason starting 'captcha:'. Never try to solve challenges.")
CAPTCHA_ASSIST = ("CAPTCHA: you may tick a plain \"I'm not a robot\" checkbox once. If an image/puzzle challenge "
                  "appears, call `wait_for_human`; the human solves it, then you continue. Never solve it yourself.")

SUBMIT = "Submit the application when every required field is complete and you've reviewed it once."
DRY_RUN = ("DRY RUN: fill in everything, but DO NOT click the final Submit/Apply/Send button. Stop on the final "
           "review page and finish with status 'ready'.")


# file inputs in this document and every same-origin iframe inside it (iCIMS, Taleo, SuccessFactors nest the

# the combobox <input> of the react-select / custom dropdown whose question text contains `q` (same-origin iframes too)
COMBO_FIND_JS = """(q => {
  // fuzzy: the share of the query's words (3+ letters) found in the text around each dropdown; best share wins,
  // the nearest enclosing question breaks ties. The agent paraphrases questions, so exact substrings miss.
  const words = q.toLowerCase().match(/[a-z0-9]{3,}/g) || [];
  const score = t => { t = t.toLowerCase(); return words.length ? words.filter(w => t.includes(w)).length / words.length : 0; };
  const docs = (function collect(doc) { let out = [doc];
    for (const f of doc.querySelectorAll('iframe,frame')) { try { if (f.contentDocument) out = out.concat(collect(f.contentDocument)); } catch (e) {} }
    return out; })(document);
  let best = null, bestScore = 0, bestDepth = 99;
  for (const doc of docs) {
    for (const inp of doc.querySelectorAll('input[role=combobox], [role=combobox] input, input[aria-autocomplete]')) {
      const lab = inp.id && doc.querySelector('label[for="' + inp.id + '"]');
      let box = inp;
      for (let d = 0; d < 8 && box; d++, box = box.parentElement) {
        const t = (lab ? lab.innerText + ' ' : '') + (box.innerText || '') + ' ' + (inp.getAttribute('aria-label') || '');
        const sc = score(t);
        if (sc >= 0.6) {
          if (sc > bestScore + 0.05 || (Math.abs(sc - bestScore) <= 0.05 && d < bestDepth)) { best = inp; bestScore = sc; bestDepth = d; }
          break;
        }
      }
    }
  }
  return best;
})"""

# visible option elements of the open dropdown, with their centre coordinates
OPTIONS_JS = """(() => {
  const docs = (function collect(doc) { let out = [doc];
    for (const f of doc.querySelectorAll('iframe,frame')) { try { if (f.contentDocument) out = out.concat(collect(f.contentDocument)); } catch (e) {} }
    return out; })(document);
  const out = [];
  for (const doc of docs) for (const o of doc.querySelectorAll('[role=option], [class*="option"]')) {
    const r = o.getBoundingClientRect();
    if (r.width && r.height) out.push({t: (o.innerText || '').trim(), x: r.left + r.width / 2, y: r.top + r.height / 2});
  }
  return out;
})()"""


SCROLL_OPTION_JS = """(t => {
  const docs = (function collect(doc) { let out = [doc];
    for (const f of doc.querySelectorAll('iframe,frame')) { try { if (f.contentDocument) out = out.concat(collect(f.contentDocument)); } catch (e) {} }
    return out; })(document);
  for (const doc of docs) for (const o of doc.querySelectorAll('[role=option], [class*="option"]')) {
    if ((o.innerText || '').trim() === t) { o.scrollIntoView({block: 'center'}); const r = o.getBoundingClientRect();
      if (r.width && r.height) return {x: r.left + r.width / 2, y: r.top + r.height / 2}; }
  }
  return null;
})(%s)"""


async def _type_keys(send, sid, text: str) -> None:
    """Type like a keyboard (keyDown with text + keyUp per character). Search-as-you-type widgets such as Greenhouse's
    city box only react to key events; Input.insertText changes the value without triggering their lookup."""
    for ch in text:
        code = ("Key" + ch.upper()) if ch.isalpha() else ("Digit" + ch if ch.isdigit() else "")
        await send.Input.dispatchKeyEvent(params={"type": "keyDown", "key": ch, "text": ch, "code": code}, session_id=sid)
        await send.Input.dispatchKeyEvent(params={"type": "keyUp", "key": ch, "code": code}, session_id=sid)
        await asyncio.sleep(0.05)


async def type_into(browser_session, index: int, text: str) -> str:
    """Clear the field at `index` and type `text` with trusted key events, then Tab out. React-controlled inputs
    (Greenhouse phone / location) can show a value set programmatically yet still report "required"."""
    node = await browser_session.get_element_by_index(index)
    if node is None:
        return f"no element with index {index} (refresh the page state and use a current index)"
    cdp = await browser_session.cdp_client_for_node(node)
    send, sid = cdp.cdp_client.send, cdp.session_id
    obj = (await send.DOM.resolveNode(params={"backendNodeId": node.backend_node_id}, session_id=sid))["object"]
    await send.Runtime.callFunctionOn(params={"objectId": obj["objectId"], "functionDeclaration":
        "function(){this.scrollIntoView({block:'center'}); this.focus(); if (this.select) this.select();}"},
        session_id=sid)
    for _ in range(2):  # delete the selection, then anything a formatter re-inserted
        await send.Input.dispatchKeyEvent(params={"type": "keyDown", "key": "Backspace", "code": "Backspace",
                                                  "windowsVirtualKeyCode": 8}, session_id=sid)
        await send.Input.dispatchKeyEvent(params={"type": "keyUp", "key": "Backspace", "code": "Backspace",
                                                  "windowsVirtualKeyCode": 8}, session_id=sid)
    await _type_keys(send, sid, text)
    await asyncio.sleep(0.4)
    await send.Input.dispatchKeyEvent(params={"type": "keyDown", "key": "Tab", "code": "Tab",
                                              "windowsVirtualKeyCode": 9}, session_id=sid)
    await send.Input.dispatchKeyEvent(params={"type": "keyUp", "key": "Tab", "code": "Tab",
                                              "windowsVirtualKeyCode": 9}, session_id=sid)
    r = await send.Runtime.callFunctionOn(params={"objectId": obj["objectId"], "returnByValue": True,
        "functionDeclaration": "function(){return [this.value, this.getAttribute('aria-invalid')]}"}, session_id=sid)
    value, invalid = (r.get("result", {}).get("value") or ["", None])
    return f"Typed into [{index}]; field now shows {value!r}" + (" and is still marked invalid" if invalid == "true" else "")


CODE_BOXES_JS = """(() => {
  const vis = e => e.offsetParent !== null && !e.disabled && !e.readOnly;
  let boxes = [...document.querySelectorAll('input[id^="security-input-"]')].filter(vis);
  if (!boxes.length) boxes = [...document.querySelectorAll('input[maxlength="1"]')].filter(vis)
        .filter(e => /code|otp|verif|digit|pin|security/i.test((e.id||'') + (e.name||'') + (e.getAttribute('aria-label')||'') +
                      (e.closest('form,fieldset,div') ? e.closest('form,fieldset,div').innerText.slice(0, 300) : '')));
  if (!boxes.length) boxes = [...document.querySelectorAll('input')].filter(vis)
        .filter(e => /security[_ -]?code|verification[_ -]?code|one[- ]time|otp|\\bcode\\b/i.test(
                      (e.id||'') + ' ' + (e.name||'') + ' ' + (e.getAttribute('aria-label')||'') + ' ' + (e.placeholder||''))).slice(0, 1);
  return boxes;
})()"""


async def enter_code(browser_session, code: str) -> str:
    """Put an emailed code into the page's code field(s) exactly as given (case matters: Greenhouse codes are mixed
    case, and model-typed codes came out as 'bN1J...' for 'bn1J...'). Handles one box per character."""
    frames, _ = await browser_session.get_all_frames()
    sessions = {}
    for frame_id in frames:
        try:
            cdp = await browser_session.cdp_client_for_frame(frame_id)
            sessions.setdefault(cdp.session_id, cdp)
        except Exception:  # noqa: BLE001
            continue
    if not sessions:  # frame listing can come back empty (e.g. data: pages): fall back to the page itself
        cdp = await browser_session.get_or_create_cdp_session()
        sessions[cdp.session_id] = cdp
    for cdp in sessions.values():
        send, sid = cdp.cdp_client.send, cdp.session_id
        try:
            arr = (await send.Runtime.evaluate(params={"expression": CODE_BOXES_JS}, session_id=sid))["result"]
            props = await send.Runtime.getProperties(params={"objectId": arr["objectId"], "ownProperties": True},
                                                     session_id=sid)
        except Exception:  # noqa: BLE001
            continue
        boxes = [p["value"]["objectId"] for p in props["result"] if p["name"].isdigit()]
        if not boxes:
            continue
        chunks = list(code) if len(boxes) == len(code) else [code]
        for oid, chunk in zip(boxes, chunks):
            await send.Runtime.callFunctionOn(params={"objectId": oid, "functionDeclaration":
                "function(){this.scrollIntoView({block:'center'}); this.focus(); if (this.select) this.select();}"},
                session_id=sid)
            await send.Input.insertText(params={"text": chunk}, session_id=sid)  # exact characters, case kept
            await asyncio.sleep(0.08)
        got = ""
        for oid in boxes[:len(chunks)]:
            r = await send.Runtime.callFunctionOn(params={"objectId": oid, "returnByValue": True,
                                                          "functionDeclaration": "function(){return this.value}"}, session_id=sid)
            got += r["result"].get("value") or ""
        if got == code:
            return f"Entered the code {code} into {len(boxes)} field(s) and checked it reads back exactly. Now click Submit."
        return (f"Tried to enter {code} into {len(boxes)} field(s) but they read {got!r}. Clear them and type it with "
                "type_like_keyboard, keeping upper/lower case exactly.")
    return ""


async def pick_combo_option(browser_session, question: str, option: str) -> str:
    """Select `option` in the dropdown labelled `question` with trusted keyboard input (focus, type, Enter): react-
    select widgets (Greenhouse) ignore programmatic value changes, so clicks via the DOM never commit the value."""
    frames, _ = await browser_session.get_all_frames()
    sessions = {}
    for frame_id in frames:
        try:
            cdp = await browser_session.cdp_client_for_frame(frame_id)
            sessions.setdefault(cdp.session_id, cdp)
        except Exception:  # noqa: BLE001
            continue
    for cdp in sessions.values():
        send, sid = cdp.cdp_client.send, cdp.session_id
        try:
            r = await send.Runtime.evaluate(params={"expression": f"{COMBO_FIND_JS}({json.dumps(question)})"},
                                            session_id=sid)
        except Exception:  # noqa: BLE001
            continue
        oid = r["result"].get("objectId")
        if not oid:
            continue
        await send.Runtime.callFunctionOn(params={"objectId": oid, "functionDeclaration":
            "function(){ this.scrollIntoView({block:'center'}); this.focus(); this.click && this.click(); }"},
            session_id=sid)
        await asyncio.sleep(0.3)
        # clear anything typed before, then type the option like a person and commit with Enter
        for _ in range(3):
            await send.Input.dispatchKeyEvent(params={"type": "keyDown", "key": "Backspace", "code": "Backspace",
                                                      "windowsVirtualKeyCode": 8}, session_id=sid)
            await send.Input.dispatchKeyEvent(params={"type": "keyUp", "key": "Backspace", "code": "Backspace",
                                                      "windowsVirtualKeyCode": 8}, session_id=sid)
        await _type_keys(send, sid, option)
        await asyncio.sleep(1.2)
        # click the option whose text matches exactly (Enter would take the first fuzzy match: "India" ->
        # "British Indian Ocean Territory"); fall back to Enter only when there's no exact / prefix match
        opts = (await send.Runtime.evaluate(params={"expression": OPTIONS_JS, "returnByValue": True},
                                            session_id=sid))["result"].get("value") or []
        want = option.strip().lower()
        hit = next((o for o in opts if o["t"].lower() == want), None) or \
            next((o for o in opts if o["t"].lower().startswith(want + " ") or o["t"].lower().startswith(want + " (")), None)
        if not hit and " " in option.strip():
            # async search boxes (city / school): retype just the first word, give suggestions time to load, then
            # take the option containing most of the wanted words ("Austin" -> "Austin, Texas, United States")
            for _ in range(len(option) + 2):
                await send.Input.dispatchKeyEvent(params={"type": "keyDown", "key": "Backspace", "code": "Backspace",
                                                          "windowsVirtualKeyCode": 8}, session_id=sid)
                await send.Input.dispatchKeyEvent(params={"type": "keyUp", "key": "Backspace", "code": "Backspace",
                                                          "windowsVirtualKeyCode": 8}, session_id=sid)
            first = re.split(r"[\s,]+", option.strip())[0]
            await _type_keys(send, sid, first)
            await asyncio.sleep(2.5)
            opts = (await send.Runtime.evaluate(params={"expression": OPTIONS_JS, "returnByValue": True},
                                                session_id=sid))["result"].get("value") or []
            words = [w for w in re.findall(r"[a-z]+", want) if len(w) > 2]
            scored = sorted(((sum(w in o["t"].lower() for w in words), o) for o in opts
                             if o["t"].lower().startswith(first.lower())), key=lambda x: -x[0])
            hit = scored[0][1] if scored else None
        if hit:
            # keyboard selection (react-select highlights the first option; ArrowDown to ours, then Enter). Mouse
            # clicks miss when the menu renders below the fold, and scrolling closes some menus.
            idx = next((i for i, o in enumerate(opts) if o is hit), 0)
            for _ in range(idx):
                for t in ("keyDown", "keyUp"):
                    await send.Input.dispatchKeyEvent(params={"type": t, "key": "ArrowDown", "code": "ArrowDown",
                                                              "windowsVirtualKeyCode": 40}, session_id=sid)
                await asyncio.sleep(0.05)
            for t in ("keyDown", "keyUp"):
                await send.Input.dispatchKeyEvent(params={"type": t, "key": "Enter", "code": "Enter",
                                                          "windowsVirtualKeyCode": 13}, session_id=sid)
        elif any(w in o["t"].lower() for o in opts for w in re.findall(r"[a-z0-9]{3,}", want)
                 if not re.search(r"no options|results? available", o["t"].lower())):
            for t in ("keyDown", "keyUp"):
                await send.Input.dispatchKeyEvent(params={"type": t, "key": "Enter", "code": "Enter",
                                                          "windowsVirtualKeyCode": 13}, session_id=sid)
        else:
            # nothing matches: Enter would leave the field cleared (the Backspaces above already dropped the old
            # choice), so the agent kept wiping its own earlier answer. Escape, then show it the real options.
            async def key(k, code, vk):
                for t in ("keyDown", "keyUp"):
                    await send.Input.dispatchKeyEvent(params={"type": t, "key": k, "code": code,
                                                              "windowsVirtualKeyCode": vk}, session_id=sid)
            await key("Escape", "Escape", 27)
            await asyncio.sleep(0.3)
            await key("ArrowDown", "ArrowDown", 40)  # opens the menu with an empty search: every option
            await asyncio.sleep(0.8)
            allopts = (await send.Runtime.evaluate(params={"expression": OPTIONS_JS, "returnByValue": True},
                                                   session_id=sid))["result"].get("value") or []
            await key("Escape", "Escape", 27)
            names = [o["t"] for o in allopts if o.get("t")][:40]
            return (f"No option matches {option!r} in the dropdown for {question!r}, so nothing was selected (the "
                    f"field is now empty). Its options are: {names}. Call pick_option again with the exact text of "
                    "the truthful one (for 'how did you hear': the job board / company website / 'Other').")
        await asyncio.sleep(0.5)
        chk = await send.Runtime.callFunctionOn(params={"objectId": oid, "returnByValue": True, "functionDeclaration":
            "function(){ let b=this; for(let i=0;i<8&&b;i++,b=b.parentElement){ const t=(b.innerText||''); "
            "if(t.length>2) return t.slice(0,160);} return ''; }"}, session_id=sid)
        return f"typed {option!r} + Enter into the dropdown for {question!r}; it now reads: " + \
            (chk["result"].get("value") or "").replace("\n", " | ")
    raise RuntimeError(f"no dropdown found whose question contains {question!r}")


# form in a same-origin iframe, which shares the page's CDP session but not its `document`)
FILE_INPUTS_JS = """((function collect(doc) {
  let out = [...doc.querySelectorAll('input[type=file]')];
  for (const f of doc.querySelectorAll('iframe,frame')) {
    try { if (f.contentDocument) out = out.concat(collect(f.contentDocument)); } catch (e) {}
  }
  return out;
})(document))"""


FILE_LABELS_JS = f"""{FILE_INPUTS_JS}.map(e => {{
  // label text alone can be a bare "Attach" button (new Greenhouse form): the id/name ("resume", "cover_letter")
  // and the group heading are what tell the fields apart
  const words = s => /resume|r[ée]sum[ée]|\bcv\b|cover/i.test(s || '');
  const lab = [(e.labels && e.labels[0] && e.labels[0].innerText) || '', e.getAttribute('aria-label') || '',
               e.name || '', e.id || ''].filter(Boolean).join(' ');
  let box = e.parentElement, ctx = '';
  for (let i = 0; i < 5 && box && !words(lab); i++, box = box.parentElement) {{
    ctx = (box.innerText || '').trim(); if (words(ctx)) break; }}
  return (lab + ' | ' + ctx.slice(0, 140)).replace(/\\s+/g, ' ').trim();
}})"""


def _pick_input(labels: list[str], which: str) -> int:
    """Best upload field for `which`: a field labelled resume/CV (or cover letter), never an 'autofill from
    resume' / import box, which Ashby & co. put first and which doesn't count as the attachment."""
    want = r"cover" if which == "cover_letter" else r"resume|r[ée]sum[ée]|\bcv\b"
    def score(lab: str) -> int:
        own, _, ctx = lab.lower().partition(" | ")
        # the field's own label / id decides; nearby text only when that names neither resume nor cover letter
        lab = own if re.search(r"resume|r[ée]sum[ée]|\bcv\b|cover", own) else (own + " " + ctx)
        return ((3 if re.search(want, lab) else 0) - (4 if re.search(r"autofill|auto-fill|parse|import|linkedin", lab) else 0)
                - (2 if which != "cover_letter" and "cover" in lab else 0))
    return max(range(len(labels)), key=lambda i: (score(labels[i]), -i)) if labels else 0


async def set_file_input(browser_session, path: str, nth: int | None = None, which: str = "resume") -> tuple[int, str, list[str]]:
    """Set a file on an <input type=file> of the page, any same-origin iframe, or any cross-origin iframe, via
    CDP. nth=None picks the field by its label. Returns (chosen index, its label, all field labels)."""
    frames, _ = await browser_session.get_all_frames()
    sessions, inputs, labels = {}, [], []
    for frame_id in frames:  # one CDP session per target: the page itself + each out-of-process iframe
        try:
            cdp = await browser_session.cdp_client_for_frame(frame_id)
            sessions.setdefault(cdp.session_id, cdp)
        except Exception as e:  # noqa: BLE001
            log.debug("frame %s: %s", frame_id, e)
    for cdp in sessions.values():
        send, sid = cdp.cdp_client.send, cdp.session_id
        try:
            # page's main world (not an isolated world: files set on isolated-world handles don't stick)
            labs = (await send.Runtime.evaluate(params={"expression": FILE_LABELS_JS, "returnByValue": True},
                                                session_id=sid))["result"].get("value") or []
        except Exception as e:  # noqa: BLE001
            log.debug("session %s: %s", sid, e)
            continue
        inputs += [(cdp, i) for i in range(len(labs))]
        labels += labs
    if not inputs:
        raise RuntimeError("no file input on the page; click the Attach/Upload button first, then retry")
    k = _pick_input(labels, which) if nth is None else min(nth, len(inputs) - 1)
    # Greenhouse removes an upload box once it holds a file: re-attaching the resume then "finds" only the cover
    # letter box, and the cover letter replaced the resume ("Resume/CV is required"). Never cross the two.
    own = labels[k].lower().partition(" | ")[0] + " " + labels[k].lower().partition(" | ")[2]
    is_cover = "cover" in own
    is_resume = bool(re.search(r"resume|r[ée]sum[ée]|\bcv\b", own.replace("cover", "")))
    if (which == "cover_letter" and is_resume and not is_cover) or (which != "cover_letter" and is_cover and not is_resume):
        raise RuntimeError(f"the only matching upload field is for the {'resume' if is_resume else 'cover letter'} "
                           f"({labels[k][:60]}), not the {which.replace('_', ' ')}. If the {which.replace('_', ' ')} "
                           "already shows a file name, it is attached: don't re-attach. Otherwise click its "
                           "Attach / Upload button (or remove the old file with its X) and retry.")
    cdp, i = inputs[k]
    send, sid = cdp.cdp_client.send, cdp.session_id
    # setFileInputFiles is a silent no-op unless the DOM domain is enabled and the document requested first
    await send.DOM.enable(session_id=sid)
    await send.DOM.getDocument(params={"depth": -1}, session_id=sid)
    obj = await send.Runtime.evaluate(params={"expression": f"{FILE_INPUTS_JS}[{i}]"}, session_id=sid)
    node = await send.DOM.describeNode(params={"objectId": obj["result"]["objectId"]}, session_id=sid)
    await send.DOM.setFileInputFiles(params={"files": [path], "backendNodeId": node["node"]["backendNodeId"]},
                                     session_id=sid)
    return k, labels[k], labels


def text_to_pdf(text: str, path: str, author: str = "") -> None:
    """A plain one-page-ish PDF of the cover letter (Helvetica 11pt, wrapped, normal margins)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
    from xml.sax.saxutils import escape

    style = ParagraphStyle("body", fontName="Helvetica", fontSize=11, leading=15)
    story = []
    for para in text.split("\n\n"):
        story += [Paragraph(escape(para).replace("\n", "<br/>"), style), Spacer(1, 9)]
    SimpleDocTemplate(path, pagesize=A4, leftMargin=60, rightMargin=60, topMargin=60, bottomMargin=60,
                      title="Cover Letter", author=author).build(story)


_GH_SLUGS: dict[str, str | None] = {}


async def resolve_gh_jid(job: Job) -> str | None:
    """Company careers pages with ?gh_jid=<id> (common on VC boards) often don't render the posting for a bot.
    Greenhouse's legacy embed endpoint redirects a bare job id to the right board's form: use that instead."""
    import httpx

    m = re.search(r"[?&]gh_jid=(\d+)", job.apply_url) or re.search(r"[?&]gh_jid=(\d+)", job.url)
    if not m:
        return None
    jid = m.group(1)
    if jid not in _GH_SLUGS:
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
                r = await c.get(f"https://boards.greenhouse.io/embed/job_app?token={jid}")
            slug = re.search(r"[?&]for=([\w-]+)", str(r.url))
            _GH_SLUGS[jid] = slug.group(1) if r.status_code == 200 and slug else None
        except httpx.HTTPError:
            return None
    return f"https://job-boards.greenhouse.io/embed/job_app?for={_GH_SLUGS[jid]}&token={jid}" if _GH_SLUGS[jid] else None


def direct_apply_url(job: Job) -> str:
    """Greenhouse jobs often redirect to a company page that wraps the form in a blob: iframe no automation can
    reach. Greenhouse's own embed URL serves the bare form for every board."""
    m = re.search(r"greenhouse\.io/([\w-]+)/jobs/(\d+)", job.apply_url) or re.search(r"greenhouse\.io/([\w-]+)/jobs/(\d+)", job.url)
    if m:
        return f"https://job-boards.greenhouse.io/embed/job_app?for={m.group(1)}&token={m.group(2)}"
    return job.apply_url


MAILTO_BLOCK = """
(() => {
  const stop = (href) => {
    window.__jobagentMailto = href;
    // tell the agent (it reads the page, not the console) what the blocked button would have done
    const addr = String(href).replace(/^mailto:/i, '').split('?')[0];
    let note = document.getElementById('jobagent-mailto-note');
    if (!note) {
      note = document.createElement('div');
      note.id = 'jobagent-mailto-note';
      note.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;background:#ffeb3b;color:#000;padding:8px;font:16px sans-serif';
      document.documentElement.appendChild(note);
    }
    note.textContent = 'This Apply button is an email link to ' + addr + '. It was blocked. Apply with the apply_by_email action, to="' + addr + '".';
    return null;
  };
  document.addEventListener('click', (e) => {
    const a = e.target && e.target.closest && e.target.closest('a[href^="mailto:" i]');
    if (a) { e.preventDefault(); e.stopImmediatePropagation(); stop(a.href); }
  }, true);
  const open = window.open;
  window.open = function (url, ...rest) { return String(url || '').toLowerCase().startsWith('mailto:') ? stop(url) : open.call(this, url, ...rest); };
})();
"""


async def block_mailto(session) -> None:
    """mailto: links make even headless Chrome launch the desktop mail app. Cancel them in every page."""
    cdp = await session.get_or_create_cdp_session()
    await cdp.cdp_client.send.Page.enable(session_id=cdp.session_id)
    await cdp.cdp_client.send.Page.addScriptToEvaluateOnNewDocument(params={"source": MAILTO_BLOCK},
                                                                      session_id=cdp.session_id)
    await cdp.cdp_client.send.Runtime.evaluate(params={"expression": MAILTO_BLOCK}, session_id=cdp.session_id)


def notify(title: str, msg: str) -> None:
    """Desktop notification + sound so you notice a captcha in assist mode (macOS / Windows / Linux)."""
    from jobagent.osutil import notify as _notify

    _notify(title, msg)


def _load_login_state(cfg: Config) -> dict | None:
    """Cookies saved by `jobagent login` (read-only copy for the workers), or None if there is no real login."""
    marker = cfg.storage_state_path.with_name("logged_in_sites.json")
    if not (cfg.storage_state_path.exists() and marker.exists()):
        return None
    try:
        state = json.loads(cfg.storage_state_path.read_text())
    except Exception:  # noqa: BLE001
        return None
    # Playwright exports CHIPS cookies with partitionKey as a string; CDP's setCookies wants an object and then
    # rejects the WHOLE batch ("Failed to deserialize params.cookies.partitionKey"), so no site was logged in
    for c in state.get("cookies", []):
        if not isinstance(c.get("partitionKey"), dict):
            c.pop("partitionKey", None)
    return state


def _has_google_session(cfg: Config) -> bool:
    try:
        cookies = json.loads(cfg.storage_state_path.read_text()).get("cookies", [])
    except Exception:  # noqa: BLE001
        return False
    return any(c["domain"].endswith("google.com") and c["name"] in ("SID", "__Secure-1PSID") for c in cookies)


class Applier:
    def __init__(self, cfg: Config, luna: Luna, clef: Clef, otp: OTPProvider, profile: dict, resume_text: str):
        self.cfg, self.luna, self.clef, self.otp = cfg, luna, clef, otp
        self.profile, self.resume_text = profile, resume_text
        self.profile_yaml = yaml.safe_dump(profile, sort_keys=False, allow_unicode=True)
        self._pids: dict[str, int | None] = {}  # job key -> its own Chrome pid (never shared between workers)
        self._mailto: dict[str, list[str]] = {}  # job key -> employer apply addresses seen on its pages
        self.has_google = _has_google_session(cfg)
        self.login_state = _load_login_state(cfg)
        self._profile_mtime = self._profile_path().stat().st_mtime if self._profile_path().exists() else 0.0

    def _profile_path(self):
        return self.cfg.path("profile", "profile.yaml")

    def _refresh_profile(self) -> None:
        """A pass can run for hours: pick up answers added to profile.yaml since it started."""
        try:
            m = self._profile_path().stat().st_mtime
            if m != self._profile_mtime:
                from jobagent.config import load_profile
                prof = load_profile(self.cfg)
                self.profile, self._profile_mtime = prof, m
                self.profile_yaml = yaml.safe_dump(prof, sort_keys=False, allow_unicode=True)
                log.info("profile.yaml changed: reloaded")
        except Exception as e:  # noqa: BLE001  (a half-written or broken file keeps the previous profile)
            log.warning("profile.yaml reload failed, keeping the previous one: %s", e)

    # -----------------------------------------------------------------------------------------
    async def apply(self, job: Job, worker_id: int = 0) -> ApplyResult:
        """Apply through the form; a mailto apply address seen on the way is the fallback when that fails."""
        self._refresh_profile()
        for link in (job.apply_url, job.url):
            if (link or "").lower().startswith("mailto:"):
                return await self._apply_mailto_posting(job, link)
        self._mailto[job.key] = []
        try:
            result = await self._apply_once(job, worker_id)
        finally:
            found = self._mailto.pop(job.key, [])
        # an email can't fix an eligibility block (US-only location, work authorization, citizenship...): skip it
        ineligible = re.search(r"u\.?s\.? (locations?|metro|states?|city)|metropolitan|work authori|right to work|"
                               r"sponsorship|citizenship|ITAR|clearance|arbitration", result.summary or "", re.I)
        if found and result.status in (Status.FAILED, Status.NEEDS_HUMAN) and not ineligible:
            to = found[0]
            if not self.outbox_has(job, to):
                subject, body = await self._application_email(job)
                self.outbox(job, to, subject, body)
                log.info("form route failed for %s; queued email application to %s", job.company, to)
            return ApplyResult(Status.READY, f"application email queued to {to} (form route: {result.summary[:300]})",
                               result.screenshot, {**result.answers, "email_to": to})
        return result

    async def _apply_mailto_posting(self, job: Job, link: str) -> ApplyResult:
        """The posting IS a mailto: link (recipients, subject, often a body template of questions to answer)."""
        from urllib.parse import parse_qs, unquote

        addr, _, query = link[7:].partition("?")
        to = [a.strip().lower() for a in unquote(addr).split(",") if re.fullmatch(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", a.strip())]
        if not to:
            return ApplyResult(Status.NEEDS_HUMAN, f"mailto posting without a usable address: {link[:120]}", None, {})
        q = {k.lower(): unquote(v[0]) for k, v in parse_qs(query).items()}
        template = q.get("body", "").strip()
        ask = ("Answer each prompt of this template from the employer, in order, as short labelled paragraphs, "
               f"keeping their labels:\n{template}") if template else ""
        subject, body = await self._application_email(job, ask)
        subject = q.get("subject") or subject
        self.outbox(job, to[0], subject, body)
        log.info("mailto posting for %s: queued email application to %s", job.company, to[0])
        return ApplyResult(Status.READY, f"application email queued to {to[0]} (posting is an email link)", None,
                           {"email_to": to[0]})

    async def _apply_once(self, job: Job, worker_id: int = 0) -> ApplyResult:
        job.apply_url = await resolve_gh_jid(job) or direct_apply_url(job)
        from browser_use import Agent, BrowserProfile, BrowserSession, Tools
        from browser_use.agent.views import ActionResult

        art = self.cfg.artifacts_dir / re.sub(r"[^\w.-]+", "_", job.key)[:120]
        art.mkdir(parents=True, exist_ok=True)
        resume = str(self.cfg.resume_path)
        cover_path = str(art / f"Cover_Letter_{re.sub(r'\W+', '_', job.company)}.txt")
        cover_pdf = cover_path[:-4] + ".pdf"  # uploads: many forms only accept PDF/DOC
        agent_box: dict = {}  # filled once the Agent exists; tools look it up at call time
        submit = self.cfg.get("apply.submit", True)
        assist = bool(self.cfg.get("apply.assist", False))  # `jobagent assist`: you're at the screen for captchas
        # visible browsers: pause on a captcha, ping you, and give you a few minutes to solve it before flagging
        captcha_wait = 600 if assist else (0 if self.cfg.get("apply.headless", True)
                                           else int(self.cfg.get("apply.captcha_wait_seconds", 0) or 0))
        interactive = captcha_wait > 0
        asked: list[dict] = []
        flagged: dict = {}
        emailed: dict = {}

        tools = Tools()

        @tools.action("Wait for and return the email verification code or link that was just sent to the candidate. "
                      "kind='code' or 'link'. sender_hint: the employer name the FORM itself uses (e.g. 'Why "
                      "Lighthouse?' on a The Hotels Network job -> 'Lighthouse'), when it differs from the job's "
                      "company; never just the ATS name like 'Greenhouse'.")
        async def get_email_verification(browser_session, kind: str = "code", sender_hint: str = "") -> ActionResult:
            # the site sent the email when the agent clicked "send code", often a few steps (minutes) before this
            # call; the helper picks the newest matching email, so a wider window doesn't return a stale code
            v = await self.otp.wait_for_code(job.company, sender_hint, requested_at=time.time() - 240,
                                             want="link" if kind == "link" else "code")
            if not v:
                return ActionResult(error="No verification email arrived within 5 minutes. Try 'resend' once, "
                                          "then call flag_for_human if it still doesn't arrive.")
            if v.startswith("NOTE:"):  # the inbox helper found the site's email, but it has no code / link
                return ActionResult(error=f"No {kind} in the inbox. What the site's latest email says: {v[5:].strip()} "
                                          "Act on that (e.g. use 'Forgot password' or 'Sign in'); don't request "
                                          "another code for the same thing.")
            if v in emailed.setdefault("given", set()):
                # the inbox has nothing newer: re-entering a code the site already took or rejected loops forever
                return ActionResult(error=f"The newest email still holds the {kind} you already got ({v}): no new one "
                                          "has arrived. If the page rejected it or it expired, click 'resend' and wait "
                                          "~30 s before asking again; if it was accepted, carry on. After two resends "
                                          "with no new code, call flag_for_human.")
            emailed["given"].add(v)
            if kind == "link" and v.startswith("http"):
                # open it ourselves: reset / magic links run to 500 chars and get mangled when the model retypes them
                # same tab: magic-login / verify links are usually single-use and continue the flow where they land
                await browser_session.navigate_to(v)
                return ActionResult(extracted_content="Opened the emailed verification / login / reset link in this "
                                    "tab (it is single-use: don't request another one). Continue from the page it opened.",
                                    long_term_memory=f"Opened the emailed {kind} for {job.company}; continue from there")
            hint = ("" if kind == "link" else " Codes are CASE-SENSITIVE: keep every upper/lower-case letter exactly. "
                    "If the code field is split into one box per character, click the FIRST box and type the whole "
                    "code in one input action (the boxes auto-advance). Don't request a new code unless the page says "
                    "this one is wrong or expired.")
            if kind != "link":
                try:
                    entered = await enter_code(browser_session, v)
                except Exception as e:  # noqa: BLE001
                    entered = ""
                    log.debug("enter_code failed: %s", e)
                if entered:
                    return ActionResult(extracted_content=f"Verification code: {v}. {entered}",
                                        long_term_memory=f"Email code for {job.company}: {v} (already entered)")
            return ActionResult(extracted_content=f"Verification {kind}: {v}.{hint}",
                                long_term_memory=f"Email {kind} for {job.company}: {v}")

        @tools.action("Get a truthful answer to an application question from the candidate's profile and resume. "
                      "Pass the exact question, the options if it's a select/radio, and a character limit if shown.")
        async def answer_question(question: str, options: list[str] | None = None, max_chars: int = 0) -> ActionResult:
            ans = await self._answer(job, question, options, max_chars)
            asked.append({"question": question, "answer": ans})
            if ans.startswith("UNKNOWN"):
                self._remember_unknown(question, options)
            # long_term_memory is what the agent sees in later steps: give it the whole answer, not a preview
            return ActionResult(extracted_content=ans, long_term_memory=f"Answer to {question[:120]!r}:\n{ans}")

        @tools.action("Choose an option in a dropdown / select box using real keystrokes (focus, type the option, "
                      "Enter). Use this for Greenhouse 'Select...' boxes and any dropdown that shows your choice but still "
                      "says 'required'. question = a few words of the question text; option = the exact option text.")
        async def pick_option(question: str, option: str, browser_session) -> ActionResult:
            try:
                msg = await pick_combo_option(browser_session, question, option)
            except Exception as e:  # noqa: BLE001
                return ActionResult(error=f"pick_option failed: {e}")
            return ActionResult(extracted_content=msg + ". Check the field's 'required' error is gone.")

        @tools.action("Type into a text field with real key presses (clears it first, then Tab). Use when a field "
                      "shows your text but the form still says it's required/invalid (phone numbers, React forms), or "
                      "when the normal input action doesn't stick. index = the field's element index.")
        async def type_like_keyboard(index: int, text: str, browser_session) -> ActionResult:
            try:
                msg = await type_into(browser_session, index, text)
            except Exception as e:  # noqa: BLE001
                return ActionResult(error=f"type_like_keyboard failed: {e}")
            return ActionResult(extracted_content=msg)

        @tools.action("Attach the resume (or cover letter) to a file-upload field on the current page. Works for "
                      "hidden inputs, custom 'Attach' buttons and forms inside iframes. Prefer this over upload_file. "
                      "which='resume' or 'cover_letter'. Leave nth empty: the field is picked by its label (it skips "
                      "'autofill from resume' boxes); pass nth only to override with a field index from a previous result.")
        async def attach_file(browser_session, which: str = "resume", nth: int | None = None) -> ActionResult:
            path = cover_pdf if which == "cover_letter" else resume
            if not Path(path).exists():
                return ActionResult(error=f"{which} file does not exist yet (call write_cover_letter first)")
            try:
                k, lab, labels = await set_file_input(browser_session, path, nth, which)
            except Exception as e:  # noqa: BLE001
                return ActionResult(error=f"attach_file failed: {e}")
            fields = "; ".join(f"[{i}] {l[:80]}" for i, l in enumerate(labels))
            return ActionResult(extracted_content=f"Attached {Path(path).name} to upload field [{k}] ({lab[:80]}). "
                                f"All upload fields: {fields}. Check the page now shows the file name in the right "
                                "field; if not, call attach_file again with nth set to the right field.",
                                long_term_memory=f"Attached {which} to field [{k}] {lab[:60]}")

        @tools.action("Apply by email, ONLY when the posting says to send your resume/application to an email "
                      "address (no form). `to` must be an address shown on the posting. `what_they_ask`: copy any "
                      "instructions for the email (e.g. 'describe a system you shipped, what broke'). Queues an email "
                      "with the resume attached; then finish with status 'applied'.")
        async def apply_by_email(to: str, browser_session, what_they_ask: str = "") -> ActionResult:
            to = to.strip().strip("<>").lower()
            if not re.fullmatch(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", to):
                return ActionResult(error=f"not an email address: {to!r}")
            page_text = ""
            try:
                cdp = await browser_session.get_or_create_cdp_session()
                r = await cdp.cdp_client.send.Runtime.evaluate(
                    params={"expression": "document.body.innerText + ' ' + [...document.querySelectorAll("
                            "'a[href^=\"mailto:\" i]')].map(a => decodeURIComponent(a.href)).join(' ') + ' ' + "
                            "(window.__jobagentMailto || '')", "returnByValue": True}, session_id=cdp.session_id)
                page_text = (r["result"].get("value") or "").lower()
            except Exception:  # noqa: BLE001
                pass
            # only addresses the employer actually published (a malicious page can't redirect your application)
            published = " ".join([job.description or "", job.url or "", job.apply_url or ""]).lower()
            if to not in page_text.replace("jobagent-mailto-note", "") and to not in published:
                return ActionResult(error=f"{to} does not appear on the posting; refusing to email it")
            subject, body = await self._application_email(job, what_they_ask)
            self.outbox(job, to, subject, body)
            emailed["to"] = to
            return ActionResult(extracted_content=f"Application email to {to} queued (subject: {subject}).",
                                long_term_memory=f"Queued application email to {to}")

        @tools.action("Write a tailored cover letter for this job. Returns the text and a .txt file path you can upload.")
        async def write_cover_letter() -> ActionResult:
            text = await self._cover_letter(job)
            Path(cover_path).write_text(text)
            text_to_pdf(text, cover_pdf, self.profile.get("personal", {}).get("full_name", ""))
            # only now may the built-in upload_file use these paths (listed up front, the agent uploaded them unwritten)
            a = agent_box.get("agent")
            if a is not None:
                a.available_file_paths = [*(a.available_file_paths or []), cover_pdf, cover_path]
            return ActionResult(extracted_content=f"Upload file (attach_file which='cover_letter'): {cover_pdf}\n"
                                                  f"Text (for a paste box):\n\n{text}",
                                long_term_memory=f"Cover letter (file {cover_path}):\n{text}")

        @tools.action("Stop and hand this application to the human (captcha, login wall, unanswerable required "
                      "question, broken form). Give a precise reason.")
        async def flag_for_human(reason: str) -> ActionResult:
            flagged["reason"] = reason
            return ActionResult(is_done=True, success=False, extracted_content=f"Flagged for human: {reason}")

        if interactive:
            @tools.action("A captcha / bot check / human verification is blocking you. The human is watching this "
                          "visible browser and will solve it. Call this, then continue once it returns.")
            async def wait_for_human(reason: str, browser_session) -> ActionResult:
                notify(f"Captcha: {job.company} (window w{worker_id})", f"Solve it in that Chrome window. {reason}")
                try:  # raise this window so you can find it among the others
                    cdp = await browser_session.get_or_create_cdp_session()
                    await cdp.cdp_client.send.Page.bringToFront(session_id=cdp.session_id)
                except Exception:  # noqa: BLE001
                    pass
                deadline = time.monotonic() + captcha_wait
                while time.monotonic() < deadline:
                    await asyncio.sleep(6)
                    img = await browser_session.take_screenshot()
                    a = await self.clef.ask("clef", {"waiting_for": reason}, {"blocked": {"type": "noul",
                        "instructions": "Is a captcha, bot check, or human-verification challenge still visible?"}},
                        images=[img])
                    if a["blocked"]["noul"] < 0.3:
                        return ActionResult(extracted_content="The human cleared the challenge. Continue.")
                flagged["reason"] = f"captcha: not solved within {captcha_wait // 60} min: {reason}"
                return ActionResult(is_done=True, success=False, extracted_content=flagged["reason"])

        try:
            _pf = self.cfg.db_path.with_name("paused_hosts.json")
            paused_now = sorted(h for h, until in json.loads(_pf.read_text()).items() if until > time.time()) \
                if _pf.exists() else []
        except Exception:  # noqa: BLE001
            paused_now = []
        task = (
            RULES.format(submit_rule=SUBMIT if submit else DRY_RUN,
                         captcha_rule=INVISIBLE_CAPTCHA + (CAPTCHA_ASSIST if interactive else CAPTCHA_HEADLESS),
                         **candidate_facts(self.profile))
            + ("\nGOOGLE SESSION: available (rule 6b applies).\n" if self.has_google else
               "\nGOOGLE SESSION: none. Never click Sign in with Google; create an email + password account instead "
               "(rule 6). If Google sign-in is the ONLY way in, flag_for_human with reason 'google login needed'.\n")
            + (f"\nPAUSED SITES (they flagged recent submissions; another one now hurts the candidate): "
               f"{', '.join(paused_now)}. If the application form turns out to be on one of these (directly, via the "
               "company careers page, or embedded), do NOT fill or submit it: call flag_for_human with reason "
               "'deferred: <site>'.\n" if paused_now else "")
            + f"\nJOB\nCompany: {job.company}\nTitle: {job.title}\nLocation: {job.location}\n"
            f"Posting: {job.url}\nApply at: {job.apply_url}\n"
            f"Description:\n{job.description or ''}\n"
            f"\nRESUME FILE PATH (for uploads): {resume}\n"
            f"\nCANDIDATE PROFILE (YAML)\n{self.profile_yaml}\n"
            f"\nRESUME TEXT\n{self.resume_text}\n"
            f"\nStart at {job.apply_url}. Finish with the done action and the structured outcome."
        )

        dom = host_of(job.apply_url)
        storage = self.cfg.storage_state_path
        # Captcha avoidance: real Google Chrome (not bundled Chromium), one persistent profile per worker so
        # cookies and reCAPTCHA trust build up over time, human-ish pacing, and per-site spacing (orchestrator).
        chrome = chrome_path(self.cfg)  # apply.chrome_path, else the OS's usual Chrome install
        headless = self.cfg.get("apply.headless", True) and dom not in set(self.cfg.get("apply.headful_hosts", []) or [])
        bprofile = BrowserProfile(
            headless=headless,
            executable_path=chrome if chrome and Path(chrome).exists() else None,
            user_data_dir=str(self.cfg.path("logins.worker_profiles", "browser_profiles/workers") / f"w{worker_id}"),
            # job-board logins from `jobagent login`, passed as a dict: given a file path, browser-use writes every
            # worker's cookies back into that shared file (it grew to 4.5 MB and froze the daemon)
            storage_state=self.login_state,
            window_size={"width": 1280, "height": 820},
            # visible mode: cascade windows so every title bar shows; click one to bring it to the front
            window_position=None if headless else {"width": 30 + (worker_id % 10) * 22 + (worker_id // 10) * 220,
                                                    "height": 30 + (worker_id % 10) * 14},
            downloads_path=str(art / "downloads"),
            keep_alive=True,  # keep the browser open after run() so we can screenshot; killed in finally
            minimum_wait_page_load_time=self.cfg.get("apply.page_wait", 0.3),
            wait_between_actions=self.cfg.get("apply.action_wait", 0.15),
            highlight_elements=False,
            cross_origin_iframes=True,  # embedded Greenhouse/Lever forms live in cross-origin iframes
        )
        session = BrowserSession(browser_profile=bprofile)
        agent = Agent(
            task=task,
            llm=browser_llm(self.cfg),
            fallback_llm=browser_fallback_llm(self.cfg),  # llm.fallback_model / azure.fallback_deployment, or None
            browser_session=session,
            tools=tools,
            available_file_paths=[resume],
            # a throwaway password used only for ATS accounts. Not domain-scoped: application flows hop from the
            # posting to the employer's ATS / SSO domains mid-run (SuccessFactors, Taleo...), and a scoped secret
            # makes the agent type the literal placeholder there
            sensitive_data=({"x_ats_password": os.environ["ATS_PASSWORD"],
                             **({"x_ats_password_short": os.environ["ATS_PASSWORD_SHORT"]}
                                if os.environ.get("ATS_PASSWORD_SHORT") else {})}
                            if os.environ.get("ATS_PASSWORD") else None),
            output_model_schema=Outcome,
            use_vision=True,
            vision_detail_level=self.cfg.get("apply.vision_detail", "low"),  # DOM gives exact fields; image is for layout
            flash_mode=self.cfg.get("apply.flash_mode", True),  # no per-step evaluate/plan/memory prose: ~2x fewer tokens
            max_actions_per_step=self.cfg.get("apply.max_actions_per_step", 12),  # fill a whole form section per step
            save_conversation_path=str(art / "conversation"),
            max_failures=4,
            use_judge=False,
            enable_signal_handler=False,
            step_timeout=300,
            max_clickable_elements_length=400000,  # luna has a 1M context: show the whole form
        )
        agent_box["agent"] = agent
        shot = str(art / "final.png")
        try:
            if early := await self._preflight(job, session, art):
                return early
            history = await agent.run(max_steps=self.cfg.get("apply.max_steps", 250))
            # what the agent saw in its last steps: a confirmation toast can be gone by the time of the final shot
            step_shots = [x for x in history.screenshot_paths(n_last=3, return_none_if_not_screenshot=False)
                          if x and Path(x).exists()]
            try:
                await session.take_screenshot(path=shot, full_page=False)
            except Exception:  # noqa: BLE001
                shot = None
            outcome: Outcome | None = None
            if not flagged:  # flag_for_human ends the run with plain text, not the structured outcome
                try:
                    outcome = history.structured_output
                except Exception as e:  # noqa: BLE001 - malformed final JSON: judge from the screenshot instead
                    log.warning("unparseable outcome for %s: %s", job.key, str(e)[:200])
        except Exception as e:  # noqa: BLE001
            log.exception("agent crashed on %s", job.key)
            return ApplyResult(Status.FAILED, f"agent error: {e}")
        finally:
            try:
                await asyncio.wait_for(self._capture_mailto(job, session), timeout=60)
            except Exception:  # noqa: BLE001
                pass
            try:
                await asyncio.wait_for(session.kill(), timeout=30)
            except Exception:  # noqa: BLE001
                pass
            # never trust kill() alone: verify the browser process tree is gone, force-kill it if not
            browsers.ensure_dead(self._pids.pop(job.key, None) or browsers.session_pid(session), job.key)

        if emailed.get("to"):  # becomes APPLIED (and lands in APPLIED_JOBS.md) only when the outbox actually sends it
            return ApplyResult(Status.READY, f"application email queued to {emailed['to']}", shot,
                               {"asked": asked, "email_to": emailed["to"]})
        if flagged:
            if flagged["reason"].startswith("deferred:"):  # reached a paused site mid-run: retry once it reopens
                return ApplyResult(Status.QUEUED, flagged["reason"], shot, {"asked": asked})
            return ApplyResult(Status.NEEDS_HUMAN, flagged["reason"], shot, {"asked": asked})
        if outcome is None:
            return ApplyResult(Status.FAILED, f"no outcome (steps exhausted?) {history.final_result() or ''}"[:500], shot,
                               {"asked": asked, "errors": [e for e in history.errors() if e][-3:]})

        status = {"applied": Status.APPLIED, "ready": Status.READY, "needs_human": Status.NEEDS_HUMAN,
                  "failed": Status.FAILED, "closed": Status.SKIPPED, "not_eligible": Status.REJECTED}[outcome.status]
        text = f"{outcome.summary} {outcome.blocker}"
        if status == Status.FAILED and BOT_CHECK.search(text):
            status = Status.NEEDS_HUMAN  # a human in `jobagent assist` gets past these; retrying headless won't
        result = ApplyResult(status, outcome.summary + (f" | blocker: {outcome.blocker}" if outcome.blocker else ""),
                             shot, {"asked": asked, "questions": [q.model_dump() for q in outcome.questions]})
        if ALREADY_APPLIED.search(text):
            # an earlier attempt (e.g. one cut off by a restart) went through: the application exists, record it
            result.status, result.summary = Status.APPLIED, f"already applied (earlier attempt): {result.summary}"
            return result
        if status == Status.APPLIED:
            await self._verify(job, result, step_shots)
        return result

    # -----------------------------------------------------------------------------------------
    PAGE_KINDS = {
        "apply_form": "An application form, or a job page with an Apply button",
        "job_closed": "The posting says it is closed, filled, expired or no longer accepting applications",
        "not_found": "404 / page not found / job does not exist / redirected to a generic careers home page",
        "login_wall": "Must sign in or create an account before anything else is visible",
        "captcha": "A captcha or bot-check challenge blocks the page",
        "other": None,
    }

    async def _preflight(self, job: Job, session, art: Path) -> ApplyResult | None:
        """Look at the landing page with clef before spending a whole browser-agent run on it."""
        try:
            await session.start()
            self._pids[job.key] = browsers.register(session, job.key)  # per job: workers share this Applier
            await block_mailto(session)
            await session.navigate_to(job.apply_url)
            await asyncio.sleep(2)
            await self._capture_mailto(job, session)
            img = await session.take_screenshot(path=str(art / "landing.png"))
            url = await session.get_current_page_url()
            # the apply link redirected onto a site that is paused (it flagged us recently): defer, don't submit
            landed = host_of(url)
            try:
                paused_file = self.cfg.db_path.with_name("paused_hosts.json")
                paused = {h for h, until in json.loads(paused_file.read_text()).items() if until > time.time()} \
                    if paused_file.exists() else set()
            except Exception:  # noqa: BLE001
                paused = set()
            frames_html = ""
            try:
                cdp = await session.get_or_create_cdp_session()
                r = await cdp.cdp_client.send.Runtime.evaluate(params={"expression": "[...document.querySelectorAll("
                    "'iframe')].map(f => f.src).join(' ')", "returnByValue": True}, session_id=cdp.session_id)
                frames_html = r["result"].get("value") or ""
            except Exception:  # noqa: BLE001
                pass
            hit = next((h for h in paused if landed == h or landed.endswith("." + h) or h in frames_html), None)
            if hit:
                return ApplyResult(Status.QUEUED, f"deferred: lands on paused site {hit}", None, {"paused_via": hit})
            # the apply link redirected onto a site that always ends in a captcha: hand it to `jobagent assist`
            # now instead of after a full agent run
            if not self.cfg.get("apply.assist", False) and any(
                    landed == h or landed.endswith("." + h) for h in self.cfg.get("apply.assist_only_hosts", []) or []):
                return ApplyResult(Status.NEEDS_HUMAN, f"captcha: redirected to {landed} (always a captcha; use "
                                   "`jobagent assist`)", str(art / "landing.png"), {"landed": url})
            a = await self.clef.ask(self.cfg.get("cloudflare.verify_model", "clef"),
                                    {"expected_job": f"{job.title} at {job.company}", "current_url": url},
                                    {"page": {"type": "choice", "instructions": "What is shown on this page?",
                                              "criteria": self.PAGE_KINDS}},
                                    images=[img])
        except Exception as e:  # noqa: BLE001 - preflight is an optimisation; let the agent try
            log.debug("preflight skipped for %s: %s", job.key, e)
            return None
        page = a["page"]
        if page.get("confidence", 0) < 0.5 or page["probabilities"][page["choice"]] < 0.75:
            return None
        kind = page["choice"]
        if kind == "job_closed":
            return ApplyResult(Status.SKIPPED, f"preflight: {kind} ({url})", str(art / "landing.png"), {"preflight": a})
        # not_found: the link is dead, not necessarily the job; the agent tries the company careers page (rule 13)
        # login_wall on an employer portal: the agent can create an account (rule 6); only job-board logins need you
        login_board = any(host_of(url).endswith(h) for h in self.cfg.get("apply.login_hosts", []) or [])
        if kind == "login_wall" and login_board and not self.cfg.storage_state_path.exists():
            return ApplyResult(Status.NEEDS_HUMAN, "preflight: login wall; run `jobagent login`", str(art / "landing.png"),
                               {"preflight": a})
        return None  # apply_form / captcha checkbox / login with saved cookies: let the agent handle it

    async def _verify(self, job: Job, result: ApplyResult, step_shots: list[str] | None = None) -> None:
        """Don't trust the agent's own report: ask clef whether the final screen (or the screens of the agent's
        last steps, for confirmations that disappear) shows a submission."""
        shots = [x for x in [*(step_shots or [])[-2:], result.screenshot] if x and Path(x).exists()]
        if not shots:
            return
        try:
            a = await self.clef.ask(
                self.cfg.get("cloudflare.verify_model", "clef"),
                {"company": job.company, "agent_summary": result.summary},
                {
                    "submitted": {"type": "noul", "instructions": "The screenshots are the last screens of the "
                                  "application, oldest first. Does any of them show the job application was successfully "
                                  "submitted (confirmation / thank-you message, or 'applied' state)?"},
                    "error": {"type": "noul", "instructions": "Does the LAST screenshot show a validation error, a "
                              "required field still empty, a captcha, or a login wall?"},
                },
                images=[Path(x).read_bytes() for x in shots],
            )
        except Exception as e:  # noqa: BLE001
            log.warning("verify failed for %s: %s", job.key, e)
            return
        result.answers["verify"] = a
        sub, err = a["submitted"]["noul"], a["error"]["noul"]
        # a confirmation the agent quoted word for word, on a clean final screen, counts even when the screenshot
        # no longer shows it (toasts / redirects); a bare claim without a quote does not (see the Tudip case)
        quoted = bool(CONFIRM_QUOTE.search(result.summary or ""))
        if (sub < 0.35 and not (quoted and sub >= 0.05 and err < 0.2)) or err > 0.6:
            result.status = Status.NEEDS_HUMAN
            result.summary = f"UNVERIFIED (clef submitted={a['submitted']['noul']:.2f}, error={a['error']['noul']:.2f}): {result.summary}"

    def _history_with(self, job: Job) -> str:
        """What this agent has on record with the company (the only application history it can vouch for)."""
        import sqlite3

        from jobagent.models import company_key

        target = company_key(job.company)
        conn = sqlite3.connect(self.cfg.db_path)
        try:
            roles = [t for c, t in conn.execute("SELECT company, title FROM jobs WHERE status='applied'")
                     if company_key(c or "") == target]
        finally:
            conn.close()
        return (f"Applied earlier (via this agent) to: {', '.join(roles)}" if roles else
                "No earlier application or interview with this company on record.")

    async def _answer(self, job: Job, question: str, options: list[str] | None, max_chars: int) -> str:
        ans = (await self.luna.chat(
            "You fill job applications for the candidate. Answer ONLY from the profile and resume; never invent "
            "facts. Choice questions: reply with one option exactly as written. Profile `custom_answers` override "
            "everything. If the answer genuinely isn't derivable (e.g. an unlisted salary, a personal fact), reply "
            "exactly 'UNKNOWN: <why>'. But don't use UNKNOWN when a truthful answer exists: 'How did you hear / "
            "Source / Referral' -> pick the closest option (job board, company website, LinkedIn, other); 'Have you "
            "built/used/done X?' -> 'Yes' plus one concrete line if the resume shows it, otherwise a plain 'No'; "
            "'Are you comfortable / willing to ...' -> answer from the profile, defaulting to Yes for normal job "
            "conditions (on-site days, travel, shifts) unless the profile says otherwise. 'Are/were you employed by "
            "<company or its group/portfolio/affiliates>?' -> 'No' unless that company is in the experience list. Behavioral 'tell us about a "
            "time' questions: use a story from profile `stories` if one fits; otherwise answer from a REAL resume project "
            "that matches the theme: what they built, the problem, how they approached it and what shipped, all as the "
            "resume states it. Never invent events, deadlines, conflicts, people or numbers that aren't in the "
            "profile/resume (say 'a tight timeline' only if the resume supports it). UNKNOWN only if nothing relates. "
            "'Have you previously applied / interviewed / worked here?' -> use COMPANY HISTORY ('No' when it says "
            "nothing is on record), except interview-history questions at Anthropic, which stay UNKNOWN. "
            "'How much did our blog / podcast / event / content influence you?' or 'Did you attend / read X?' -> "
            "the lowest option ('Not at all' / 'No'), since none of it was part of this application. "
            "'Do you meet the minimum / basic qualifications?' -> compare the posting's stated minimums (years, "
            "degree, specific skills) with the profile and resume: 'Yes' only if every stated minimum is met, else "
            "'No' (never UNKNOWN for this). "
            "Short factual questions: answer in a few words, like a person typing fast. "
            "Motivation/essay questions: follow the WRITING STYLE.\n\nWRITING STYLE\n" + HUMAN_STYLE,
            f"PROFILE:\n{self.profile_yaml}\n\nRESUME:\n{self.resume_text}\n\nCOMPANY HISTORY: {self._history_with(job)}\n\n"
            f"JOB: {job.title} at {job.company}\n"
            f"{job.description or ''}\n\nQUESTION: {question}\n"
            + (f"OPTIONS: {json.dumps(options)}\n" if options else "")
            + (f"LIMIT: {max_chars} characters\n" if max_chars else ""),
            max_tokens=32000,  # reasoning tokens count against this
        )).strip()
        if options or ans.startswith("UNKNOWN") or len(ans) < 120:
            return ans
        # essay answers: rewrite once if stock AI phrasing slipped through
        if bad := tells(ans):
            ans = (await self.luna.chat(
                "Rewrite this job-application answer so it sounds like the candidate typed it themselves. Keep every "
                f"fact, drop these giveaway phrases: {', '.join(bad)}.\n\nWRITING STYLE\n" + HUMAN_STYLE,
                ans, max_tokens=16000)).strip()
        return clean(ans)[: max_chars or None]

    async def _cover_letter(self, job: Job) -> str:
        """Draft with the Azure model; clef rejects generic or AI-sounding letters (up to 3 rewrites)."""
        best, best_score, feedback = "", -1.0, ""
        for _ in range(4):
            text = clean(await self._draft_cover_letter(job, feedback))
            try:
                a = await self.clef.ask(self.cfg.get("cloudflare.verify_model", "clef"),
                                        {"job": f"{job.title} at {job.company}\n{job.description or ''}",
                                         "cover_letter": text},
                                        {"specific": {"type": "score", "instructions": "How specific is the cover "
                                                      "letter to this job and grounded in concrete projects?",
                                                      "criteria": ["generic template", "somewhat tailored",
                                                                   "tailored", "specific with concrete evidence"]},
                                         "human": HUMAN_QUESTION,
                                         "claims_ok": {"type": "noul", "instructions": "Is the letter free of "
                                                       "placeholders like [Company] and of unverifiable hype?"}})
                spec, human = a["specific"]["score"] / 3, a["human"]["score"] / 3
                score = spec * human * a["claims_ok"]["noul"] * (0.5 if tells(text) else 1.0)
            except Exception:  # noqa: BLE001
                return text
            if score > best_score:
                best, best_score = text, score
            if score >= 0.5 and human >= 0.67 and not tells(text):
                break
            feedback = ("Previous draft " + ("read as AI-written or templated. " if human < 0.67 else "")
                        + ("was too generic: name the company's actual product/problem. " if spec < 0.67 else "")
                        + (f"used giveaway phrases: {', '.join(tells(text))}. " if tells(text) else "")
                        + "Write it again, more like a person.")
        return best

    async def _draft_cover_letter(self, job: Job, feedback: str = "") -> str:
        return (await self.luna.chat(
            "Write a short cover letter (130-220 words) from the candidate. Focus on the value they bring to THIS "
            "company: what they'd build or fix for them, backed by the 2 resume projects that best prove they can. Plain text, no placeholders. Start "
            "with 'Hi <team or hiring manager name if given>,' and end with 'Thanks,\n" + first_name(self.profile) + "'.\n\nWRITING STYLE\n"
            + HUMAN_STYLE + (f"\n\n{feedback}" if feedback else ""),
            f"PROFILE:\n{self.profile_yaml}\n\nRESUME:\n{self.resume_text}\n\n"
            f"JOB: {job.title} at {job.company} ({job.location})\n{job.description or ''}",
            max_tokens=32000,
        )).strip()

    MAILTO_SCAN = """(() => {
      const out = [];
      if (window.__jobagentMailto) out.push({href: String(window.__jobagentMailto), ctx: '(the Apply button itself)'});
      document.querySelectorAll('a[href^="mailto:" i]').forEach(a => {
        const ctx = ((a.closest('section,div,p,li') || a).innerText || '').slice(0, 600);
        const t = (a.innerText + ' ' + ctx).toLowerCase();
        if (/apply|application|resume|cv|career|hiring|jobs?@|join/.test(t + ' ' + a.href.toLowerCase())) out.push({href: a.href, ctx});
      });
      return out;
    })()"""

    async def _capture_mailto(self, job: Job, session) -> None:
        """Remember every employer apply address the browser sees (mailto links / blocked mailto clicks); `apply`
        emails the first one if the form route fails. clef checks an address really is where this employer takes
        applications (not a job board's / resume site's contact), unless it's careers@-style on their own domain."""
        cdp = await session.get_or_create_cdp_session()
        r = await cdp.cdp_client.send.Runtime.evaluate(params={"expression": self.MAILTO_SCAN, "returnByValue": True},
                                                       session_id=cdp.session_id)
        page = await session.get_current_page_url()
        for link in r["result"].get("value") or []:
            to = link["href"].split(":", 1)[1].split("?")[0].strip().lower()
            if not re.fullmatch(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", to) or re.search(r"privacy|gdpr|press|media|abuse|legal|security@", to):
                continue
            if self.outbox_has(job, to):
                continue
            if self._own_hiring_address(job, to):
                p_yes = 1.0  # careers@/jobs@/hr@ on the company's own domain: no model needed
            else:
              try:
                a = await self.clef.ask(self.cfg.get("cloudflare.triage_model", "clef-flash"),
                    {"hiring_company": job.company, "role": job.title, "page_url": page, "email": to,
                     "text_around_link": link.get("ctx", "")},
                    {"apply_here": {"type": "noul", "instructions": "Is this email address where applicants for this "
                     "role at the hiring company should send their application? No if it belongs to a job board, "
                     "resume/recruiting platform or website operator (a contact/support address), or to another company."}})
                p_yes = a["apply_here"]["noul"]
              except Exception as e:  # noqa: BLE001
                log.warning("mailto check failed for %s (%s): not queuing %s", job.company, e, to)
                continue
            if p_yes < 0.6:
                log.info("ignored mailto for %s -> %s (not the employer's apply address, p=%.2f)", job.company, to, p_yes)
                continue
            seen = self._mailto.setdefault(job.key, [])
            if to not in seen:
                seen.append(to)
                log.info("found mailto apply for %s -> %s (used if the form route fails)", job.company, to)

    @staticmethod
    def _own_hiring_address(job: Job, to: str) -> bool:
        local, domain = to.split("@", 1)
        if not re.match(r"(careers?|jobs?|hir(e|ing)\w*|talent|recruit\w*|hr|people|join(us)?|apply|resumes?|cv)\b", local):
            return False
        words = [w for w in re.findall(r"[a-z0-9]+", normalize_company(job.company)) if len(w) >= 3]
        # the company's own domain, or its per-company ATS inbox (apply@tether.recruitee-inbox.com)
        labels = [l for l in domain.split(".")[:-1] if len(l) >= 3]
        return any(w in l or l in w for w in words for l in labels) if words else False

    def outbox_has(self, job: Job, to: str) -> bool:
        import sqlite3

        conn = sqlite3.connect(self.cfg.db_path)
        try:
            return bool(conn.execute("SELECT 1 FROM outbox WHERE to_addr=? AND company=?", (to, job.company)).fetchone())
        finally:
            conn.close()

    async def _application_email(self, job: Job, what_they_ask: str = "") -> tuple[str, str]:
        if what_they_ask:
            letter = await self._answer(job, "Write the body of the application email. It must directly answer what "
                                        f"the posting asks applicants to send: {what_they_ask}. 120-250 words, start "
                                        "with 'Hi,' and end with 'Thanks,\n" + first_name(self.profile) + "'.", None, 0)
        else:
            letter = await self._cover_letter(job)
        p = self.profile.get("personal", {})
        subject = f"Application for {job.title} - {p.get('full_name', '')}".strip(" -")
        sig = "\n".join(x for x in [p.get("full_name"), p.get("phone_country_code", "") + " " + p.get("phone", ""),
                                     p.get("email"), p.get("linkedin"), p.get("github")] if x and x.strip())
        main = re.split(r"\n(Thanks|Regards|Best)[,!]?\s*\n", letter)[0].rstrip()
        # drop any sentence of the letter that talks about an attachment ("I've attached my CV...", "Please find my
        # resume enclosed"): the email adds exactly one resume line itself
        att = re.compile(r"\b(attach\w*|enclos\w*|find (my|the) (resume|cv))\b", re.I)
        paras = []
        for para in main.split("\n\n"):
            kept = [x for x in re.split(r"(?<=[.!?])\s+", para) if not (att.search(x) and re.search(r"\b(resume|cv|r[ée]sum[ée])\b", x, re.I))]
            if kept:
                paras.append(" ".join(kept))
        main = "\n\n".join(paras).strip()
        if not re.match(r"(hi|hello|dear)\b", main, re.I):
            main = "Hi,\n\n" + main
        body = main + "\n\nMy resume is attached.\n\nThanks,\n" + sig
        return subject, body

    def outbox(self, job: Job, to: str, subject: str, body: str) -> None:
        import sqlite3
        from jobagent.db import now

        conn = sqlite3.connect(self.cfg.db_path, isolation_level=None)
        try:
            if conn.execute("SELECT 1 FROM outbox WHERE to_addr=? OR company=?", (to, job.company)).fetchone():
                return  # one application email per company (and per inbox): more reads as spam
            conn.execute("INSERT INTO outbox(job_key,company,to_addr,subject,body,attachment,created_at) "
                         "VALUES(?,?,?,?,?,?,?)", (job.key, job.company, to, subject, body, str(self.cfg.resume_path), now()))
        finally:
            conn.close()

    def _remember_unknown(self, question: str, options: list[str] | None) -> None:
        """Collect unanswerable questions so you can answer them once in profile.yaml (custom_answers)."""
        p = self.cfg.root / "unanswered_questions.yaml"
        data = yaml.safe_load(p.read_text()) if p.exists() else {}
        data = data or {}
        data.setdefault(question.strip(), {"options": options or [], "answer": ""})
        p.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
