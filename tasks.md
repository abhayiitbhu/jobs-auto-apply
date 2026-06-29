# Tasks: Robust resume upload

Derived from [plan.md](plan.md). Add a shared resume-upload helper and adopt it across all apply/profile flows.

## 1. Create shared helper `jobs_auto_apply/resume_upload.py`

- [ ] Add single entry point `async def upload_resume(page, resume_path, *, scope=None, save=False) -> bool`.
- [ ] Strategy 1 — Direct input: scan site-agnostic `input[type="file"]` selectors (id/name/accept variants) and call `set_input_files` (works even when hidden, no OS picker).
- [ ] Strategy 2 — Source-menu button:
  - [ ] Detect a visible trigger matching an upload/attach regex (`upload|attach|add` + `resume|cv|file`) and click it to open the menu.
  - [ ] Pick the local option (`local|computer|my device|upload from computer|attach a file|browse`).
  - [ ] Explicitly avoid cloud options (`google drive|dropbox|onedrive|box|paste|url`).
  - [ ] Sub-case A — local option is a label wrapping a hidden file input: re-scan inputs and `set_input_files`.
  - [ ] Sub-case B — local option opens the OS picker: wrap the click in `expect_file_chooser` and `set_files`.
- [ ] Strategy 3 — Verify (either signal is enough):
  - [ ] `resume_path.name` (or its stem) text appears on the page/scope, OR
  - [ ] a success/checkmark indicator shows / "Uploaded" text / the upload trigger disappears or changes label.
  - [ ] Poll briefly (a few short waits) to allow async upload to complete before deciding.
- [ ] Return `True` only when a strategy attached AND verification passed; log a warning otherwise.
- [ ] Reuse the existing trigger/verify regexes from naukri/hirist as starting vocabulary.

## 2. Adopt in apply/profile flows

- [ ] `jobs_auto_apply/ats/apply.py`: replace `_upload_resume` body with a call to `upload_resume`; use the returned bool to drive the multi-step submit loop.
- [ ] `jobs_auto_apply/ats/workday.py`: keep the Workday-specific `input[data-automation-id="file-upload-input-ref"]` selector as a first hint, then delegate to `upload_resume`.
- [ ] `jobs_auto_apply/wellfound/apply.py`: route `ensure_resume_on_profile` through `upload_resume(..., save=True)` (keeps the Save/Update click); add an upload step in the apply-modal flow if a file field is present.
- [ ] `jobs_auto_apply/naukri/resume.py` + `jobs_auto_apply/hirist/resume.py`: delegate the attach step to the shared helper's strategies while keeping their existing save + "Uploaded On" verification (no behavior regression).

## 3. Validate

- [ ] Run a dry/manual check against one Greenhouse-style form to confirm the source menu is handled and verification reports success.

## Notes / decisions

- Cloud sources (Google Drive/Dropbox) are intentionally never clicked (they require external auth); the helper always prefers the local-file path.
- Verification uses filename-match OR success-UI.
- All clicks that could open the OS file picker run inside `expect_file_chooser` so macOS Finder never stays open (matching the existing naukri/hirist guard).
