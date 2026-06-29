# Robust resume upload

Add a shared resume-upload helper that handles upload buttons opening a source menu (Local / Google Drive / Dropbox), picks the local-file option, sets the file, and verifies the upload actually succeeded. Adopt it across all apply/profile flows.

## Problem

Every flow uploads by targeting a hidden `input[type="file"]` and calling `set_input_files`:

- `jobs_auto_apply/ats/apply.py` `_upload_resume`
- `jobs_auto_apply/ats/workday.py` `_upload_resume`
- `jobs_auto_apply/wellfound/apply.py` `ensure_resume_on_profile`

None handle a button that opens a *source menu* (Local / Google Drive / Dropbox / Paste), and most never confirm the file landed. Only `jobs_auto_apply/naukri/resume.py` and `jobs_auto_apply/hirist/resume.py` click a trigger (via `expect_file_chooser`) and verify.

## New shared helper: `jobs_auto_apply/resume_upload.py`

Single entry point:

```python
async def upload_resume(page, resume_path, *, scope=None, save=False) -> bool
```

Logic, in order, stopping at first success that verifies:

1. **Direct input**: scan site-agnostic `input[type="file"]` selectors (id/name/accept variants) and `set_input_files` (works even when hidden, no OS picker).
2. **Source-menu button**: if a visible trigger matches an upload/attach regex (`upload|attach|add` + `resume|cv|file`), click it to open the menu. Then pick the local option matching `local|computer|my device|upload from computer|attach a file|browse`, explicitly avoiding cloud options (`google drive|dropbox|onedrive|box|paste|url`). Two sub-cases handled:
   - Local option is a label wrapping a hidden file input -> re-scan inputs and `set_input_files`.
   - Local option opens the OS picker -> wrap its click in `expect_file_chooser` and `set_files`.
3. **Verify** (either signal is enough):
   - `resume_path.name` (or its stem) text appears on the page/scope, OR
   - a success/checkmark indicator shows / "Uploaded" text / the upload trigger button disappears or changes label.
   - Poll briefly (a few short waits) to allow async upload to complete before deciding.
4. Return `True` only when a strategy attached AND verification passed; log a warning otherwise.

Reuse the existing trigger/verify regexes from naukri/hirist as the starting vocabulary.

## Adopt in flows

- `ats/apply.py`: replace `_upload_resume` body with a call to `upload_resume`; use returned bool to drive the multi-step submit loop.
- `ats/workday.py`: keep the Workday-specific `input[data-automation-id="file-upload-input-ref"]` selector as a first hint, then delegate to `upload_resume`.
- `wellfound/apply.py`: route `ensure_resume_on_profile` through `upload_resume(..., save=True)` (keeps the Save/Update click), and add an upload step in the apply-modal flow if a file field is present.
- `naukri/resume.py` + `hirist/resume.py`: delegate the attach step to the shared helper's strategies while keeping their existing save + "Uploaded On" verification (no behavior regression).

## Notes / decisions

- Cloud sources (Google Drive/Dropbox) are intentionally never clicked (they require external auth); the helper always prefers the local-file path.
- Verification uses filename-match OR success-UI, matching the chosen acceptance criteria.
- All clicks that could open the OS file picker are done inside `expect_file_chooser` so macOS Finder never stays open (matching the existing naukri/hirist guard).

## Todos

- [ ] Create `jobs_auto_apply/resume_upload.py` with `upload_resume()`: direct-input strategy, source-menu strategy (click trigger, pick local option, avoid cloud), `expect_file_chooser` handling, and success verification (filename OR success-UI), with brief polling.
- [ ] Refactor `ats/apply.py` `_upload_resume` to delegate to `upload_resume` and use its bool in the submit loop.
- [ ] Refactor `ats/workday.py` `_upload_resume` to try the workday automation-id input first, then delegate to `upload_resume`.
- [ ] Route wellfound `ensure_resume_on_profile` through `upload_resume(save=True)`; add a resume-upload step in the apply modal if a file field exists.
- [ ] Have `naukri/resume.py` and `hirist/resume.py` reuse the shared helper's attach strategies while keeping their save + "Uploaded On" verification.
- [ ] Run a dry/manual check against one Greenhouse-style form to confirm the source menu is handled and verification reports success.
