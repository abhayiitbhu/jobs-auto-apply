# Jobs Auto Apply

Automatically search, filter, and apply to jobs across **Wellfound**, **Uplers**, **Naukri**, **Hirist**, and **Instahyre** — including company ATS forms (Greenhouse, Lever, Workday, etc.) and recruiter screening questions, which are answered from your profile facts with an optional local LLM.

| Platform | Apply type |
|----------|------------|
| Wellfound | In-platform Easy Apply |
| Uplers | Redirects to company ATS (Greenhouse, Workday, Lever, …) |
| Naukri | Quick apply + chatbot screening on naukri.com |
| Hirist | One-click apply + screening form on hirist.tech |
| Instahyre | One-click apply on the opportunities feed |

## How it works

For every run the tool:

1. **Searches** each enabled platform using your filters.
2. **Filters** out already-applied jobs, skipped companies, and skipped roles.
3. **Applies** — fills the platform form / company ATS, including resume upload and a tailored cover note.
4. **Answers screening questions** using the resolution order below.
5. **Defers** anything it can't answer confidently to `data/pending_questions.json` for you to answer later (instead of guessing).

### Answer resolution order

When a recruiter question appears, the answer engine (`jobs_auto_apply/answers/`) tries, in order:

1. **Saved memory** — a previously confirmed answer in `data/user_memory.json`.
2. **Config / profile facts** — deterministic values from `profile/application_facts.yaml` (notice period, PAN/UAN, education, skill years, location, CTC, …).
3. **RAG** — retrieval over your profile facts / resume to answer factual questions.
4. **LLM** — a local Ollama model drafts an answer, optionally double-checked by a verifier model for high-risk fields (CTC, employer, years of experience).

Answers below the confidence bar are **not** auto-filled — they're queued in `pending_questions.json`. Genuine fill failures are recorded in `data/technical_failures.json` so they can be retried.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

cp config.example.yaml config.yaml
cp -r profile.example profile
# Edit config.yaml + profile/*.yaml, add your resume.pdf
# (config.yaml and profile/ are gitignored — they hold your personal data)

# (optional) set up the local LLM — see "Local LLM" below
bash scripts/setup_ollama_models.sh

