# Upload Audit — OMNI Automation
*Generated 2026-05-11 | Audited by Claude Code*

---

## Methodology

Searched:
- `templates/*.html` — `type="file"`, `accept=`, drag-drop handlers, FormData submit paths
- `app.py` — `request.files`, `encode_file()`, `variation_file_to_block()`, `ALLOWED_TYPES`, `VARIATION_ALLOWED_TYPES`, `MAX_CONTENT_LENGTH`, per-route validation logic

Backend MIME sets:
- `ALLOWED_TYPES` (app.py:799) — `image/jpeg`, `image/png`, `image/gif`, `image/webp`
- `VARIATION_ALLOWED_TYPES` (app.py:802) — above four + `image/heic`, `application/pdf`, `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`, `application/vnd.ms-excel`, `text/csv`, `application/vnd.openxmlformats-officedocument.wordprocessingml.document`, `application/msword`
- Global max size: `MAX_CONTENT_LENGTH = 40 MB` (app.py:36) — applies to the entire request, not per-file

---

## Inventory Table

| Task / Workflow | Upload Field Label | Field Name | Frontend `accept=` | Backend MIME enforcement | Backend validator | Magic-byte check | Max size | Max files | Required | Frontend location | Backend location |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **Arrears — General** | Arrears data | `file` | `.csv,text/csv` (arrears.html:221) | None — content decoded as UTF-8 CSV; no MIME check | None | No | 40 MB (global) | 1 | Yes | `arrears.html:221` | `app.py:4573` (`arrears_upload`) |
| **Arrears — Admin project upload** | Work queue CSV | `file` | `.csv,text/csv` (admin_projects.html:201) | None — content decoded as UTF-8 CSV; no MIME check | None | No | 40 MB (global) | 1 | Yes | `admin_projects.html:201` | `app.py:3935` (`upload_work_items`) |
| **Completions** | Contribution Schedule | `contribution_schedule` | `image/*` (completions.html:777) | `ALLOWED_TYPES` via `encode_file()` | app.py:2430 | Yes (magic bytes) | 40 MB (global) | Multiple | No | `completions.html:777` | `app.py:4400` |
| **Completions** | Creditor Claims | `creditor_claims` | `image/*` (completions.html:811) | `ALLOWED_TYPES` via `encode_file()` | app.py:2430 | Yes (magic bytes) | 40 MB (global) | Multiple | No | `completions.html:811` | `app.py:4400` |
| **Completions** | EOS | `eos` | `image/*` (completions.html:822) | `ALLOWED_TYPES` via `encode_file()` | app.py:2430 | Yes (magic bytes) | 40 MB (global) | Multiple | No | `completions.html:822` | `app.py:4400` |
| **Completions** | Modifications | `modifications` | `image/*` (completions.html:791) | `ALLOWED_TYPES` via `encode_file()` | app.py:2430 | Yes (magic bytes) | 40 MB (global) | Multiple | No (also accepts pasted text) | `completions.html:791` | `app.py:4400` |
| **Completions** | Modifications (pasted text) | `modifications_text` | N/A — textarea | None — plain text field | None | N/A | 40 MB (global) | 1 | No | `completions.html` (textarea) | `app.py:4393` |
| **Completions** | R&P | `rp` | `image/*` (completions.html:802) | `ALLOWED_TYPES` via `encode_file()` | app.py:2430 | Yes (magic bytes) | 40 MB (global) | Multiple | No | `completions.html:802` | `app.py:4400` |
| **Completions** | VMOC Modifications | `vmoc_modifications` | `image/*` (completions.html:855) | `ALLOWED_TYPES` via `encode_file()` | app.py:2441 | Yes (magic bytes) | 40 MB (global) | Multiple | Conditional (VMOC_UNAGREED only) | `completions.html:855` | `app.py:4441` |
| **Completions** | VMOC Modifications (pasted text) | `vmoc_modifications_text` | N/A — textarea | None — plain text field | None | N/A | 40 MB (global) | 1 | Conditional (VMOC_UNAGREED only) | `completions.html` (textarea) | `app.py:4444` |
| **Completions (legacy — unrouted)** | R&P (image mode) | `rp` | `image/*` (index.html:375) | `ALLOWED_TYPES` via `encode_file()` | app.py:2430 | Yes (magic bytes) | 40 MB (global) | Multiple | No | `index.html:375` | `app.py:4400` |
| **Completions (legacy — unrouted)** | R&P (PDF mode) | `rp` | `.pdf,application/pdf` (index.html:383) | PDF path: no MIME check beyond sniffing ext; base64-encoded as `application/pdf` | app.py:4420 | No | 40 MB (global) | 1 | No | `index.html:383` | `app.py:4420` |
| **Income & Expenditure** | I&E Document | `ie_document` | `image/*,.pdf,.xlsx,.xls,.csv,.docx,.doc` (variations.html:664) | `VARIATION_ALLOWED_TYPES` via `variation_file_to_block()` | app.py:2456 | No | 40 MB (global) | Multiple | Yes | `variations.html:664` | `app.py:3534` |
| **Parker Philips Arrears** | Cases in Arrears | `cases_in_arrears` | `.xlsx,.xlsm` (pp_arrears.html:331) | None — saved to temp file by filename; no MIME check | None | No | 40 MB (global) | 1 | Yes | `pp_arrears.html:331` | `app.py:4889` |
| **Parker Philips Arrears** | IVA Fees | `iva_fees` | `.xlsx,.xlsm` (pp_arrears.html:317) | None — saved to temp file by filename; no MIME check | None | No | 40 MB (global) | 1 | Yes | `pp_arrears.html:317` | `app.py:4889` |
| **Parker Philips Arrears** | TD Fees | `td_fees` | `.xlsx,.xlsm` (pp_arrears.html:324) | None — saved to temp file by filename; no MIME check | None | No | 40 MB (global) | 1 | Yes | `pp_arrears.html:324` | `app.py:4889` |
| **Parker Philips Arrears** | Total Live Cases | `total_live_cases` | `.xls,.xlsx` (pp_arrears.html:345) | None — saved to temp file by filename; no MIME check | None | No | 40 MB (global) | 1 | Yes | `pp_arrears.html:345` | `app.py:4889` |
| **Parker Philips Arrears** | WF Arrears | `wf_arrears` | `.xls,.xlsx` (pp_arrears.html:338) | None — saved to temp file by filename; no MIME check | None | No | 40 MB (global) | 1 | Yes | `pp_arrears.html:338` | `app.py:4889` |
| **Terminations** | Contribution Schedule | `contribution_schedule` | `image/*` (terminations.html:344) | `ALLOWED_TYPES` via `encode_file()` | app.py:2430 | Yes (magic bytes) | 40 MB (global) | Multiple | No | `terminations.html:344` | `app.py:2783` |
| **Terminations** | EOS | `eos` | `image/*` (terminations.html:362) | `ALLOWED_TYPES` via `encode_file()` | app.py:2430 | Yes (magic bytes) | 40 MB (global) | Multiple | No | `terminations.html:362` | `app.py:2783` |
| **Terminations** | Modifications | `modifications` | `image/*` (terminations.html:353) | `ALLOWED_TYPES` via `encode_file()` | app.py:2430 | Yes (magic bytes) | 40 MB (global) | Multiple | No | `terminations.html:353` | `app.py:2783` |
| **Terminations** | R&P | `rp` | `image/*` (terminations.html:335) | `ALLOWED_TYPES` via `encode_file()` | app.py:2430 | Yes (magic bytes) | 40 MB (global) | Multiple | No | `terminations.html:335` | `app.py:2783` |
| **Terminations** | VMOC Modifications | `vmoc_modifications` | `image/*` (terminations.html:407) | `ALLOWED_TYPES` via `encode_file()` | app.py:2809 | Yes (magic bytes) | 40 MB (global) | Multiple | Conditional (VMOC_UNAGREED only) | `terminations.html:407` | `app.py:2803` |
| **Variations / EOS Generator** | Agreed EOS | `agreed_eos` | `image/*,.pdf,.xlsx,.xls,.csv,.docx,.doc` (variations.html:604) | `VARIATION_ALLOWED_TYPES` via `variation_file_to_block()` | app.py:2456 | No | 40 MB (global) | Multiple | No | `variations.html:604` | `app.py:2966` |
| **Variations / EOS Generator** | Chart of Accounts | `chart_of_accounts` | `image/*,.pdf,.xlsx,.xls,.csv,.docx,.doc` (variations.html:622) | `VARIATION_ALLOWED_TYPES` via `variation_file_to_block()` | app.py:2456 | No | 40 MB (global) | Multiple | No | `variations.html:622` | `app.py:2966` |
| **Variations / EOS Generator** | Schedule of Modifications | `modifications` | `image/*,.pdf,.xlsx,.xls,.csv,.docx,.doc` (variations.html:613) | `VARIATION_ALLOWED_TYPES` via `variation_file_to_block()` | app.py:2456 | No | 40 MB (global) | Multiple | No | `variations.html:613` | `app.py:2966` |

