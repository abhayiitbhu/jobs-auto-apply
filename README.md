# Job Auto Apply

Automatically search and apply on **Wellfound**, **Uplers**, **Naukri**, **Hirist**, and **Instahyre**.

| Platform | Apply type |
|----------|------------|
| Wellfound | In-platform Easy Apply |
| Uplers | Redirects to company ATS (Greenhouse, Workday, etc.) |
| Naukri | Quick apply on naukri.com |
| Hirist | One-click apply on hirist.tech |
| Instahyre | One-click apply on opportunities feed |

## Quick start

```bash
cp config.example.yaml config.yaml
# Add resume.pdf, quit Chrome (Cmd+Q)

python main.py run --platform all
python main.py run --platform naukri
python main.py run --platform hirist
python main.py run --platform instahyre
```

Uses your **Chrome profile** by default — log into all sites in Chrome first (Google sign-in works).

## Platforms config

See `config.example.yaml` for filters per platform. Enable/disable with `enabled: true/false`.

### Naukri filters
- `keywords`, `locations`, `experience_min`, `salary_min_lakhs`, `remote_only`

### Hirist filters
- `keywords` (list), `cities`, `experience` (`0-2`, `2-5`, `5-10`)

### Instahyre filters
- `job_functions`, `locations`, `experience_years`, `company_size`

## Commands

```bash
python main.py chrome-profiles
python main.py login --platform all
python main.py verify --platform naukri
python main.py run --platform all --verbose
```

## Cover letters

Each application scrapes the **job description** from the page and builds a tailored note:

- Matches JD keywords to your `profile.core_skills`
- Picks a role-specific hook (platform engineer, architect, FDE, etc.)
- Includes CTC when `cover_letter.include_ctc: true`

```yaml
cover_letter:
  mode: dynamic    # dynamic (default) | template | llm
  include_ctc: true
  max_words: 200

compensation:
  current_ctc_lpa: 48
  current_fixed_lpa: 40
  current_variable_lpa: 2
  current_esops_lpa: 6
  expected_ctc_lpa: 55
```

For highest quality, set `mode: llm` and export `OPENAI_API_KEY`.

## Notes

- Naukri has daily apply limits (~50/day on free tier) — use delays
- Instahyre is fastest (true one-click, no forms)
- Hirist supports Google login via Chrome profile
- Quit Chrome before each run when using `use_chrome_profile: true`

## Quick start

```bash
cd wellfound-auto-apply

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

cp config.example.yaml config.yaml
# Add resume.pdf

# One-time Google login (only if use_chrome_profile: false)
# python main.py login --platform all

# Auto-apply (quit Chrome first if using chrome profile)
python main.py run --platform all
```

## Logging in with Gmail (Google OAuth)

### Option A — Reuse your Chrome profile (recommended)

If you're already signed into Google, Wellfound, and Uplers in Chrome:

```yaml
browser:
  use_chrome_profile: true
  chrome_profile_name: "Default"   # run: python main.py chrome-profiles
  chrome_channel: "chrome"
  headless: false
```

```bash
# 1. Quit Chrome completely (Cmd+Q on macOS)
# 2. Run — opens your real Chrome profile with existing Google login
python main.py run --platform all
```

List available profiles:

```bash
python main.py chrome-profiles
```

**Important:** Chrome must be fully quit before the script runs (Chrome locks its profile).

### Option B — One-time login in Playwright Chromium

```bash
python main.py login --platform all
```

Session saved to `data/sessions/`. Set `browser.use_chrome_profile: false` for this mode.

**Do not** store your Gmail password in config. Passkeys work in the browser window during `login`.

Legacy: `auth.method: cookies` + `python main.py export-cookies-help`.

## Configuration

### User profile (required for company ATS forms)

```yaml
user:
  name: "Abhay Jain"
  email: "abhay.jain.cse11@itbhu.ac.in"
  phone: "9358161425"
  linkedin: "https://www.linkedin.com/in/abhay-jain"
  expected_display_name: "abhay"
```

### Uplers flow

1. Logs into Uplers (saved Google session or cookies)
2. Applies your filters (keywords, skills, locations, roles)
3. For each job listing, opens the detail page
4. Clicks Apply / View Job → captures the **company career site URL**
5. Navigates to Greenhouse / Lever / Ashby / etc.
6. Fills name, email, phone, LinkedIn, resume upload, cover letter
7. Submits the application

### Supported company ATS systems

- **Workday** (`*.myworkdayjobs.com`) — full multi-step wizard (see below)
- Greenhouse (`boards.greenhouse.io`)
- Lever (`jobs.lever.co`)
- Ashby (`jobs.ashbyhq.com`)
- SmartRecruiters, iCIMS, BambooHR, Teamtailor, Jobvite, Recruitee
- **Generic fallback** for other career pages

### Workday support

When a job redirects to a Workday career portal, the tool runs a dedicated handler that:

1. Accepts cookie notice (`legalNoticeAcceptButton`)
2. Clicks **Apply** → **Apply Manually**
3. Signs in or creates an account (if `workday.password` is set)
4. Walks through the multi-page wizard (My Information → Experience → Questions → Disclosures → Review)
5. Fills fields via `data-automation-id` selectors (name, email, phone, address, resume, cover letter)
6. Clicks **Save and Continue** through each step and submits on the review page

Add to `config.yaml`:

```yaml
workday:
  password: "your-workday-password"   # same password for create-account on new company portals
  how_did_you_hear: "LinkedIn"
  skip_voluntary_disclosures: true
  address:
    city: "Bengaluru"
    state: "Karnataka"
    country: "India"
```

**Notes:**
- Each company has a separate Workday account tied to your email — `workday.password` is reused when creating new accounts.
- Some companies require email verification after account creation; complete that manually if prompted.
- Custom screening questions may need manual answers if the tool cannot match them.

### Cookie export

Only needed if `auth.method: cookies`. Otherwise use `python main.py login`.

```bash
python main.py export-cookies-help --platform uplers
```

## Commands

```bash
python main.py chrome-profiles           # list Chrome profiles on your Mac
python main.py login --platform all      # one-time sign-in (if not using Chrome profile)
python main.py verify --platform all
python main.py run --platform all
```

## Safety

- Start with `dry_run: true` in config — searches jobs without submitting
- Use `max_jobs_per_run: 3` for first live test
- Delays default to 45–90s between applications
- Company ATS forms vary — some may need manual completion if the site uses CAPTCHA or custom fields
- High-volume auto-apply may violate platform ToS

## Project layout

```
wellfound-auto-apply/
├── config.example.yaml
├── cookies.wellfound.example.json
├── cookies.uplers.example.json
├── wellfound_auto_apply/
│   ├── cli.py              # --platform wellfound|uplers|all
│   ├── search.py           # Wellfound job search
│   ├── apply.py            # Wellfound apply
│   ├── uplers/
│   │   ├── search.py       # Uplers job discovery
│   │   └── apply.py        # Resolve external URL + apply
│   └── ats/
│       ├── detector.py     # Detect Greenhouse/Lever/etc.
│       └── apply.py        # Fill company ATS forms
```

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Uplers login fails | Re-export cookies from platform.uplers.com |
| No jobs found | Broaden keywords/skills; check you're logged in |
| External URL not captured | Job may use in-platform apply — check manually |
| ATS submit failed | Site may use CAPTCHA or non-standard form — apply manually |
| Missing email error | Set `user.email` in config.yaml |