# Quit Chrome first (Cmd+Q) if using your Chrome profile, then:
python main.py run --platform all
python main.py run --platform naukri
python main.py run --platform hirist
python main.py run --platform instahyre
```

Requires **Python 3.10+**. By default it reuses your **Chrome profile** — log into the job sites in Chrome first (Google sign-in works).

## Commands

The entry point is `main.py`, which loads `.env` (if present) and invokes the Click CLI.

| Command | Description |
|---------|-------------|
| `run` | Search, filter, and apply automatically (or apply the approved queue if `require_review` is set). |
| `serve` | Always-on HTTP server (uvicorn): re-applies every N minutes and exposes `GET /status` and `POST /run-now` (`--host`/`--port`, default `127.0.0.1:8765`). With `telegram.mode: listener`, Telegram Q&A runs in the same process. |
| `review` | Collect listings per platform, then interactively approve/reject before applying. |
| `apply-reviewed` | Submit applications only for jobs approved in the review queue. |
| `review-status` | Show pending / approved / rejected counts per platform. |
| `answer-questions` | Answer deferred pending questions, or `--review` bad auto-generated saved answers. |
| `telegram-login` | Verify the bot token and capture your chat_id (send /start to your bot). |
| `telegram-answer` | Send pending questions to Telegram now, save replies, then retry those jobs (`--test` to verify). |
| `telegram-listen` | Always-on daemon: asks pending questions on Telegram and applies as replies arrive. |
| `whatsapp-login` | Link WhatsApp Web once (scan the QR). Unofficial — ToS/ban risk; prefer Telegram. |
| `whatsapp-answer` | Send pending questions to WhatsApp now (`--test` to verify the link). |
| `whatsapp-listen` | Always-on WhatsApp daemon (unofficial; prefer Telegram). |
| `memory` | Show saved review decisions, preferences, and question answers. |
| `login` | Sign in once (Google/passkey) and save the session. |
| `verify` | Open each platform to verify the saved session is valid. |
| `chrome-profiles` | List the Chrome profiles available on your machine. |
| `export-cookies-help` | Print cookie-export instructions (legacy `auth.method: cookies`). |

Common options: `--config <path>` (default `config.yaml`), `--platform {wellfound,uplers,naukri,hirist,instahyre,all}` (default `all`), `--verbose`.

```bash
python main.py chrome-profiles
python main.py login --platform all
python main.py verify --platform naukri
python main.py run --platform all --verbose
python main.py answer-questions          # fill in deferred questions
python main.py answer-questions --review # fix bad saved answers
```

## Answer pending questions over chat (Telegram / WhatsApp)

Instead of answering deferred questions in the terminal, the tool can send them to
your phone and wait for your replies. Each unanswered question is sent as a message;
you reply with the answer, it is saved like a manual answer, and the skipped jobs
are re-applied automatically. Anything you don't answer within `reply_timeout_seconds`
stays pending for next time.

When answering (terminal, Telegram, or WhatsApp), you have three ways to decline:

| Action | Terminal | Chat reply | Effect |
|--------|----------|------------|--------|
| **Skip for now** | `s` | `skip` | Question stays pending; job may come back on the next run |
| **Drop job** | `d` | `drop` | Abandons the job(s) linked to this question — they won't be retried |
| **Ignore question** | `i` | `ignore` | Saves a default N/A answer (e.g. "No", "0 years") and never asks again |

Both channels share the same flow and the same two modes:

- **`inline`** — questions are asked at the end of each `run`; that run waits for
  your replies (up to `reply_timeout_seconds`).
- **`listener`** — a separate long-running daemon owns the chat session. `run` just
  defers questions; the daemon asks them and re-applies as your replies arrive —
  **even hours later**.

If both channels are enabled, **Telegram wins**.

### Telegram (recommended)

Official, free Bot API via long polling — **no server, no tunnel, and no ToS/ban
risk**.

```bash
# 1) create a bot with @BotFather, copy the token into telegram.bot_token in config.yaml
# 2) capture your chat id (send /start to your bot when prompted)
python main.py telegram-login
# 3a) inline mode (telegram.mode: inline): happens at end of `run`, or:
python main.py telegram-answer            # add --test to verify the round-trip
# 3b) listener mode (telegram.mode: listener): bundled into `serve` — no extra process:
python main.py serve
#    (or standalone: python main.py telegram-listen)
```

### WhatsApp (unofficial — use with caution)

Drives WhatsApp Web with Playwright; link once by scanning a QR. **Automating
WhatsApp Web is against WhatsApp's Terms of Service and can get the number
temporarily restricted or permanently banned.** Prefer Telegram.

```bash
python main.py whatsapp-login              # scan QR (WhatsApp → Linked Devices)
python main.py whatsapp-answer             # --test to verify; or whatsapp-listen
```

If you point `whatsapp.phone` at your **own** number it uses the "Message yourself"
chat; for a cleaner split, link WhatsApp Web with a **secondary number** and set
`whatsapp.phone` to your personal number.

## Configuration

Copy `config.example.yaml` to `config.yaml` and edit. Top-level sections:

| Section | Purpose |
|---------|---------|
| `user` | Name, email, phone, LinkedIn/GitHub — used to fill ATS forms. `phone_country_code` (e.g. `+91`) formats the phone in cover-letter signatures; `expected_display_name` defaults to your first name. |
| `profile` | Core skills, roles, headline used for matching and cover notes. |
| `compensation` | Current/expected CTC (used for CTC questions and cover letter). |
| `cover_letter` | Cover note mode (`dynamic` / `template`), reference letter path, and options. |
| `auth`, `browser` | Login method and Chrome-profile / Playwright browser settings. |
| `paths` | Locations of `application_facts`, `user_memory.json`, `pending_questions.json`, etc. |
| `answers` | Notice/join threshold and default experience chip options. |
| `resume` | Resume PDF path. |
| `wellfound`, `uplers`, `naukri`, `hirist`, `instahyre` | Per-platform `enabled` flag + filters. |
| `application` | Run-wide behaviour: dry run, caps, delays, `parallel_platforms`, review gating. |
| `llm` | Local LLM / RAG settings (models, confidence thresholds, FAISS). |
| `telegram` | Send pending questions to a Telegram bot and read replies (official Bot API; recommended). |
| `whatsapp` | Send pending questions to WhatsApp and read replies (unofficial WhatsApp Web; ToS/ban risk). |
| `workday` | Credentials/answers for Workday multi-step portals. |
| `state` | Misc persisted run state. |

### Platform filters

- **Naukri** — `keywords`, `locations`, `experience_max` (Naukri uses the max; `experience_min` feeds other platforms), `salary_min_lakhs`, `remote_only`, `quick_apply_only`, `sort`, `max_job_age_days`, `max_pages`
- **Hirist** — `keywords` (list), `cities`, `experience` (`0-2`, `2-5`, `5-10`)
- **Instahyre** — `job_functions`, `locations`, `experience_years`, `company_size`
- **Wellfound / Uplers** — keywords, skills, locations, roles

### Profile facts (answer the screening questions)

Two YAML files under `profile/` feed the answer engine. Copy the templates in `profile.example/` to `profile/` (`cp -r profile.example profile`), then add real values — never invent PAN/UAN; leave blank to defer to manual.

- **`profile/application_facts.yaml`** — structured facts: `pan`, `uan`, `gender`, `notice_period_days`, `serving_notice`, `education` (bachelors/masters/etc.), `date_of_birth`, `pincode`, `current_location`, `willing_to_relocate`, `preferred_locations`, `past_employers`, and a `skill_years` map (explicit years per skill; `0` = none) plus free-text facts like `reason_for_change`.
- **`profile/resume_facts.yaml`** — your resume as structured data: headline, skills, work `experience`, education, `skip_companies`, and a profile summary. Used for RAG and cover-letter matching.

## Local LLM (Ollama)

The LLM drafts and verifies answers entirely on-device via [Ollama](https://ollama.com).

```bash
brew install ollama && brew services start ollama
bash scripts/setup_ollama_models.sh
```

This pulls `qwen2.5:7b` and creates a **`job-answers`** model (generator). If `llm.verifier_enabled: true`, it also pulls `llama3.2:3b` and creates **`job-verify`**, a lightweight independent verifier for high-risk fields. The verifier deliberately uses a **different model family** (Llama vs Qwen) so its mistakes decorrelate from the generator's, while staying small (~3b) to keep latency and VRAM low. Then in `config.yaml`:

```yaml
llm:
  enabled: true
  base_url: "http://127.0.0.1:11434"
  model: job-answers
  verifier_model: job-verify   # or "job-answers" to reuse one resident model (<=16GB RAM)
  verifier_enabled: true
  min_confidence: 0.92         # fill threshold
  min_confidence_persist: 0.98 # only write LLM/RAG drafts to memory above this
  use_faiss_memory: true       # FAISS RAG over prior Q/A + profile facts
  embeddings_model: sentence-transformers/all-MiniLM-L6-v2
