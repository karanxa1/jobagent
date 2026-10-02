# jobagent

An AI agent that finds AI-engineering jobs across dozens of sources, filters them, and applies to them with
parallel browser agents. It fills real application forms (Greenhouse, Lever, Workday, Ashby, Oracle/Taleo,
iCIMS, SuccessFactors, Wellfound and more) the way a careful person would: it uploads your resume, writes
answers and cover letters in your voice, and reads email verification codes. It also creates portal accounts
when a site requires one.

It is built to be **truthful**. Every answer comes from your `profile.yaml` and your resume. When a required
question can't be answered from them, the job is set aside for you instead of guessed.

**Works with any major LLM provider:** Azure OpenAI, OpenAI, OpenRouter, Google Gemini, Anthropic, Groq,
Mistral, DeepSeek, Ollama or any OpenAI-compatible server. Runs on **macOS, Linux and Windows**.

> Defaults target **AI / LLM engineering roles** for a candidate **based in India** (Naukri, Instahyre, Indian
> phone formats, INR salary fields). Everything candidate-specific lives in `profile.yaml` and `config.yaml`,
> so you can point it at other roles and countries. See [Customising](#customising).

---

## Contents
- [How it works](#how-it-works)
- [What it does well and what it won't do](#what-it-does-well-and-what-it-wont-do)
- [Requirements](#requirements)
- [Setup guide](#setup-guide)
- [Choosing an LLM provider](#choosing-an-llm-provider)
- [Email verification codes: 5 options](#email-verification-codes-5-options)
- [Windows notes](#windows-notes)
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
   │  triage     a decision model scores every posting: AI role? can this candidate apply (location / visa)?
   │             seniority? fit? still open?  (Cloudflare Workers AI "clef" models, or your main LLM)
   ▼
queue ── N workers ──► one real Google Chrome per worker, driven by your LLM (any provider) through browser-use
                       tools: answer_question · write_cover_letter · attach_file · pick_option (React dropdowns)
                              type_like_keyboard · get_email_verification · apply_by_email · flag_for_human
                       per-site concurrency, gaps between starts, daily caps, a RAM guard
   │  verify     the decision model looks at the final screenshot: was it really submitted?
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
- **Verification codes.** When a site emails a code or link, the worker asks the inbox reader for it. That can
  be your own Gmail session kept open in a browser, IMAP, a Gmail MCP server, or a Claude Code session. See
  [the 5 options](#email-verification-codes-5-options). The code is entered exactly, and case is preserved.
  Reset / magic links are opened directly.
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
| macOS, Linux or Windows 10/11 | | See [Windows notes](#windows-notes). |
| Python 3.12 + [uv](https://docs.astral.sh/uv/) | runtime | macOS/Linux: `curl -LsSf https://astral.sh/uv/install.sh \| sh` · Windows: `powershell -c "irm https://astral.sh/uv/install.ps1 \| iex"` |
| Google Chrome | the browser the agents drive | Found automatically; or set `apply.chrome_path`. Real Chrome gets fewer captchas than bundled Chromium. |
| **An LLM that can read images and call tools** | drives the browser, writes answers | Any provider: see [Choosing an LLM provider](#choosing-an-llm-provider). |
| Cloudflare Workers AI *(optional)* | cheap, calibrated decision models for bulk triage + verification | Without it, your main LLM makes those decisions. |
| A Gmail account | verification codes + email applications | No app password needed: see the [5 options](#email-verification-codes-5-options). |
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
- The API key for your LLM provider, e.g. `OPENROUTER_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY`,
  `ANTHROPIC_API_KEY` or `AZURE_OPENAI_API_KEY`. Azure can also use `az login` with no key.
- *(Optional)* `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN`: Cloudflare dashboard → *My Profile → API Tokens*
  → create a token with **Workers AI: Read**.
- `ATS_PASSWORD` (18 chars) and `ATS_PASSWORD_SHORT` (16 chars): a **new** password used only for job-portal
  accounts the agent creates for you. Use upper and lower case, digits and a symbol.
- *(Optional)* `GMAIL_APP_PASSWORD`: Google Account → Security → 2-Step Verification → *App passwords*. It
  lets the agent read verification codes over IMAP and send email applications over SMTP.

Put your own settings (provider, model, endpoints, worker count) in **`config.local.yaml`**. It is
gitignored and overlaid on `config.yaml`, so `git pull` never conflicts with your changes.

### 3. Pick your LLM
Create `config.local.yaml` with your provider and model, for example:
```yaml
llm:
  provider: openrouter          # azure | openai | openrouter | gemini | anthropic | groq | mistral | deepseek | ollama | openai_compatible
  model: openai/gpt-5.1
  fallback_model: anthropic/claude-sonnet-4.5   # optional, used when the main model errors
apply:
  workers: 4
```
More examples are in [Choosing an LLM provider](#choosing-an-llm-provider). To check it works:
```bash
uv run python -c "import asyncio;from jobagent.config import load_config;from jobagent.llm import LLM;print(asyncio.run(LLM(load_config()).chat('Reply OK','ping')))"
```

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
Many portals email a code or a link before they accept an application. Pick one of the
[5 options](#email-verification-codes-5-options). The easiest one needs no passwords or Google Cloud setup:
```bash
uv run jobagent gmail-login           # Chrome opens Gmail: sign in, then close the window
uv run jobagent gmail-login --check   # confirms the session works
```
Then set `otp.provider: gmail_web` in `config.local.yaml`.

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
# or keep it running in the background (any OS):
uv run jobagent start                # stop with: uv run jobagent stop · logs in logs/agent.log
uv run jobagent daemon-status
```
`scripts/start.sh` / `scripts/stop.sh` (macOS/Linux) and `scripts\start.cmd` / `scripts\stop.cmd` (Windows) do the same.
Want to review before anything is sent? Set `apply.submit: false` (dry run: forms are filled, never
submitted), or run `uv run jobagent apply --dry-run -n 20`.

## Choosing an LLM provider

One model drives the browser, so it **must accept images (screenshots) and support tool calling**. Set it in
`config.local.yaml` and put the key in `.env`:

| provider | example `llm:` | key in `.env` | notes |
|---|---|---|---|
| `openrouter` | `{provider: openrouter, model: openai/gpt-5.1, fallback_model: anthropic/claude-sonnet-4.5}` | `OPENROUTER_API_KEY` | one key, every model |
| `gemini` | `{provider: gemini, model: gemini-2.5-flash}` | `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) | fast and cheap |
| `openai` | `{provider: openai, model: gpt-5.1, reasoning_effort: low}` | `OPENAI_API_KEY` | |
| `anthropic` | `{provider: anthropic, model: claude-sonnet-4-5}` | `ANTHROPIC_API_KEY` | |
| `azure` | `{provider: azure, model: <deployment>, base_url: https://<resource>.openai.azure.com}` | `AZURE_OPENAI_API_KEY`, or none with `az login` | the old `azure:` section still works |
| `groq` | `{provider: groq, model: meta-llama/llama-4-maverick-17b-128e-instruct}` | `GROQ_API_KEY` | pick a vision model |
| `mistral` | `{provider: mistral, model: mistral-medium-latest}` | `MISTRAL_API_KEY` | |
| `deepseek` | `{provider: deepseek, model: deepseek-chat}` | `DEEPSEEK_API_KEY` | text only: fine for triage/answers, **not** for the browser |
| `ollama` | `{provider: ollama, model: qwen2.5vl:32b, base_url: http://localhost:11434/v1}` | none | local; needs a strong vision model |
| `openai_compatible` | `{provider: openai_compatible, model: my-model, base_url: http://host:8000/v1}` | `OPENAI_COMPATIBLE_API_KEY` (optional) | vLLM, LM Studio, Together, Fireworks… |

Other `llm:` keys: `fallback_model`, `base_url`, `reasoning_effort` (reasoning models), `api_key_env` (read the
key from a differently named variable), `headers`, `max_output_tokens`.

**Decision models** (triage, "was it submitted?", is-this-the-right-email) use `decider:`:
- `provider: auto` (default) uses Cloudflare Workers AI when `CLOUDFLARE_ACCOUNT_ID` + `CLOUDFLARE_API_TOKEN`
  are set, and otherwise your main LLM.
- To use a cheaper model for bulk triage, set `decider.fast_model` (e.g. `gemini-2.5-flash-lite` on the
  same provider).

## Email verification codes: 5 options

| `otp.provider` | What you set up | Good for |
|---|---|---|
| **`gmail_web`** | `uv run jobagent gmail-login` once (sign in to Gmail in a normal Chrome window) | **Most people.** No app password, no Google Cloud project. The agent keeps your Gmail open in its own headless browser profile and reads codes from it (unread feed + Gmail search; it doesn't mark mail as read when listing). |
| `imap` | `GMAIL_APP_PASSWORD` in `.env` + `otp.imap.user` | Simple and very reliable if you use 2-Step Verification. Also sends email applications over SMTP. |
| `bridge` + `jobagent otp-relay` | run `uv run jobagent otp-relay` in a second terminal (it uses `gmail_web`, or `--via imap`) | One Gmail reader serving several daemons / processes / machines sharing `jobs.db`. |
| `bridge` + Claude Code | paste the helper prompt from [docs/claude-helpers.md](docs/claude-helpers.md) into Claude Code with the Gmail connector | If you already use Claude Code. Also sends email applications through Gmail. |
| `mcp` | a local Gmail MCP server + `uv run jobagent gmail-auth` (Google Cloud OAuth client) | If you already run a Gmail MCP server. |

How the relay works: every worker that needs a code writes a request into `jobs.db` (`mail_requests`). The
reader finds the newest matching email from that company, its ATS, or a brand it trades under. It only looks
at mail received from 5 minutes before the request, and answers with the exact code, the full link, or a note
like "account already exists, use Forgot Password". A code given to one company is never handed to another.
With `gmail_web` and `apply.processes > 1`, process 0 holds the Gmail browser and serves the others
automatically.

If Google signs the session out (it happens now and then), codes stop arriving and the log says so. Run
`uv run jobagent gmail-login` again.

## Windows notes
- Install [uv](https://docs.astral.sh/uv/) and Google Chrome. Use PowerShell or Windows Terminal.
- Start / stop: `uv run jobagent start` / `uv run jobagent stop` / `uv run jobagent daemon-status`. The same is
  available as `scripts\start.cmd`, `scripts\stop.cmd`, `scripts\start.ps1` and `scripts\stop.ps1`.
- `jobagent login` / `gmail-login` open Chrome: sign in, then **close the window**. That's Cmd+Q on macOS.
- The daemon keeps the PC awake while it runs (`daemon.keep_awake`).
- Chrome is found in `Program Files`, `Program Files (x86)` or `%LocalAppData%`. Otherwise set
  `apply.chrome_path: 'C:\path\to\chrome.exe'` in `config.local.yaml`.
- Status: Windows support is newer than macOS/Linux and has had less real-world use. Please open an issue with
  `logs/agent.log` if something breaks.

## Setup prompt for an AI coding agent

Paste this into [Claude Code](https://claude.com/claude-code) (or another coding agent with shell access),
opened in an empty folder. It walks through the whole setup with you and asks you for every personal detail
instead of guessing.

````text
Set up "jobagent" for me. It's an auto-apply agent for AI-engineering jobs; the repo is
https://github.com/karanxa1/jobagent. Work step by step, explain what each step is for, and ASK ME for anything
personal: never invent profile facts (degrees, dates, experience, salary, work authorization).

1. Prerequisites. Detect my OS (macOS / Linux / Windows). Check for: Python 3.12+, uv, Google Chrome, git.
   Install what's missing (ask before using sudo, Homebrew, winget or admin rights). On Windows use PowerShell
   syntax for every command. Chrome is auto-detected; if it isn't found, set apply.chrome_path in config.local.yaml.
2. Clone the repo, cd into it, run `uv sync`.
3. LLM provider: ask which provider I want to use (OpenRouter, Gemini, OpenAI, Anthropic, Azure, Groq,
   Mistral, Ollama, or any OpenAI-compatible server) and which model. The model MUST accept images and support
   tool calling (it drives the browser from screenshots). Write `llm: {provider, model, fallback_model?}` into
   config.local.yaml (create it; it's gitignored). Azure: ask for the endpoint + deployment; key auth via
   AZURE_OPENAI_API_KEY, or `az login` with no key.
4. Secrets: `cp .env.example .env` (Windows: `Copy-Item .env.example .env`), then help me fill it:
   - the API key for my chosen provider (e.g. OPENROUTER_API_KEY, GEMINI_API_KEY, OPENAI_API_KEY,
     ANTHROPIC_API_KEY, AZURE_OPENAI_API_KEY).
   - optional: CLOUDFLARE_ACCOUNT_ID + CLOUDFLARE_API_TOKEN (Workers AI Read) for cheaper triage. Without them,
     my main LLM does triage (decider.provider: auto).
   - ATS_PASSWORD (18 chars) and ATS_PASSWORD_SHORT (16 chars): generate strong, NEW random passwords with
     upper/lower case, digits and a symbol, and tell me to save them in my password manager.
   Never print secret values back to me after writing them, and never commit .env.
   Verify the LLM with: `uv run python -c "import asyncio;from jobagent.config import load_config;from jobagent.llm import LLM;print(asyncio.run(LLM(load_config()).chat('Reply OK','ping')))"`.
5. Email verification codes: explain the 5 otp.provider options from the README and recommend gmail_web.
   For gmail_web: run `uv run jobagent gmail-login` (Chrome opens Gmail; I sign in and close the window), then
   `uv run jobagent gmail-login --check`, and set otp.provider: gmail_web in config.local.yaml. For imap: help me
   create a Gmail app password (GMAIL_APP_PASSWORD in .env) and set otp.imap.user. Either way, make sure
   personal.email in profile.yaml is this same Gmail address.
6. Resume and profile: ask me for my resume PDF path and copy it to assets/resume.pdf. Run
   `uv run jobagent profile`, then go through profile.yaml with me section by section, using
   profile.example.yaml as the reference: fix parse errors, fill every TODO, and ask me for work authorization,
   notice period, current/expected compensation, address + postal code, school_aliases, EEO preferences,
   relocation stance, and anything a form commonly asks. Add custom_answers for my known answers. Offer to
   write 1-2 true `stories` from what I tell you (in my words). Validate with:
   `uv run python -c "import yaml;yaml.safe_load(open('profile.yaml'));print('ok')"`.
7. config.local.yaml: write candidate.eligibility (where I live, my
   citizenship/work authorization, relocation) and candidate.fit (what I'm good at) from my answers; set
   search.locations; ask whether I want dry-run mode first (apply.submit: false). Set apply.workers based on
   my RAM (about 1 GB per browser; keep apply.processes: 1 below ~20 workers, 2 above).
8. Optional logins: ask which job boards I use (Google, LinkedIn, Naukri, Wellfound, Instahyre, YC, Hirist,
   Cutshort). Run `uv run jobagent login <sites>` (it opens Chrome; I sign in on every tab and quit/close the window).
9. Smoke test: pick one open Greenhouse posting for an AI-engineer role and run
   `uv run jobagent apply-url "<url>" -c "<Company>" -t "<exact job title>"` (visible, no submit). Show me the result and the
   screenshot path in artifacts/. Fix anything that went wrong before continuing.
10. First real pass: `uv run jobagent discover`, then `uv run jobagent triage`, then `uv run jobagent status`.
   Show me counts and 10 queued jobs (`uv run jobagent status --show queued`). Ask before starting to submit.
11. When I say go: `uv run jobagent start`, then confirm it's running (`uv run jobagent daemon-status`, the end
    of logs/agent.log, `uv run jobagent status`). Explain how to stop it (`uv run jobagent stop`), where applied jobs are listed (APPLIED_JOBS.md), how to see what
    needs me (`uv run jobagent status --show needs_human`), where unanswered questions collect
    (unanswered_questions.yaml → answer them under custom_answers in profile.yaml, then `uv run jobagent retry`),
    and how to finish captcha-blocked ones (`uv run jobagent assist`).

Rules for you: restart the daemon only with `uv run jobagent stop` then `uv run jobagent start`. Don't solve captchas or add
anti-bot-detection tricks. Don't commit or share profile.yaml, .env, config.local.yaml, jobs.db, browser_profiles/ or assets/.
````

## Running it

| Command | What it does |
|---|---|
| `uv run jobagent run` | One full pass: discover → triage → apply |
| `uv run jobagent start` / `stop` / `daemon-status` | Background daemon (all processes) on any OS; stop also cleans up its Chromes |
| `uv run jobagent gmail-login [--check]` | Sign in to Gmail for the `gmail_web` code reader / check the session |
| `uv run jobagent otp-relay [--via imap]` | Serve verification codes to every daemon sharing `jobs.db` (`otp.provider: bridge`) |
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
| `uv run jobagent login <sites>` | Save job-board sessions for the workers (`--export-only` re-exports without opening Chrome) |

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
| `llm.*` | (uses `azure:`) | Provider, model, fallback, base_url. See [Choosing an LLM provider](#choosing-an-llm-provider). |
| `decider.*` | auto | Cloudflare decision models if keys exist, else the main LLM; `fast_model` for bulk triage. |
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
| `otp.provider` | imap | `gmail_web`, `imap`, `bridge` (`otp-relay` or Claude helper), `mcp` or `none`. |
| `apply.chrome_path` | auto-detect | Path to Chrome if it isn't in the standard place. |
| `daemon.keep_awake` | true | Keep the machine from sleeping while the daemon runs. |
| `sources.disable` / `sources.only` | | Turn job sources off / on by name. |

Put your personal overrides in `config.local.yaml` (gitignored). `JOBAGENT_CONFIG=<file>` adds one more overlay.
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
| Stray Chrome processes after a crash | `uv run jobagent stop` kills only agent Chromes no live daemon owns (it never touches your own Chrome). |
| Codes stop arriving with `gmail_web` / "Gmail session expired" in the log | Google signed the session out. Run `uv run jobagent gmail-login` again. |
| `jobagent login` closed before you finished | Run it again for the missing sites only. Cookies from earlier sites are kept in `browser_profiles/login`. |

## Project layout
```
src/jobagent/
  cli.py           all commands (typer)
  orchestrator.py  discovery loop, triage, worker pool, per-site limiter, RAM guard, daemon shards
  applier.py       one application = one browser-use agent; custom tools and the rules it follows
  triage.py        decision-model questions + thresholds
  sources/         job sources (ATS boards, YC, VC portfolios, LinkedIn, Naukri, Wellfound, Instahyre, aggregators)
  llm.py           LLM clients for every provider + decision models (Cloudflare or LLM-based)
  otp.py           verification codes: matching, IMAP, Gmail MCP, the bridge queue, the otp-relay loop
  gmail_web.py     reads Gmail through your own signed-in browser profile
  daemonctl.py     start / stop / status for the daemon on any OS
  osutil.py        Chrome detection, keep-awake, platform differences
  style.py         the writing guide for answers and cover letters
  browsers.py      tracks every Chrome the agent starts; nothing outlives its job
  db.py ledger.py  SQLite queue, APPLIED_JOBS.md ledger
scripts/           start/stop wrappers (.sh, .cmd, .ps1), kill_agent_browsers.py, source smoke tests
docs/              Claude Code helper prompts (verification codes, email applications)
config.yaml        settings (no secrets); your overrides go in config.local.yaml
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
