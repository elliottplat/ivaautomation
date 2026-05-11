# Upload Fixes Plan

**Status:** Awaiting approval — no code has been changed yet.

---

## Scope 1 — Modifications paste-text in Terminations

### Current behaviour

| Layer | File | Lines | State |
|---|---|---|---|
| completions.html (HTML) | `templates/completions.html` | 782–795 | Inline toggle: Screenshot / Paste text buttons swap between an upload area and a `<textarea id="mods-paste-area">` |
| completions.html (JS) | `templates/completions.html` | 1086–1091 | `setModsMode(mode)` shows/hides elements |
| completions backend | `app.py` | 4393 | `mods_text = request.form.get("modifications_text", "").strip()` — pasted text read and injected into prompt |
| terminations.html (HTML) | `templates/terminations.html` | 349–356 | Single `<input type="file" accept="image/*">` — no paste-text option |
| terminations backend | `app.py` | 2782–2812 | No `modifications_text` read; all fields via `encode_file()` (images only) |

### Proposed change

1. **terminations.html** — Wrap the Modifications card upload area in a `<div id="term-mods-screenshot-area">`, add a `<div class="input-mode-toggle">` with Screenshot/Paste text buttons, and add `<textarea class="paste-area" id="term-mods-paste-area">`. Mirror the exact pattern from `completions.html:782–795`.
2. **terminations.html JS** — Add `setTermModsMode(mode)` function (same shape as `setModsMode()`).
3. **terminations.html form submit** — Before `slots.forEach(...)`, read `document.getElementById('term-mods-paste-area').value`; if non-empty and paste mode is active, append `fd.append('modifications_text', text)` instead of any modifications file.
4. **app.py — analyze_termination** (~line 2782) — Before the slot loop, read `modifications_text = request.form.get("modifications_text", "").strip()`. In the slot loop, skip uploading `modifications` when `modifications_text` is set; instead inject a text block `"--- Modifications (pasted text) ---\n{modifications_text}"`.

### Risks / open questions

- The terminations prompt already expects a Modifications document. Verify the prompt can handle plain pasted text without images (it can — completions prompt already does this).
- No change to backend validation logic; modifications remains optional (not mandatory for paste mode).

---

## Scope 2 — Terminations: R&P PDF/DOC/DOCX + helper messages + mandatory validation

### Current behaviour

| Layer | File | Lines | State |
|---|---|---|---|
| R&P input | `templates/terminations.html` | 333–338 | `accept="image/*"` — screenshots only |
| Contribution Schedule | `templates/terminations.html` | 341–347 | `accept="image/*"` |
| Modifications | `templates/terminations.html` | 349–356 | `accept="image/*"` |
| EOS | `templates/terminations.html` | 358–365 | `accept="image/*"` |
| VMOC Modifications | `templates/terminations.html` | 402–411 | `accept="image/*"` |
| Backend — all slots | `app.py` | 2794–2798 | All fields go through `encode_file()` — images only; PDF would raise `ValueError` |

### Proposed change

**Frontend (`templates/terminations.html`):**

1. **R&P card** — Add a mode toggle (Screenshot / PDF or DOC) above the upload area. In Screenshot mode: keep current `accept="image/*"`. In PDF/DOC mode: swap to a second `<input id="inp-rp-doc" accept=".pdf,.docx,.doc,application/pdf,...">`. Add helper text: *"Accepted: JPEG, PNG, GIF, WebP (screenshots) or PDF, DOCX, DOC"*.
2. **All other cards** — Add helper text below each upload area: *"Accepted: JPEG, PNG, GIF, WebP screenshots"*.
3. **Front-end mandatory validation** — In the submit handler, verify all 4 mandatory slots (`rp`, `contribution_schedule`, `modifications`, `eos`) have at least one file (or paste text for modifications) before allowing submit. Alert the user if any are missing.

**Backend (`app.py` — `analyze_termination`, ~line 2782):**

4. In the slot loop for `rp`, detect whether any uploaded file has a PDF/DOC MIME type. If so, call `variation_file_to_block(page)` instead of `encode_file(page)`. This already handles PDF as a `document` block and DOC/DOCX as a text placeholder. The `encode_file()` path remains for image uploads.
5. No change needed for other slots — they remain image-only.

### Risks / open questions