```

On a 16GB Mac, limit Ollama concurrency: `export OLLAMA_NUM_PARALLEL=1` and `export OLLAMA_MAX_LOADED_MODELS=2`. To disable the LLM entirely, set `llm.enabled: false` (factual config/RAG answers still work; unknown questions are deferred).

## Cover letters

Each application scrapes the **job description** and builds a tailored note:

- Matches JD keywords to your `profile.core_skills`
- Picks a role-specific hook (platform engineer, architect, FDE, …)
- Includes CTC when `cover_letter.include_ctc: true`

```yaml
cover_letter:
  mode: dynamic    # dynamic (default) | template
  include_ctc: true
  max_words: 200
  reference_path: "profile/cover_letter_reference.txt"
```

In `dynamic` mode, the letter is generated from the job description and your reference letter at `cover_letter.reference_path` (default `profile/cover_letter_reference.txt`) — drop in a sample cover letter there to anchor the tone and structure. When no JD is available (or `mode: template`), a static template is used instead.

## Logging in

### Option A — Reuse your Chrome profile (recommended)

```yaml
browser:
  use_chrome_profile: true
  chrome_profile_name: "Default"   # run: python main.py chrome-profiles
  chrome_channel: "chrome"
  headless: false
```

```bash
# Quit Chrome completely (Cmd+Q) — Chrome locks its profile — then:
python main.py run --platform all
```

### Option B — One-time login in Playwright Chromium

```bash
python main.py login --platform all
```

Session is saved to `data/sessions/`. Set `browser.use_chrome_profile: false` for this mode. Do **not** store your Gmail password in config — passkeys work in the login window. Legacy cookie mode: `auth.method: cookies` + `python main.py export-cookies-help`.

## Company ATS (Uplers flow)

Uplers jobs redirect to the company's career site. The tool detects the ATS and fills the form:

- **Workday** (`*.myworkdayjobs.com`) — full multi-step wizard (see below)
- Greenhouse, Lever, Ashby, SmartRecruiters, iCIMS, BambooHR, Teamtailor, Jobvite, Recruitee
- **Generic fallback** for other career pages

### Workday

When a job redirects to a Workday portal, a dedicated handler accepts the cookie notice, clicks Apply → Apply Manually, signs in / creates an account (using `workday.password`), walks the wizard (My Information → Experience → Questions → Disclosures → Review) filling fields by `data-automation-id`, and submits.

```yaml
workday:
  password: "your-workday-password"   # reused to create accounts on new company portals
  how_did_you_hear: "LinkedIn"
  skip_voluntary_disclosures: true
  address:
    city: "Bengaluru"
    state: "Karnataka"
    country: "India"
```

Each company has a separate Workday account tied to your email; some require manual email verification, and custom screening questions may need manual answers.

## Run platforms in parallel

```yaml
naukri: { enabled: true }
hirist: { enabled: true }
instahyre: { enabled: true }

application:
  parallel_platforms: true   # naukri + hirist + instahyre concurrently
