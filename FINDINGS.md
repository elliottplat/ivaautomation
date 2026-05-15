# OMNI Automation — Findings
*Generated 2026-05-11 | Phase 2 of audit*

---

## 1. Bugs & Correctness

**F-01 · HIGH · Database connections leak on exception paths**
- **Location:** `app.py` — every route that opens a connection (67 `get_db_conn()` calls, only 91 `conn.close()` calls, but many close calls are inside `if os.environ.get("DATABASE_URL"):` blocks that can skip the close on early returns or exceptions).
- **Symptom:** Under sustained load or when upstream exceptions fire mid-handler, open connections accumulate until PostgreSQL's `max_connections` is exhausted, causing `OperationalError: too many connections` for all subsequent requests.
- **Root cause:** `get_db_conn()` returns a raw `psycopg2` connection with no pooling and no context manager. Pattern throughout the codebase:
  ```python
  conn = get_db_conn()
  with conn.cursor(...) as cur:
      cur.execute(...)
  conn.commit()
  conn.close()   # ← NEVER reached if exception fires above
  ```
- **Fix:** Wrap every connection in a `try/finally`:
  ```python
  conn = get_db_conn()
  try:
      with conn.cursor(cursor_factory=RealDictCursor) as cur:
          ...
      conn.commit()
  finally:
      conn.close()
  ```
  Or better: replace `get_db_conn()` with `psycopg2.pool.ThreadedConnectionPool` (thread-safe, matches the gthread worker model). One pool, sized to `min=2, max=10`.
- **Effort:** M (pool setup) or S (try/finally fixup per route)

---

**F-02 · HIGH · `remember=True` sessions never expire**
- **Location:** `app.py:2430`
- **Symptom:** Login cookies are persistent with no `PERMANENT_SESSION_LIFETIME` set, no `SESSION_COOKIE_SECURE`, no `SESSION_COOKIE_SAMESITE`. A stolen cookie grants indefinite access. There is also no logout-all / session invalidation mechanism.
- **Root cause:** `login_user(user, remember=True)` without any session lifetime or secure cookie config.
- **Fix:**
  ```python
  app.config["PERMANENT_SESSION_LIFETIME"] = datetime.timedelta(hours=12)
  app.config["SESSION_COOKIE_SECURE"] = True      # HTTPS only
  app.config["SESSION_COOKIE_HTTPONLY"] = True    # block JS access
  app.config["SESSION_COOKIE_SAMESITE"] = "Lax"  # CSRF mitigation
  ```
  Consider `remember=False` — or explicit `REMEMBER_COOKIE_DURATION` — rather than indefinite.
- **Effort:** S

---

**F-03 · MEDIUM · Password reset tokens are not rate-limited**
- **Location:** `app.py:2471` — `forgot_password` route, public, no guard
- **Symptom:** Any actor can POST to `/forgot-password` with a valid username/email repeatedly, generating unlimited reset emails and invalidating each prior token. No lockout, no CAPTCHA, no send-frequency check.
- **Root cause:** No rate limiting or cooldown between reset requests.
- **Fix:** Add a per-email cooldown — before generating a new token, check `password_reset_expires > now - 5min`; if so, return "A reset email was already sent recently" without regenerating. Also add Flask-Limiter to the route (e.g., 5/hour per IP).
- **Effort:** S

---

**F-04 · MEDIUM · `init_db()` hardcoded default admin password**
- **Location:** `app.py:2263–2285` — reads `ADMIN_PASSWORD` from env, falls back to `"changeme"` if not set
- **Symptom:** Deployments without `ADMIN_PASSWORD` set silently use `"changeme"` as the admin password.
- **Root cause:** `os.environ.get("ADMIN_PASSWORD", "changeme")` with no startup assertion.
- **Fix:** Replace the default with an exception:
  ```python
  admin_pw = os.environ.get("ADMIN_PASSWORD")
  if not admin_pw:
      raise RuntimeError("ADMIN_PASSWORD env var must be set")
  ```
- **Effort:** S

---