- The Termination prompt was designed around screenshot images. PDF and DOC blocks are already understood by Claude (via `document` type or text placeholder). Verify prompt output quality is unchanged.
- DOCX sent as a text placeholder `[Attached file: ...]` gives Claude no actual content — acceptable only if users understand this limitation. Consider adding a note in the UI for DOC/DOCX files.
- `analyze_termination` currently has no `modifications_text` support (see Scope 1). R&P PDF addition is independent and can be committed separately.

---

## Scope 3 — Completions: R&P PDF/DOC/DOCX + helper messages + mandatory validation

### Current behaviour

| Layer | File | Lines | State |
|---|---|---|---|
| R&P input | `templates/completions.html` | 799–805 | `accept="image/*"` — screenshots only |
| Backend R&P PDF path | `app.py` | 4420–4426 | PDF **already supported**: if `mime == "application/pdf"`, sends a `document` block. No code change needed here. |
| Other fields (contribution_schedule, creditor_claims, eos, vmoc_modifications) | `templates/completions.html` | 775–778, 807–814, 820–823, 853–857 | `accept="image/*"` only |

### Proposed change

**Frontend (`templates/completions.html`):**

1. **R&P card** — Add mode toggle (Screenshot / PDF or DOC) above the upload area. In Screenshot mode: `accept="image/*"`. In PDF/DOC mode: second input `accept=".pdf,.docx,.doc,application/pdf,..."`. Add JS `setRpMode(mode)` function. Add helper text: *"Accepted: JPEG, PNG, GIF, WebP (screenshots) or PDF, DOCX, DOC"*.
2. **Front-end form submit** — When PDF mode is active for R&P, append the document file instead of image file. Backend already handles this.
3. **Helper messages** — Add a small `<p class="upload-helper">` below each upload area for all 5 fields showing accepted types.
4. **Mandatory validation** — Verify `contribution_schedule`, `rp`, `eos` have at least one file before submit. `modifications` is optional (can use paste text). `creditor_claims` is optional.

**Backend:** No changes required — R&P PDF path exists at app.py:4420.

### Risks / open questions

- The Completions backend already handles PDF for R&P. DOC/DOCX for R&P would fall through to `encode_file()` and raise a `ValueError`. If we add DOC/DOCX to the frontend, we must also handle it in the backend (same as Scope 2 — use `variation_file_to_block()`).
- Decision needed: add DOC/DOCX support to completions R&P backend, or advertise PDF-only for documents? Recommending PDF-only for simplicity (DOC/DOCX gives Claude no readable content anyway).

---

## Scope 4 — Completions EOS VMOC

### Current behaviour

Fully implemented in `templates/completions.html`:

| Feature | Lines |
|---|---|
| Q1 radio: "Is this EOS from a VMOC?" | 826–833 |
| Q2 radio: "Is this a Revised Agreed EOS?" | 834–840 |
| `getEosState()` returns `NON_VMOC` / `VMOC_AGREED` / `VMOC_UNAGREED` | 1061–1066 |
| `applyEosState()` shows/hides Q2 and VMOC Modifications card | 1068–1083 |
| VMOC Modifications card with Screenshot / Paste text toggle | 846–860 |

Backend also handles all three states at app.py:4438–4456.

### Proposed change

**No action required.** This scope was completed when the VMOC logic was ported from `index.html` to `completions.html`.

The only follow-up is the index.html decision covered in Scope 5.

---

## Scope 5 — Security

### 5a — Magic-byte gap in `variation_file_to_block()`

| Item | Location |
|---|---|
| Function | `app.py:2442–2465` |
| Gap | Images accepted via `VARIATION_ALLOWED_TYPES` but `_detect_image_mime()` is never called — a JPEG could be sent with `content_type="image/png"` and pass validation |

