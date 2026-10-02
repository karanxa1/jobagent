# jobagent

An AI agent that finds AI-engineering jobs across dozens of sources, filters them, and applies to them with
parallel browser agents. It fills real application forms (Greenhouse, Lever, Workday, Ashby, Oracle/Taleo,
iCIMS, SuccessFactors, Wellfound and more) the way a careful person would: it uploads your resume, writes
answers and cover letters in your voice, and reads email verification codes. It also creates portal accounts
when a site requires one.

It is built to be **truthful**. Every answer comes from your `profile.yaml` and your resume. When a required
question can't be answered from them, the job is set aside for you instead of guessed.

> Defaults target **AI / LLM engineering roles** for a candidate **based in India** (Naukri, Instahyre, Indian
> phone formats, INR salary fields). Everything candidate-specific lives in `profile.yaml` and `config.yaml`,
> so you can point it at other roles and countries. See [Customising](#customising).

---

## Contents
- [How it works](#how-it-works)
- [What it does well and what it won't do](#what-it-does-well-and-what-it-wont-do)
- [Requirements](#requirements)
- [Setup guide](#setup-guide)
- [Setup prompt for an AI coding agent](#setup-prompt-for-an-ai-coding-agent)
- [Running it](#running-it)
- [Daily workflow](#daily-workflow)
- [Configuration reference](#configuration-reference)
- [Customising](#customising)
- [Troubleshooting](#troubleshooting)
- [Project layout](#project-layout)
- [Responsible use](#responsible-use)
- [License](#license)

---

## How it works

```
sources ─ ATS boards (Greenhouse, Lever, Ashby, Workable, SmartRecruiters…), YC Work at a Startup,
          VC portfolio job boards (a16z, Accel, Sequoia, General Catalyst…), LinkedIn, Naukri, Wellfound,
          Instahyre, Hirist, Cutshort, remote boards (Himalayas, Arbeitnow…), Hacker News "Who is hiring"
   │  discover   all sources concurrently (httpx), deduplicated by company + title
   ▼
jobs.db (SQLite)
   │  triage     Cloudflare Workers AI "clef-flash" scores every posting: AI role? can this candidate apply
   │             (location / visa)? seniority? fit? still open?  Borderline ones get a second opinion from "clef".
   ▼
queue ── N workers ──► one real Google Chrome per worker, driven by an Azure OpenAI model through browser-use
                       tools: answer_question · write_cover_letter · attach_file · pick_option (React dropdowns)
                              type_like_keyboard · get_email_verification · apply_by_email · flag_for_human
                       per-site concurrency, gaps between starts, daily caps, a RAM guard
   │  verify     clef looks at the final screenshot: was it really submitted?
   ▼
applied ─ recorded in APPLIED_JOBS.md (never applies twice to the same role)
ready   ─ email application queued (sent by Gmail SMTP or a Claude helper)
needs_human ─ captcha, unknown question, login needed… (with the exact reason)
failed / rejected / skipped
```

- **Discovery** runs every `daemon.interval_minutes` (default 3 h). Applying runs continuously alongside it.
- **Triage** uses probability-returning decision models, so all the thresholds are in `config.yaml`. You can
  re-apply new thresholds without new model calls (`jobagent rescore`).
- **Applying.** Each job gets its own fresh Chrome profile. The agent prefers the employer's own form. If that
  is broken, it tries other routes in order: the company careers page, then the original ATS link, then the
  hiring email the company publishes.
- **Verification codes.** When a site emails a code or link, the worker asks the inbox helper for it. That is
  IMAP with a Gmail app password, or a Claude Code session with the Gmail connector. The code is entered exactly,
  and case is preserved. Reset / magic links are opened directly.
- **Writing.** Answers and cover letters follow `src/jobagent/style.py`: plain first-person writing about what you
  would do for *them*, with no stock AI phrases and no em dashes. A model checks drafts and rewrites the ones
  that sound generated.

## What it does well and what it won't do

**Does:**
- Fills multi-page ATS forms: React-select dropdowns, phone country pickers, location autocompletes,
  split-box security codes, file uploads (resume vs cover letter detected separately), iframes, and Workday
  account creation.
- Writes tailored cover letters and essay answers from your real projects.
- Retries jobs that failed for reasons outside the job: the network dropped, the daemon restarted, or the site
  was paused.
- Spaces out submissions per site and pauses a site automatically when it says "possible spam".
- Never applies twice: it checks `APPLIED_JOBS.md` and the DB by normalised company + role and by URL.

**Won't (by design):**
- **Solve captchas or hide that it's automated.** It may tick a plain "I'm not a robot" box once. An image or
  puzzle challenge marks the job `needs_human`; `jobagent assist` reopens those in a visible window for you.
- **Make things up.** Not degrees, dates, years of experience, salary, work authorization, or relatives.
- **Accept binding arbitration / jury-trial waivers or broad background-check consents.** Those are flagged
  for you.
- **Click `mailto:` links.** Email applications go through an outbox you control.

## Requirements

| Need | Why | Notes |
|---|---|---|
| macOS or Linux | | Scripts use `caffeinate` (macOS). On Linux, remove it from `scripts/start.sh` and set `apply.chrome_path`. |
| Python 3.12 + [uv](https://docs.astral.sh/uv/) | runtime | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Google Chrome | the browser the agents drive | Real Chrome gets fewer captchas than bundled Chromium. |
| **Azure OpenAI** deployment of a vision + tool-calling chat model | drives the browser, writes answers | Auth is your `az login` session (no key). Install the [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli). |
| **Cloudflare account** with Workers AI | `clef` / `clef-flash` decision models for triage + verification | API token with *Workers AI: Read*. |
| Gmail account | verification codes + email applications | Gmail app password (needs 2-Step Verification), or Claude Code with the Gmail connector. |
| RAM | ~1 GB per parallel browser | The RAM guard keeps `apply.min_free_gb` free. |

## Setup guide

### 1. Get the code and install
```bash
git clone https://github.com/karanxa1/jobagent.git && cd jobagent
uv sync
```

### 2. Secrets
```bash
cp .env.example .env
```
Fill in `.env`:
- `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN`: Cloudflare dashboard → *My Profile → API Tokens* →
  create a token with **Workers AI: Read**. The account ID is on the dashboard sidebar.
- `ATS_PASSWORD` (18 chars) and `ATS_PASSWORD_SHORT` (16 chars): a **new** password used only for job-portal
  accounts the agent creates for you. Use upper and lower case, digits and a symbol.
- `GMAIL_APP_PASSWORD`: Google Account → Security → 2-Step Verification → *App passwords*. It lets the agent
  read verification codes over IMAP and send email applications over SMTP.

### 3. Azure OpenAI
```bash
az login
```
In `config.yaml → azure`, set your resource `endpoint`, `deployment` (and optionally `fallback_deployment`). The
deployment must accept images (screenshots) and tool calls. Reasoning models work; `reasoning_effort: low` is
fast enough for forms.

### 4. Your resume and profile
```bash
cp ~/path/to/your_resume.pdf assets/resume.pdf
uv run jobagent profile              # drafts profile.yaml from the resume
```
Then open `profile.yaml` next to `profile.example.yaml` and complete it:
- Fix anything the resume parse got wrong, and fill every `TODO`.
- Fill work authorization, notice period, compensation, address, EEO answers, and `school_aliases`.
- Add `custom_answers` for questions you know forms ask, and true `stories` for behavioural questions.

The agent will **only** say what's in this file and your resume.

### 5. Tell triage who you are
In `config.yaml`:
- `candidate.eligibility`: one or two sentences on where you live, your citizenship / work authorization,
  and whether you'd relocate with sponsorship.
- `candidate.fit`: what you're good at, so postings can be scored for fit.
- `search.locations`: where to look.
- `triage.*`: thresholds (`accept_role_types`, `max_seniority`…).

### 6. Email codes and email applications
- **Simplest:** keep `otp.provider: imap`, set `otp.imap.user` to your Gmail address, and set
  `GMAIL_APP_PASSWORD` in `.env`.
- **Alternative (no app password):** `otp.provider: bridge`, and run the two Claude Code helper prompts in
  [docs/claude-helpers.md](docs/claude-helpers.md). They read codes and send email applications through the
  Gmail connector. Set `resume_url` to a public link to your resume, since those emails link to it instead of
  attaching it.

### 7. (Optional) Log in to job boards
LinkedIn, Naukri, Wellfound, Instahyre, YC, Hirist and Cutshort jobs are skipped until you log in once:
```bash
uv run jobagent login google wellfound naukri    # opens plain Chrome with those login pages
```
Sign in on every tab, then **quit that Chrome window (Cmd+Q)**: the cookies are exported to
`browser_profiles/storage_state.json` and shared with every worker. Some boards don't work from background
browsers. See [Troubleshooting](#troubleshooting).

### 8. Test on one job (visible, no submit)
```bash
uv run jobagent apply-url "https://job-boards.greenhouse.io/<company>/jobs/<id>" -c "<Company>"
```
Watch it fill the form. It stops before submitting. Check `artifacts/<id>/` for the transcript and screenshot.

### 9. Go
```bash
uv run jobagent run                  # one pass: discover → triage → apply
# or keep it running in the background:
scripts/start.sh                     # stop with scripts/stop.sh; logs in logs/agent.log
```
Want to review before anything is sent? Set `apply.submit: false` (dry run: forms are filled, never
submitted), or run `uv run jobagent apply --dry-run -n 20`.

## Setup prompt for an AI coding agent

Paste this into [Claude Code](https://claude.com/claude-code) (or another coding agent with shell access),
opened in an empty folder. It walks through the whole setup with you and asks you for every personal detail
instead of guessing.

````text
Set up "jobagent" for me. It's an auto-apply agent for AI-engineering jobs; the repo is
https://github.com/karanxa1/jobagent. Work step by step, explain what each step is for, and ASK ME for anything
personal: never invent profile facts (degrees, dates, experience, salary, work authorization).

1. Prerequisites. Check for: Python 3.12+, uv, Google Chrome, Azure CLI (`az`), git. Install what's missing
   (ask before using sudo or Homebrew). On Linux, note that scripts/start.sh uses macOS `caffeinate` (remove it)
   and set apply.chrome_path in config.yaml to the Chrome binary.
2. Clone the repo, cd into it, run `uv sync`.
3. Secrets: `cp .env.example .env`, then help me fill it:
   - CLOUDFLARE_ACCOUNT_ID + CLOUDFLARE_API_TOKEN (token permission: Workers AI Read). Verify with:
     curl -s -H "Authorization: Bearer $TOKEN" https://api.cloudflare.com/client/v4/user/tokens/verify
   - ATS_PASSWORD (18 chars) and ATS_PASSWORD_SHORT (16 chars): generate strong, NEW random passwords with
     upper/lower case, digits and a symbol, and tell me to save them in my password manager.
   - GMAIL_APP_PASSWORD: walk me through creating a Gmail app password (needs 2-Step Verification).
   Never print secret values back to me after writing them, and never commit .env.
4. Azure OpenAI: run `az login` (I'll complete it in the browser). Ask me for my resource endpoint and the
   deployment name(s) of a vision + tool-calling chat model; put them in config.yaml → azure. Verify with a
   one-line call: `uv run python -c "import asyncio;from jobagent.config import load_config;from jobagent.llm import Luna;print(asyncio.run(Luna(load_config()).chat('Reply OK','ping')))"`.
5. Resume and profile: ask me for my resume PDF path and copy it to assets/resume.pdf. Run
   `uv run jobagent profile`, then go through profile.yaml with me section by section, using
   profile.example.yaml as the reference: fix parse errors, fill every TODO, and ask me for work authorization,
   notice period, current/expected compensation, address + postal code, school_aliases, EEO preferences,
   relocation stance, and anything a form commonly asks. Add custom_answers for my known answers. Offer to
   write 1-2 true `stories` from what I tell you (in my words). Validate with:
   `uv run python -c "import yaml;yaml.safe_load(open('profile.yaml'));print('ok')"`.
6. config.yaml: set otp.imap.user to my Gmail address; write candidate.eligibility (where I live, my
   citizenship/work authorization, relocation) and candidate.fit (what I'm good at) from my answers; set
   search.locations; ask whether I want dry-run mode first (apply.submit: false). Set apply.workers based on
   my RAM (about 1 GB per browser; keep processes: 1 below ~20 workers, 2 above).
7. Optional logins: ask which job boards I use (Google, LinkedIn, Naukri, Wellfound, Instahyre, YC, Hirist,
   Cutshort). Run `uv run jobagent login <sites>` (it opens Chrome; I sign in on every tab and press Cmd+Q).
8. Smoke test: pick one open Greenhouse posting for an AI-engineer role and run
   `uv run jobagent apply-url "<url>" -c "<Company>"` (visible, no submit). Show me the result and the
   screenshot path in artifacts/. Fix anything that went wrong before continuing.
9. First real pass: `uv run jobagent discover`, then `uv run jobagent triage`, then `uv run jobagent status`.
   Show me counts and 10 queued jobs (`uv run jobagent status --show queued`). Ask before starting to submit.
10. When I say go: `scripts/start.sh`, then confirm it's running (tail logs/agent.log, `uv run jobagent status`).
    Explain how to stop it (scripts/stop.sh), where applied jobs are listed (APPLIED_JOBS.md), how to see what
    needs me (`uv run jobagent status --show needs_human`), where unanswered questions collect
    (unanswered_questions.yaml → answer them under custom_answers in profile.yaml, then `uv run jobagent retry`),
    and how to finish captcha-blocked ones (`uv run jobagent assist`).

Rules for you: restart the daemon only with scripts/stop.sh then scripts/start.sh. Don't solve captchas or add
anti-bot-detection tricks. Don't commit or share profile.yaml, .env, jobs.db, browser_profiles/ or assets/.
````

## Running it

| Command | What it does |
|---|---|
| `uv run jobagent run` | One full pass: discover → triage → apply |
| `scripts/start.sh` / `scripts/stop.sh` | Background daemon (all processes), forever / stop it and every agent Chrome |
| `uv run jobagent status` | Counts per status |
| `uv run jobagent status --show needs_human` | Jobs waiting on you, with the reason |
| `uv run jobagent apply-url <url> -c <Company>` | One posting, visible, dry run (add `--submit` to send it) |
| `uv run jobagent apply --dry-run -n 20` | Fill 20 queued forms without submitting |
| `uv run jobagent retry` | Requeue `failed` + `needs_human` after you fix something |
| `uv run jobagent assist` | Reopen captcha-blocked jobs in a visible Chrome: you solve the captcha, it finishes |
| `uv run jobagent manual` | `MANUAL_APPLY.md`: jobs on sites that block automation, with a cover letter each |
| `uv run jobagent mark-applied <url>` | Record a job you applied to yourself |
| `uv run jobagent inbox` | Classify recruiter replies (interview / assessment / rejection) |
| `uv run jobagent rescore` | Re-apply triage thresholds after editing `config.yaml` |
| `uv run jobagent login <sites>` | Save job-board sessions for the workers |

Logs: `tail -f logs/agent.log`. Each application leaves a folder `artifacts/<source>_<id>/` with the agent
transcript, the final screenshot and any cover letter.

## Daily workflow
1. `uv run jobagent status --show needs_human` shows what's waiting on you.
2. Open `unanswered_questions.yaml` (questions the agent couldn't answer truthfully). Add your answers under
   `custom_answers:` in `profile.yaml`. The running daemon reloads the profile automatically.
3. `uv run jobagent retry` puts those jobs back in the queue.
4. `uv run jobagent assist` clears captcha-blocked ones while you're at the screen.
5. `APPLIED_JOBS.md` is your record of what was sent where.

## Configuration reference

`config.yaml` is commented. The main knobs:

| Key | Default | Meaning |
|---|---|---|
| `apply.workers` | 8 | Browsers in parallel (total). ~1 GB RAM each. |
| `apply.processes` | 1 | Daemon processes. One Python event loop can't drive more than ~25 browsers, so use 2 above ~20 workers. |
| `apply.min_free_gb` | 2 | Never start a browser that would leave less free RAM than this. |
| `apply.headless` | true | Invisible browsers. `false` shows the windows. |
| `apply.submit` | true | `false` = dry run, never presses the final Submit. |
| `apply.job_timeout_min` | 20 | Hard cap per application. |
| `apply.host_concurrency` / `host_min_gap_seconds` / `host_daily_cap` | per site | Rate limits per site. Logged-in boards ban accounts that apply in bursts. |
| `apply.login_hosts` | job boards | Skipped until `jobagent login` saved a session for them. |
| `apply.assist_only_hosts` | captcha-walled sites | Only handled by `jobagent assist`. |
| `apply.manual_hosts` | [] | Never automated; listed in `MANUAL_APPLY.md`. |
| `triage.*` | | Which postings get a browser. |
| `candidate.eligibility` / `candidate.fit` | | How triage judges if you can apply and how well you fit. |
| `otp.provider` | imap | `imap`, `bridge` (Claude helper), `mcp` (local Gmail MCP server) or `none`. |
| `sources.disable` / `sources.only` | | Turn job sources off / on by name. |

Paused sites are kept in `paused_hosts.json` (`host → unix time until`), written when a site flags a submission
as spam. Delete an entry to resume that site early.

## Customising
- **Other roles:** edit `QUESTIONS` in `src/jobagent/triage.py` (what counts as a relevant role) and
  `triage.accept_role_types`; adjust the source keyword lists in `src/jobagent/sources/`.
- **Other countries:** set `personal.country`, `phone_country_code`, `work_authorization` and
  `candidate.eligibility`; set `search.locations`; disable India-only sources
  (`sources.disable: [naukri, instahyre, hirist, cutshort]`). Salary fields in `profile.yaml` are free text,
  so describe your currency there.
- **Your voice:** `src/jobagent/style.py` is the writing guide used for every answer and cover letter.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Lots of `needs_human` with "Cloudflare" / "Just a moment…" | That site blocks headless browsers (Instahyre does). Remove it from your logins, or set `apply.headless: false` and accept visible windows. |
| LinkedIn asks for a security PIN or your session stops working | LinkedIn treats new automated browsers as suspicious. Keep LinkedIn concurrency at 1–2, or skip it. |
| Log goes quiet, jobs stay `in_progress` with no browsers | Too many browsers for one process. Set `apply.processes: 2` (or lower `apply.workers`) and restart with the scripts. |
| A site says "possible spam" | The site is paused for 6 h automatically. If it keeps happening, add it to `apply.manual_hosts`. |
| Verification code never arrives | Check `otp.imap.user` / `GMAIL_APP_PASSWORD` and your spam folder. In bridge mode, after 4 failed lookups in 30 min the agent assumes the site never sent it and flags the job. |
| Greenhouse fields show a value but stay "required" | Usually reCAPTCHA-related. The agent reloads once and refills; if it persists the job is flagged, and `assist` finishes it. |
| Stray Chrome processes after a crash | `scripts/stop.sh` kills only agent Chromes (it never touches your own profile). Don't kill browsers while the daemon runs. |
| `jobagent login` closed before you finished | Run it again for the missing sites only. Cookies from earlier sites are kept in `browser_profiles/login`. |

## Project layout
```
src/jobagent/
  cli.py           all commands (typer)
  orchestrator.py  discovery loop, triage, worker pool, per-site limiter, RAM guard, daemon shards
  applier.py       one application = one browser-use agent; custom tools and the rules it follows
  triage.py        decision-model questions + thresholds
  sources/         job sources (ATS boards, YC, VC portfolios, LinkedIn, Naukri, Wellfound, Instahyre, aggregators)
  llm.py           Azure OpenAI + Cloudflare Workers AI clients
  otp.py           verification codes: IMAP, Gmail MCP, or the Claude "bridge"
  style.py         the writing guide for answers and cover letters
  browsers.py      tracks every Chrome the agent starts; nothing outlives its job
  db.py ledger.py  SQLite queue, APPLIED_JOBS.md ledger
scripts/           start.sh / stop.sh (daemon), kill_agent_browsers.py, source smoke tests
docs/              Claude Code helper prompts (verification codes, email applications)
config.yaml        settings (no secrets)
profile.example.yaml   template for your profile.yaml
```

## Responsible use
- **You are responsible for every application sent in your name.** Start with dry runs, read what it writes,
  and keep `profile.yaml` accurate.
- Job boards' terms of service may prohibit automated use, and accounts can be restricted. The per-site limits
  exist to keep the pace human. Don't raise them aggressively on sites where you're logged in.
- Apply only to roles you'd actually take. Mass-applying to everything wastes recruiters' time and hurts your
  reputation with employers.
- Never commit `profile.yaml`, `.env`, `jobs.db`, `browser_profiles/` or `assets/`. `.gitignore` already excludes them.

## License
MIT. See [LICENSE](LICENSE).