---

## Notes

### 1. Uploads with no backend file-type enforcement (security risk)

| Field | Route | Risk |
|---|---|---|
| `file` (Arrears CSV) | `POST /api/arrears/upload` | Frontend restricts to `.csv,text/csv` only. Backend calls `f.read().decode("utf-8-sig")` with no MIME or extension check — any file that decodes as UTF-8 text will be processed as CSV. A crafted non-CSV file could trigger unexpected `csv.DictReader` behaviour. |
| `file` (Work queue CSV) | `POST /api/work-items/upload` | Same pattern as above — `f.read().decode("utf-8-sig")` with no content-type check. |
| `iva_fees`, `td_fees`, `cases_in_arrears`, `wf_arrears`, `total_live_cases` (Parker Philips) | `POST /api/pp/upload` | Frontend restricts to `.xlsx/.xlsm/.xls`. Backend writes directly to a temp file using `f.save(dest)` with no MIME check. The downstream `run_pipeline()` will fail if given a non-Excel file, but the failure mode is uncontrolled — no clean 400 error is guaranteed. |

### 2. Uploads with no per-file max-size limit

All uploads rely solely on Flask's global `MAX_CONTENT_LENGTH = 40 MB` (app.py:36), which applies to the entire request body. There is no per-file size cap. A request with five images each just under 40 MB cannot exist (the request limit would be hit), but the 40 MB limit is generous for individual image slots — a single image in a Completions or Terminations upload could be up to 40 MB with no additional check.