**Proposed change:** In `variation_file_to_block()`, after reading `data` (currently only base64-encoded), call `_detect_image_mime(data)` for image/* types (same check as `encode_file()` at app.py:2433–2438). Raise `ValueError` on mismatch.

Implementation note: read bytes first, then encode to base64, then validate magic bytes for images. Add `image/heic` detection to `_detect_image_mime()` (magic bytes: `....ftyp` HEIF container — check `data[4:8] == b"ftyp"`).

**Risk:** Low. Only affects variation/I&E uploads. No change to user-visible behaviour for valid files.

### 5b — Arrears CSV: no MIME/extension validation

| Item | Location |
|---|---|
| Route | `app.py:4561` — `arrears_upload()` |
| Gap | `f.read().decode("utf-8-sig")` at line 4577 with no prior check that `f` is actually a CSV — an attacker could upload any file and attempt decode |

**Proposed change:** Before `f.read()`, verify `(f.content_type in ("text/csv", "application/csv") or (f.filename or "").lower().endswith(".csv"))`. If neither, return `jsonify({"error": "Only CSV files are accepted."})`, 400.

**Risk:** Minimal. The existing `csv.DictReader` would reject non-CSV content anyway, but the explicit check prevents unnecessary processing and gives a clearer error message.

### 5c — Work Queue CSV: no MIME/extension validation

| Item | Location |
|---|---|
| Route | `app.py:3926` — `upload_work_items()` |
| Gap | Same pattern as 5b — `f.read().decode("utf-8-sig")` at line 3939 with no type check |

**Proposed change:** Same as 5b — add content-type / filename extension check before `f.read()`.

### 5d — PP Excel: files saved to disk without validation

| Item | Location |
|---|---|
| Route | `app.py:4861` — `pp_upload()` |
| Gap | Five files saved to a temp directory via `f.save(dest)` at line 4891 with no type check. An uploaded file with a malicious name or type could be saved and then passed to `run_pipeline()` |

**Proposed change:** For each of the 5 required files, verify the filename ends with `.xlsx` or `.xls` before calling `f.save()`. Also sanitise the filename used as `dest` — use the key name (`iva_fees`, etc.) plus a fixed `.xlsx` suffix rather than `f.filename or key` to prevent directory traversal.

```python
dest = os.path.join(tmp_dir, f"{key}.xlsx")
ext = (f.filename or "").rsplit(".", 1)[-1].lower()
if ext not in ("xlsx", "xls"):
    return jsonify({"error": f"File '{key}' must be an Excel file (.xlsx or .xls)"}), 400
f.save(dest)
```

**Risk:** Medium — this is an admin-only endpoint (`admin` / `uploader` roles), but defence in depth is still warranted.

### 5e — I&E .heic: frontend vs backend alignment

| Item | Location |
|---|---|
| Backend | `app.py:802–803` — `image/heic` in `VARIATION_ALLOWED_TYPES` |
| Frontend | `templates/variations.html:664` — `accept="image/*,.pdf,.xlsx,.xls,.csv,.docx,.doc"` |
| Frontend JS | `templates/variations.html:2375` — `ALLOWED_EXTS` includes `heic` |

**Assessment:** `image/*` in `accept=` is browser-interpreted and will match `.heic` on macOS Safari and iOS. The JS validation at line 2375 explicitly allows `heic`. **No gap found — no action required.**

### 5f — `index.html` dead code

| Item | Detail |
|---|---|
| File | `templates/index.html` |
| Status | Not routed anywhere — `GET /` serves `home.html` (app.py:2687); `GET /completions` serves `completions.html`; no route references `index.html` |
| Contains | A historical copy of the completions UI with a paste modal implementation and R&P PDF toggle — these have since been ported to `completions.html` |

**Proposed change:** Delete `templates/index.html`. It poses no active security risk but adds confusion for future contributors who might edit it believing it is live. Before deleting, confirm no Jinja `{% include %}` or `{% extends %}` references exist (a quick grep confirms none).

**Risk:** None — file is not served.

---

## Implementation order (Phase 2 commit grouping)

| Commit | Scope | Files |
|---|---|---|
| 1 | Security quick wins (5b, 5c, 5d) | `app.py` |
| 2 | Magic-byte gap + heic detection (5a) | `app.py` |
| 3 | Terminations paste-text Modifications (Scope 1) | `app.py`, `templates/terminations.html` |
| 4 | Terminations R&P PDF mode + helpers + mandatory (Scope 2) | `app.py`, `templates/terminations.html` |
| 5 | Completions R&P PDF mode + helpers + mandatory (Scope 3) | `templates/completions.html` |
| 6 | Delete index.html dead code (5f) | `templates/index.html` |

---

*Awaiting explicit approval before any code changes.*