**F-05 · MEDIUM · SSE streaming generator swallows DB save errors silently**
- **Location:** `app.py` — all streaming analysis routes (`generate()` generators at lines ~2700, ~2875, ~3010, ~3445, ~4340)
- **Symptom:** If the DB insert inside `generate()` fails (e.g., connection lost), the response has already started streaming. The error is `print()`-ed to stdout but no error signal reaches the client — the stream ends normally. The case is lost.
- **Root cause:** SSE generators commit to the HTTP response before DB operations succeed. Exceptions inside the generator after headers are sent can't propagate an HTTP error code.
- **Fix:** Accept the architectural constraint (can't change HTTP status mid-stream) but: (1) emit a final `data: {"error": "save_failed"}` event before closing; (2) add server-side logging (not just print); (3) consider a two-phase approach where DB save happens synchronously before the stream, or the stream sends a transaction ID the client can poll to confirm.
- **Effort:** M

---

**F-06 · LOW · `cases` table has no index on commonly filtered columns**
- **Location:** `app.py:3948` — `/api/cases` queries `WHERE task_type = %s AND submitted_by = %s ...`
- **Symptom:** Full table scan on every case list request. As the `cases` table grows (it stores full calculation results as TEXT), this degrades linearly.
- **Fix:** Add indexes:
  ```sql
  CREATE INDEX IF NOT EXISTS cases_task_type ON cases(task_type);
  CREATE INDEX IF NOT EXISTS cases_submitted_by ON cases(submitted_by);
  CREATE INDEX IF NOT EXISTS cases_review_status ON cases(review_status);
  ```
- **Effort:** S

---

## 2. Security

**F-07 · CRITICAL · Hardcoded fallback `SECRET_KEY`**
- **Location:** `app.py:23`
  ```python
  app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-key-please-set-in-production")
  ```
- **Symptom:** If `SECRET_KEY` is not set in the environment, Flask uses `"dev-key-please-set-in-production"` — a publicly known string. Anyone who knows this key can forge session cookies, granting admin access without credentials.
- **Root cause:** Insecure default chosen for developer convenience.
- **Fix:**
  ```python
  secret_key = os.environ.get("SECRET_KEY")
  if not secret_key:
      raise RuntimeError("SECRET_KEY env var must be set to a random value in production")
  app.config["SECRET_KEY"] = secret_key
  ```
- **Effort:** S

---

**F-08 · HIGH · No brute-force protection on login**
- **Location:** `app.py:2412` — `/login` route, public POST, no rate limit
- **Symptom:** Unlimited login attempts against any account. Username enumeration is also possible — failed logins due to "user not found" return the same error as "wrong password" (good) but response timing differs slightly (DB miss vs hash check).
- **Fix:** Add Flask-Limiter (`5/minute` per IP on the login route). Optionally add account lockout after N failures (store `failed_attempts` + `locked_until` in users table). Constant-time comparison already handled by `check_password_hash`.
- **Effort:** S

---

**F-09 · HIGH · No security response headers**
- **Location:** `app.py` — no `@app.after_request` adding security headers
- **Symptom:** The app sends no `Content-Security-Policy`, `X-Frame-Options`, `X-Content-Type-Options`, or `Strict-Transport-Security` headers. XSS attacks can load scripts from arbitrary origins; the app can be iframed for clickjacking.
- **Fix:** Add an `after_request` hook:
  ```python
  @app.after_request
  def add_security_headers(response):
      response.headers["X-Content-Type-Options"] = "nosniff"
      response.headers["X-Frame-Options"] = "DENY"
      response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
      response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
      # CSP: tighten once inline scripts are identified/nounced
      return response
  ```
- **Effort:** S

---

**F-10 · HIGH · File upload MIME type validated by content-type header only**
- **Location:** `app.py:2320–2324` — `encode_file()`:
  ```python
  media_type = file.content_type
  if media_type not in ALLOWED_TYPES:
      raise ValueError(...)
  ```
- **Symptom:** The `content_type` field comes from the HTTP request (set by the browser/client), not from the file content. A malicious uploader can send a file with `Content-Type: image/jpeg` that is actually a polyglot (e.g., a JPEG+script or a PHP file) and it will pass validation. The file is then base64-encoded and forwarded to Claude, but any server-side processing of the filename or content could be exploited.
- **Fix:** Use `python-magic` or the `imghdr` stdlib to verify the file magic bytes match the declared MIME type before processing. At minimum, validate the file extension against an allowlist.
- **Effort:** S

---

**F-11 · MEDIUM · `debug=True` in production startup path**
- **Location:** `app.py:6488`
  ```python
  if __name__ == "__main__":
      app.run(debug=True)
  ```
- **Symptom:** If the app is started directly (`python app.py`) rather than via gunicorn, the Werkzeug debugger is exposed — anyone who triggers an error gets an interactive Python console in the browser.
- **Fix:** `app.run(debug=os.environ.get("FLASK_DEBUG", "false").lower() == "true")`. This is low-risk in the Heroku/gunicorn deployment, but is a footgun for local/dev runners.
- **Effort:** S

---

**F-12 · MEDIUM · No CSRF protection on state-mutating API routes**
- **Location:** All `POST /api/*` routes
- **Symptom:** Any page the user visits while authenticated could trigger state-mutating POSTs (e.g., creating users, deleting cases) via a cross-site form submit. Flask-Login's session cookie is sent with cross-site requests unless `SameSite=Lax/Strict` is set (see F-02).
- **Fix:** F-02's `SESSION_COOKIE_SAMESITE = "Lax"` mitigates most of this. For full protection, add Flask-WTF CSRF tokens to all state-mutating forms, or verify `Origin`/`Referer` headers on API routes.
- **Effort:** M (combined with F-02 fix, S)

---

## 3. Performance

**F-13 · HIGH · No database connection pool — new connection per request**
- **Location:** `app.py:1662–1666` — `get_db_conn()` calls `psycopg2.connect()` directly
- **Symptom:** Each HTTP request that touches the database opens a new TCP connection to PostgreSQL and closes it when done. With 4 gunicorn threads and multiple concurrent users, this creates 4+ new connections per second under load — PostgreSQL connection overhead (~5ms) adds directly to request latency.
- **Fix:** Replace `get_db_conn()` with a `ThreadedConnectionPool`:
  ```python
  from psycopg2.pool import ThreadedConnectionPool
  _pool = None

  def get_db_conn():
      global _pool
      if _pool is None:
          _pool = ThreadedConnectionPool(2, 10, os.environ["DATABASE_URL"])
      return _pool.getconn()

  def release_db_conn(conn):
      _pool.putconn(conn)
  ```
  Then wrap every usage in `try/finally: release_db_conn(conn)`.
- **Effort:** M
- **Note:** This also fixes F-01 (leak risk) if combined with proper try/finally.

---

**F-14 · MEDIUM · `cases` result column stores full Claude output as TEXT with no pagination**
- **Location:** `app.py:3948` — `/api/cases` SELECTs `result` (full calculation text, up to 16k tokens of prose) for up to 100 rows
- **Symptom:** A single list request transfers up to `100 × 64KB = 6.4MB` of unneeded text to the frontend. As the cases table grows, this gets worse.
- **Fix:** The list query should select `result` only for detail view (`/api/cases/<id>`). The list query should select only summary columns: `id, case_number, created_at, task_type, review_status, submitted_by`. Add a `LIMIT/OFFSET` pagination mechanism.
- **Effort:** M

---

**F-15 · MEDIUM · `pandas` loaded for CSV parsing that could use stdlib `csv`**
- **Location:** `app.py` — arrears CSV upload parsing uses pandas
- **Symptom:** pandas adds ~40MB to the container and ~0.5–1s import overhead. The CSV parsing is simple (read rows, extract named columns) — the stdlib `csv.DictReader` does this in zero additional dependencies.
- **Fix:** Replace arrears CSV parsing with `csv.DictReader`. Keep openpyxl/xlrd for Excel support. Remove pandas from requirements.txt.
- **Effort:** M

---

**F-16 · LOW · In-memory DSS performance cache not safe for multi-worker deployments**
- **Location:** `app.py:6315` — `_perf_cache = {}` module-level dict
- **Symptom:** If a second gunicorn worker is ever added, each worker has its own `_perf_cache`. Cache invalidation calls on one worker don't propagate to others. Currently safe (1 worker) but a footgun for scaling.
- **Fix:** When/if scaling to multiple workers, move cache to Redis or Postgres (the daily rollup table is already the right place for this data).
- **Effort:** M (if/when needed)

---

## 4. Reliability

**F-17 · HIGH · Analysis routes have no timeout protection against Claude API hangs**
- **Location:** All streaming analysis routes
- **Symptom:** The gunicorn timeout is 300s. If the Anthropic API hangs (not overloaded, just slow), the generator blocks for the full 5 minutes, holding a gunicorn thread. With 4 threads and multiple hung requests, the app becomes unresponsive.
- **Fix:** Add `httpx_timeout` to the Anthropic client:
  ```python
  client = anthropic.Anthropic(
      api_key=os.environ.get("ANTHROPIC_API_KEY"),
      timeout=httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0),
  )
  ```
  120s read timeout is generous for streaming. The 300s gunicorn timeout should then be the last-resort backstop, not the first line of defence.
- **Effort:** S

---

**F-18 · MEDIUM · No health check endpoint**
- **Location:** `app.py` — no `GET /health` or `GET /ping` route
- **Symptom:** Load balancers and uptime monitors have no lightweight endpoint to probe. They either hit a full page route (triggers session checks, DB queries) or have no visibility at all.
- **Fix:**
  ```python
  @app.route("/health")
  def health():
      return {"status": "ok"}, 200
  ```
  Optional: add a DB connectivity check (`SELECT 1`) for deeper health.
- **Effort:** S

---

**F-19 · MEDIUM · `init_db()` schema migration is not idempotent for all DDL**
- **Location:** `app.py:1669–2252`
- **Symptom:** Most `ALTER TABLE ADD COLUMN IF NOT EXISTS` blocks are safe. But some `DO $$ BEGIN … END $$` trigger blocks use bare `ALTER TABLE ADD COLUMN` without `IF NOT EXISTS`, which will fail if re-run on an already-migrated database.
- **Fix:** Audit each `DO $$` block for missing `IF NOT EXISTS`. Long-term: adopt a proper migration tool (Alembic) rather than `init_db()` as a migration runner.
- **Effort:** S (audit) / L (Alembic migration)

---

## 5. Frontend UX

**F-20 · MEDIUM · Response text rendered as raw `textContent` — no Markdown formatting**
- **Location:** All analysis pages — `responseText.textContent += data.text`
- **Symptom:** Claude returns well-structured output with headers, tables, and bold text using Markdown. The UI strips all formatting, displaying a wall of plain text. This makes the output harder to review quickly.
- **Fix:** Pipe the response through a lightweight Markdown renderer (e.g., `marked.js`, ~10KB gzipped). Set `responseText.innerHTML = marked.parse(fullText)` after the stream ends, or incrementally using a streaming parser.
- **Effort:** S

---

**F-21 · MEDIUM · No error state for fetch failures on data-loading pages**
- **Location:** DSS dashboard, arrears pages, home page — all fetch data on load
- **Symptom:** If any initial data fetch fails (network error, 500), the page shows nothing with no user-facing error message. The error is logged to the browser console only.
- **Fix:** Add `.catch(err => showError(...))` handlers to all page-load fetch chains with user-visible error messages.
- **Effort:** S

---

**F-22 · LOW · No loading state on page-load data fetches**
- **Location:** DSS, arrears, home dashboard pages
- **Symptom:** Tables and stat cards are empty for 200–500ms while data loads, then populate abruptly. No spinner or skeleton.
- **Fix:** Add a simple `.is-loading` CSS state with skeleton rows; remove on fetch completion.
- **Effort:** S

---

## 6. Code Quality & DX

**F-23 · HIGH · No automated tests**
- **Location:** `tests/` — contains only JSON fixture files; zero `.py` test files; no test runner configured
- **Symptom:** Every change to `app.py` must be manually verified. The prompt system (v20→v21, v15→v16, etc.) has changed significantly — there is no regression check that the correct prompt is loaded, that routes accept/reject the right inputs, or that DB operations produce correct results.
- **Fix:** Add at minimum:
  1. `pytest` + `pytest-flask` with a test client fixture
  2. Unit tests for: `encode_file()`, `variation_file_to_block()`, `get_db_conn()` error paths, MIME validation
  3. Route-level integration tests for: login (valid/invalid), `/analyze` (missing file → 400, wrong role → 403), `/api/cases` (returns correct fields), `/analyze-termination` (VMOC_UNAGREED without mods → 400)
  4. The existing JSON fixtures in `tests/terminations/` are test case specs — write a runner that calls the actual endpoint with mocked Claude responses
- **Effort:** L

---

**F-24 · MEDIUM · `app.py` is 6,488 lines — a single monolithic module**
- **Location:** `app.py`
- **Symptom:** All routes, prompts, DB logic, business logic, and utilities live in one file. This makes navigation and code review difficult, and it means any import error crashes the entire app.
- **Fix:** Split by domain into Flask Blueprints:
  - `blueprints/auth.py` — login, logout, password reset
  - `blueprints/completions.py` — /analyze + SYSTEM_PROMPT
  - `blueprints/terminations.py` — /analyze-termination + TERMINATION_SYSTEM_PROMPT
  - `blueprints/variations.py` — /analyze-variation, /analyze-ie
  - `blueprints/cases.py` — /api/cases/* CRUD
  - `blueprints/arrears.py` — /api/arrears/*
  - `blueprints/pp.py` — /api/pp/*
  - `blueprints/dss.py` — /api/dss/*
  - `db.py` — connection pooling, `get_db_conn`
  - `prompts/` — one file per system prompt
- **Effort:** L (refactor only — no logic change)

---

**F-25 · MEDIUM · Dependency version floors only — no lockfile**
- **Location:** `requirements.txt`
- **Symptom:** `anthropic>=0.40.0` will install whatever the latest version is at deploy time. A breaking change in the Anthropic SDK (e.g., API response shape change, method rename) silently breaks all analysis routes in production.
- **Fix:** Generate and commit `requirements.txt` with pinned versions (`pip freeze > requirements.txt`) and keep a separate `requirements.in` for human-readable floors. Use `pip-tools` to manage the pin/upgrade cycle.
- **Effort:** S

---

**F-26 · LOW · `print()` used for all logging**
- **Location:** Throughout `app.py` — ~15 `print()` calls for errors
- **Symptom:** No log levels (debug/info/warning/error), no structured format (JSON), no timestamps. In production on Heroku/Fly, stdout logs are captured but can't be filtered or alerted on.
- **Fix:**
  ```python
  import logging
  logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
  logger = logging.getLogger(__name__)
  # Replace print(f"Failed to save case: {e}") with:
  logger.exception("Failed to save case")  # includes traceback
  ```
- **Effort:** S

---

## 7. Operational

**F-27 · HIGH · No CI/CD pipeline**
- **Location:** Repo root — no `.github/workflows/`, no CI config
- **Symptom:** Every merge to the branch goes to production without any automated check — no lint, no type check, no test run, no deploy preview.
- **Fix:** Add a minimal GitHub Actions workflow:
  ```yaml
  on: [push, pull_request]
  jobs:
    test:
      runs-on: ubuntu-latest
      steps:
        - uses: actions/checkout@v4
        - uses: actions/setup-python@v5
          with: { python-version: "3.11" }
        - run: pip install -r requirements.txt pytest pytest-flask
        - run: pytest tests/
        - run: python -m py_compile app.py  # syntax check until proper tests exist
  ```
- **Effort:** S

---

**F-28 · MEDIUM · No structured error tracking**
- **Location:** `app.py` — no Sentry, Rollbar, or equivalent
- **Symptom:** Unhandled exceptions in streaming generators, DB errors, and Claude API failures produce no alert. The team learns about production errors only when users report them.
- **Fix:** Add Sentry (free tier covers the scale):
  ```python
  import sentry_sdk
  sentry_sdk.init(dsn=os.environ.get("SENTRY_DSN"), traces_sample_rate=0.1)
  ```
- **Effort:** S

---

## Hit List — Top 10 by (Impact × Confidence) / Effort

| Rank | ID | Title | Severity | Effort |
|------|----|-------|----------|--------|
| 1 | F-07 | Hardcoded fallback SECRET_KEY | CRITICAL | S |
| 2 | F-02 | Sessions never expire + no secure cookie flags | HIGH | S |
| 3 | F-08 | No brute-force protection on login | HIGH | S |
| 4 | F-17 | No timeout on Claude API stream | HIGH | S |
| 5 | F-09 | No security response headers | HIGH | S |
| 6 | F-01/F-13 | DB connection leak / no pool | HIGH | M |
| 7 | F-23 | Zero automated tests | HIGH | L |
| 8 | F-27 | No CI/CD pipeline | HIGH | S |
| 9 | F-04 | Hardcoded default admin password | MEDIUM | S |
| 10 | F-10 | File MIME type not validated from content | HIGH | S |

Items 1, 2, 3, 4, 5, 8, 9, 10 are all Small effort. They can be shipped in a single focused session.