### 3. Magic-byte validation gap: `variation_file_to_block()` is not covered

`encode_file()` (Completions, Terminations) has magic-byte validation via `_detect_image_mime()` (app.py:2415). `variation_file_to_block()` (Variations, I&E) does **not** call `_detect_image_mime()` — it trusts the declared `content_type` only (app.py:2442–2456). Variations and I&E uploads accept PDFs and Office documents in addition to images, so image magic-byte detection would need extending before it could be applied there.

### 4. Frontend/backend accept mismatch

| Field | Frontend `accept=` | Backend enforces | Mismatch |
|---|---|---|---|
| `rp` (Completions — `completions.html`) | `image/*` | `ALLOWED_TYPES` (images only) | No mismatch, but backend supports PDF for `rp` (app.py:4420); the frontend never offers it. Users cannot submit an R&P as PDF via the completions page. **The PDF path was built in `index.html` (unrouted) but never ported to `completions.html`.** |
| `rp` (Completions — `index.html`, unrouted) | `image/*` + `.pdf,application/pdf` (two separate inputs) | PDF path skips `encode_file()` — no MIME or magic-byte check | PDF mode has no backend MIME validation; any file named `.pdf` is accepted. |
| `iva_fees` / `td_fees` / `cases_in_arrears` (PP) | `.xlsx,.xlsm` | None | Frontend restricts; backend does not — frontend-only control. |
| `wf_arrears` / `total_live_cases` (PP) | `.xls,.xlsx` | None | Frontend restricts; backend does not — frontend-only control. |
| `ie_document` (I&E) | `image/*,.pdf,.xlsx,.xls,.csv,.docx,.doc` | `VARIATION_ALLOWED_TYPES` | `.xlsm` not in frontend `accept=` but is in `VARIATION_ALLOWED_TYPES` — minor, harmless. `.heic` in backend but not frontend. |

### 5. Unrouted `index.html` upload UI

`index.html` contains a fully-featured upload UI (with PDF mode for R&P, paste-text mode for Modifications, two-question VMOC flow, and VMOC Modifications card) that targets `POST /analyze`. However, the `/completions` route renders `completions.html`, not `index.html`. `index.html` is not rendered by any route. All work done to `index.html` in previous sessions is effectively dead code unless the route is updated or the file is deleted. The R&P PDF mode in particular was only ever wired into `index.html`.

### 6. Duplicate upload patterns — consolidation candidates

The following field names and validation logic appear identically in Completions and Terminations, backed by the same `encode_file()` function and `ALLOWED_TYPES` set:

- `contribution_schedule` — image-only, multiple, optional
- `modifications` — image-only, multiple, optional (terminations); image-or-paste (completions)
- `eos` — image-only, multiple, optional
- `rp` — image-only, multiple, optional
- `vmoc_modifications` — image-only, multiple, conditional

These could be consolidated into a shared upload-card component (Jinja2 macro or JS module) rather than duplicated HTML/CSS/JS across three templates (`completions.html`, `terminations.html`, and the unrouted `index.html`).
