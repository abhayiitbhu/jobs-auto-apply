# Instructions: Robust resume upload

Implementation instructions for adding a shared resume-upload helper and adopting it across all apply/profile flows. Follow these steps in order. Each step lists the file, what to change, and the acceptance check.

## Goal

Replace the scattered, fragile resume-upload logic with one shared helper that:

- Sets files on a (possibly hidden) `input[type="file"]` directly when one exists.
- Handles an upload/attach button that opens a source menu (Local / Google Drive / Dropbox / Paste) by clicking the local-file option and skipping cloud options.
- Wraps any click that could open the OS file picker in `expect_file_chooser` so macOS Finder never stays open.
- Verifies the upload actually landed (filename text OR success UI) before reporting success.

## Step 1 - Create `jobs_auto_apply/resume_upload.py`

Add a new module exposing a single entry point:

```python
async def upload_resume(page, resume_path, *, scope=None, save=False) -> bool
```

- `page`: Playwright `Page`.
- `resume_path`: `pathlib.Path` to the local resume; bail out early (return `False`, log warning) if it does not exist.
- `scope`: optional locator/frame to constrain the search (defaults to `page`).
- `save`: when `True`, click a Save/Update/Submit button after attaching (profile flows).

Implement the strategies in this order, stopping at the first that attaches AND verifies:

1. **Direct input** - scan site-agnostic `input[type="file"]` selectors (id / name / accept variants, then bare `input[type="file"]`) and call `set_input_files`. Works even when the input is hidden; no OS picker.
2. **Source-menu button** - if no direct input worked, find a visible trigger matching an upload/attach regex (`upload|attach|add` near `resume|cv|file`) and click it to open the menu. Then pick the local option matching `local|computer|my device|upload from computer|attach a file|browse`, explicitly avoiding cloud options (`google drive|dropbox|onedrive|box|paste|url`). Two sub-cases:
   - Local option wraps a hidden file input -> re-scan inputs and `set_input_files`.
   - Local option opens the OS picker -> wrap the click in `async with page.expect_file_chooser(...)` and `chooser.set_files(...)`.
3. **Verify** (either signal is sufficient), polling with a few short waits to let async uploads finish:
   - `resume_path.name` (or its stem) text appears on the page/scope, OR
   - a success/checkmark indicator / "Uploaded" text shows, or the upload trigger disappears / changes label.
4. Return `True` only when a strategy attached AND verification passed; otherwise log a warning and return `False`.

Reuse the existing vocabulary from [jobs_auto_apply/naukri/resume.py](jobs_auto_apply/naukri/resume.py) and [jobs_auto_apply/hirist/resume.py](jobs_auto_apply/hirist/resume.py) as the starting regexes:

- Trigger regex: `update\s*resume|upload\s*resume|attach\s*cv|...` (extend with `add`, `attach a file`).
- File-input selectors: `#attachCV`, accept-based variants, then bare `input[type="file"]`.
- Save regex: `^(save|submit|done)$|save\s*changes|update\s*profile`.
- Uploaded-confirmation regex: `uploaded\s*on` (and filename match).

Keep the `expect_file_chooser(timeout=...)` + `set_files` pattern and the per-locator try/except from `_attach_via_file_chooser` in the naukri/hirist modules.

Acceptance: module imports cleanly; `upload_resume` returns `False` (with a warning) when no field exists and `True` when a file is attached and verified.

## Step 2 - Refactor `jobs_auto_apply/ats/apply.py`

Replace the body of `_upload_resume` (currently lines 61-72) with a call to the shared helper, and use the returned bool to drive the multi-step submit loop.

Current direct-input-only version:

```61:72:jobs_auto_apply/ats/apply.py
async def _upload_resume(page: Page, resume_path: Path) -> bool:
    file_input = page.locator('input[type="file"]')
    if await file_input.count() == 0:
        return False
    for i in range(await file_input.count()):
        inp = file_input.nth(i)
        try:
            await inp.set_input_files(str(resume_path))
            return True
        except PlaywrightTimeout:
            continue
    return False
```

- Delegate to `upload_resume(page, resume_path)` (import from `..resume_upload`).
- In `apply_on_company_site` (lines 159, 171), capture the bool so the submit loop can avoid re-submitting when the resume never attached on a form that requires it.

Acceptance: Greenhouse/Lever/Ashby forms that use a source-menu button now upload successfully; the submit loop still confirms via success phrases.

## Step 3 - Refactor `jobs_auto_apply/ats/workday.py`

Keep the Workday-specific selector as a first hint, then delegate.

Current version:

```155:167:jobs_auto_apply/ats/workday.py
async def _upload_resume(page: Page, resume_path: Path) -> bool:
    file_input = page.locator('input[data-automation-id="file-upload-input-ref"], input[type="file"]')
    if await file_input.count() == 0:
        return False
    for i in range(await file_input.count()):
        inp = file_input.nth(i)
        try:
            await inp.set_input_files(str(resume_path))
            await page.wait_for_timeout(2500)
            return True
        except PlaywrightTimeout:
            continue
    return False
```

- Try `input[data-automation-id="file-upload-input-ref"]` first (existing behavior), then fall through to `upload_resume(page, resume_path)`.

Acceptance: existing Workday uploads keep working; non-standard Workday upload widgets fall back to the shared helper.

## Step 4 - Route wellfound through the helper

In [jobs_auto_apply/wellfound/apply.py](jobs_auto_apply/wellfound/apply.py), `ensure_resume_on_profile` (lines 340-356) currently sets files directly and clicks Save.

- Route it through `upload_resume(page, resume_path, save=True)` (keeps the Save/Update click inside the helper).
- Add a resume-upload step in the apply-modal flow if a file field is present.

Acceptance: profile upload still saves; apply modal uploads a resume when a file field is shown.

## Step 5 - Have naukri/hirist reuse the shared attach strategies

In [jobs_auto_apply/naukri/resume.py](jobs_auto_apply/naukri/resume.py) and [jobs_auto_apply/hirist/resume.py](jobs_auto_apply/hirist/resume.py):

- Delegate the attach step (direct input + file-chooser) to the shared helper's strategies.
- Keep their existing save + "Uploaded On" / "last updated" verification so there is no behavior regression.

Acceptance: naukri/hirist profile uploads behave identically (same save + date-verification), just sharing the attach code.

## Step 6 - Manual/dry check

Run a manual check against one Greenhouse-style form to confirm:

- the source menu (Local / Drive / Dropbox) is handled and the local option is chosen,
- cloud options are never clicked,
- verification reports success via filename match or success UI.

## Decisions / constraints

- Cloud sources (Google Drive / Dropbox / OneDrive / Box / Paste) are intentionally never clicked - they require external auth; always prefer the local-file path.
- Verification uses filename-match OR success-UI.
- Every click that could open the OS file picker must be inside `expect_file_chooser` so macOS Finder never stays open (matching the existing naukri/hirist guard).

## Todos

- [x] Create `jobs_auto_apply/resume_upload.py` with `upload_resume()` (direct-input, source-menu, `expect_file_chooser`, verify + polling).
- [x] Refactor `ats/apply.py` `_upload_resume` to delegate and capture the bool in the submit loop.
- [x] Refactor `ats/workday.py` `_upload_resume` to try the automation-id input first, then delegate.
- [x] Route wellfound `ensure_resume_on_profile` through `upload_resume(save=True)`; add an apply-modal upload step.
- [x] Have `naukri/resume.py` and `hirist/resume.py` reuse the shared attach strategies (via `attach_resume()`) while keeping save + "Uploaded On" verification.
- [ ] Manual check against one Greenhouse-style form (requires a live browser).
