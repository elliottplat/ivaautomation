# OMNI Automation — Audit Report
*Generated 2026-05-11 | Audited by Claude Code*

---

## Architecture Diagram

```mermaid
graph TD
    Browser -->|HTTPS| Flask[Flask 3.1 / Gunicorn\n1 worker, 4 threads, 300s timeout]
    Flask -->|psycopg2\nnew conn per request| PG[(PostgreSQL\nDATABASE_URL)]
    Flask -->|anthropic SDK 0.98\nSSE stream| Claude[Anthropic API\nclaude-opus-4-7]
    Flask -->|resend SDK| Email[Resend Email\nomnigroup domain]
    Flask --> Static[/static/ logo.png]

    subgraph Auth
        LoginManager[flask-login\nUserMixin]
        Roles[roles: admin / reviewer\n uploader / team_leader]
        Specialisms[specialisms: comma-separated\ntask_type visibility]
    end

    Flask --> LoginManager
    LoginManager --> PG

    subgraph Modules
        Completions[/analyze POST\nCompletion calc]
        Terminations[/analyze-termination POST\nTermination calc]
        Variations[/analyze-variation POST\nVariation EOS/IE]
        IE[/analyze-ie POST\nIncome & Expenditure]
        Arrears[/api/arrears/*\nGeneral Arrears]
        PP[/api/pp/*\nParker Philips Arrears]
        DSS[/api/dss/*\nDelivery Support Service]
    end

    Flask --> Completions & Terminations & Variations & IE
    Flask --> Arrears & PP & DSS
    Completions & Terminations & Variations & IE --> Claude
```

---

## Entry Points

| Type | Path / Trigger | Auth |
|------|---------------|------|
| Page | `GET /` | login_required |
| Page | `GET /completions` | login_required + specialism |
| Page | `GET /terminations` | login_required + specialism |
| Page | `GET /variations` | login_required + specialism |
| Page | `GET /arrears` | login_required + specialism |
| Page | `GET /pp-arrears` | login_required |
| Page | `GET /dss/*` | login_required + role |
| Page | `GET /admin/*` | login_required + admin role |
| API | `POST /analyze` | login_required + uploader/admin |
| API | `POST /analyze-termination` | login_required + uploader/admin |
| API | `POST /analyze-variation` | login_required + uploader/admin |
| API | `POST /analyze-ie` | login_required |
| API | `POST /api/arrears/upload` | login_required + uploader/admin |
| API | `POST /api/pp/upload` | login_required + uploader/admin |
| API | `POST /api/dss/entry` | login_required + team_leader/admin |
| API | `GET /forgot-password` | Public |
| API | `GET /reset-password` | Public (token-gated) |
| Startup | `init_db()` | Server-side on boot |

---

## Data Model Summary

### Core
| Table | Key Columns | Indexes |
|-------|-------------|---------|
| `users` | id, username (UNIQUE), password_hash, role, specialisms, email, reset_token | username |
| `cases` | id, case_number, result (TEXT full output), task_type, review_status, variation_data (JSONB) | — |
| `notifications` | id, user_id→users, case_id→cases, read | — |
| `projects` | id, name, slug (UNIQUE) | slug |
| `user_projects` | user_id, project_id (composite PK) | — |
| `work_items` | id, project_id, task_type, case_number, status, assigned_to | — |

### Arrears
| Table | Key Columns | Indexes |
|-------|-------------|---------|
| `arrears_uploads` | id, project_id, upload_date, record_count | — |
| `arrears_cases` | id, upload_id, project_id, client_name, arrears_amount | — |
| `arrears_project_config` | project_id (UNIQUE), min_days, min_amount, require_both | — |

### Parker Philips
| Table | Key Columns | Indexes |
|-------|-------------|---------|
| `pp_snapshots` | id (UUID PK), snapshot_date, pipeline_result (JSONB), superseded | — |
| `pp_case_snapshots` | id UUID, snapshot_id, reference, many metrics | snapshot_id, reference, case_type, cycle |
| `pp_case_notes` | id UUID, reference, note_text, created_by | reference |

### DSS
| Table | Key Columns | Indexes |
|-------|-------------|---------|
| `dss_teams` | id, name, timezone | — |
| `dss_team_members` | id, team_id, name, is_active | — |
| `dss_task_types` | id, team_id, name, rate_per_hour, is_base | — |
| `dss_daily_shifts` | id, team_member_id, work_date, hours_worked (UNIQUE member+date) | team+date, member+date |
| `dss_daily_completions` | id, daily_shift_id, task_type_id, count | shift_id, type_id, subtype_id |
| `dss_daily_landings` | id, team_id, work_date, task_type_id (UNIQUE team+date+type) | — |
| `dss_daily_team_rollups` | id, team_id, work_date, running_backlog, sla_status (UNIQUE team+date) | team+date |

**Missing indexes:** `cases(case_number)`, `cases(submitted_by)`, `cases(review_status)`, `notifications(user_id, read)`, `work_items(project_id, status)`, `arrears_cases(upload_id)`.

---

## Dependency Inventory

| Package | Version spec | Notes |
|---------|-------------|-------|
| anthropic | >=0.40.0 | Pinned floor only — could silently upgrade to breaking version |
| flask | >=3.0.0 | Flask 3.1.3 installed |
| flask-login | >=0.6.0 | 0.6.x installed |
| python-dotenv | >=1.0.0 | OK |
| gunicorn | >=21.0.0 | OK |
| psycopg2-binary | >=2.9.0 | 2.9.12 installed |
| pandas | >=2.0.0 | Heavy dependency; only used for CSV arrears parsing |
| openpyxl | >=3.1.0 | Used for Excel support |
| xlrd | >=2.0.1 | Used for .xls support |
| resend | >=2.0.0 | Email service |

**No upper-bound pins on any dependency.** A major version bump in `anthropic` could silently break all API calls. No `pip freeze` lockfile (requirements.lock or similar) exists.

---

## Build / Test / Deploy Pipeline

| Stage | Status |
|-------|--------|
| CI (automated tests on PR) | **None** — no `.github/`, no CI config |
| Test suite | **None** — `tests/` contains only JSON fixture files, zero `.py` test files |
| Staging environment | **Unknown** — no evidence of staging vs production separation |
| Deployment | Heroku-style via `Procfile` (`gunicorn --worker-class gthread --workers 1 --threads 4 --timeout 300`) |
| Rollback | Manual only |
| Secrets rotation story | No documented process |
| Docker | None |

---

## Observability State

| Concern | Status |
|---------|--------|
| Application logging | `print()` to stdout only — no structured logger, no log levels |
| Error tracking | None (no Sentry, Rollbar, etc.) |
| Metrics | None |
| Distributed tracing | None |
| Alerting | None |
| API usage monitoring | Anthropic token counts written to DB per case — manual query only |
| Health check endpoint | None |

**All errors surface only as `print()` calls to stdout.** Production exceptions that don't reach the client are invisible unless logs are scraped.