```

```bash
python main.py run --platform all
```

Each cookie-based platform gets its own browser session. Wellfound/Uplers run sequentially (they share the Chrome profile).

## Data files

Created under `data/` (paths configurable in `config.paths`):

| File | Purpose |
|------|---------|
| `data/user_memory.json` | Confirmed Q&A answers, review decisions, preferences. |
| `data/pending_questions.json` | Questions deferred for you to answer manually. |
| `data/technical_failures.json` | Jobs that failed to fill (for retry/inspection). |
| `data/naukri_resume_sync.json` | Timestamp of the last Naukri resume sync (`config.paths.naukri_resume_sync`). |
| `data/hirist_resume_sync.json` | Timestamp of the last Hirist resume sync (`config.paths.hirist_resume_sync`). |
| `data/telegram_chat.json` | Captured Telegram `chat_id` (written by `telegram-login`). |
| `data/telegram_offset.json` | Last processed Telegram `getUpdates` offset (survives `serve --reload`). |
| `data/sessions/` | Saved browser sessions (Option B login). |
| `data/faiss/` | FAISS vector index for RAG over prior answers. |
| `data/applied_*.json`, `data/run.log` | Applied-job ledger and run log. |

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/setup_ollama_models.sh` | Pull base models and create `job-answers` / `job-verify`. |
| `scripts/generate_config_schema.py` | Regenerate `config.schema.json` from the config dataclasses. |
| `scripts/cleanup_user_memory.py` | Prune/repair entries in `user_memory.json`. |
| `scripts/migrate_memory_to_groups.py` | Migrate legacy memory to group-keyed answers. |
| `scripts/test_single_naukri.py` | Apply to a single Naukri job for debugging. |
| `scripts/test_single_hirist.py` | Apply to a single Hirist job for debugging. |
| `scripts/debug_jd.py` | Inspect scraped job-description / cover-note output. |

## Safety

- Start with `dry_run: true` — searches without submitting.
- Use a small `max_jobs_per_run` for the first live test.
- Delays default to ~45–90s between applications.
- Naukri has daily apply limits (~50/day on free tier).
- Some ATS forms (CAPTCHA / custom fields) need manual completion.
- High-volume auto-apply may violate platform ToS — use responsibly.

## Project layout

```
jobs-auto-apply/
├── main.py                     # entry point -> jobs_auto_apply.cli:main
├── config.example.yaml          # copy to config.yaml and edit (gitignored)
├── requirements.txt
├── profile.example/             # copy to profile/ and edit (gitignored)
│   ├── application_facts.yaml   # structured facts for answering questions
│   ├── resume_facts.yaml        # resume as structured data (RAG source)
│   └── cover_letter_reference.txt
├── ollama/
│   ├── Modelfile.job-answers    # generator model
│   └── Modelfile.job-verify     # verifier model
├── scripts/                     # setup + maintenance + debug helpers
└── jobs_auto_apply/
    ├── cli.py                   # Click CLI (run, review, login, …)
    ├── config.py                # config loading + dataclasses
    ├── browser.py               # Playwright / Chrome-profile sessions
    ├── memory.py                # user_memory.json read/write
    ├── pending_questions.py     # deferred-question queue
    ├── technical_failures.py    # fill-failure ledger
    ├── rag_answers.py           # RAG over profile facts
    ├── llm_answers.py           # Ollama LLM generate/verify + FAISS
    ├── question_groups.py       # group questions to share one answer
    ├── application_questions.py # discover/resolve/fill orchestration
    ├── answers/                 # answer resolution engine
    │   ├── resolve.py           #   saved -> config -> RAG -> LLM order
    │   ├── memory_store.py      #   save/lookup saved answers
    │   ├── persist_policy.py    #   when a draft may be persisted
    │   └── …                    #   fields, validation, chips, location, etc.
    ├── ats/                     # company ATS detection + form fill (Workday, …)
    ├── wellfound/               # search.py / apply.py / pipeline.py
    ├── uplers/                  # search.py / apply.py
    ├── naukri/                  # search.py / apply.py / questions.py / resume sync
    ├── hirist/                  # search.py / apply.py / questions.py
    └── instahyre/               # search.py / apply.py / feeds.py
```

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Login fails | Re-run `python main.py login --platform <p>`, or quit Chrome before using the Chrome profile. |
| No jobs found | Broaden keywords/filters; confirm you're logged in (`verify`). |
| Questions keep deferring | Add the fact to `profile/application_facts.yaml`, or run `python main.py answer-questions`. |
| LLM not used | Ensure Ollama is running and `llm.enabled: true`; check `base_url` and model names. |
| External URL not captured | Job may use in-platform apply — apply manually. |
| ATS submit failed | Site may use CAPTCHA or a non-standard form — apply manually. |
| Missing email error | Set `user.email` in `config.yaml`. |
```
