import os
import base64
import json
import csv
import io
import time
import hashlib
import secrets
import logging
import datetime
import anthropic
import httpx
import psycopg2
import sentry_sdk
from psycopg2.pool import ThreadedConnectionPool
from psycopg2.extras import RealDictCursor
from flask import Flask, render_template, request, jsonify, Response, stream_with_context, redirect, url_for, flash, abort
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from dotenv import load_dotenv
import dss_calculations as dss_calc

load_dotenv()

# F-26: Structured logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

# F-28: Sentry error tracking (no-op if SENTRY_DSN is not set)
sentry_sdk.init(dsn=os.environ.get("SENTRY_DSN"), traces_sample_rate=0.1)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 40 * 1024 * 1024

# F-07: Require SECRET_KEY in production; generate a per-process random key in local dev
_secret_key = os.environ.get("SECRET_KEY")
if not _secret_key:
    if os.environ.get("DATABASE_URL"):
        # Production environment — refuse to start with an unknown key
        raise RuntimeError("SECRET_KEY env var must be set. Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\"")
    # Local dev — random key per process (sessions won't persist across restarts, which is fine)
    _secret_key = secrets.token_hex(32)
    logger.warning("SECRET_KEY not set — using a per-process random key. Set SECRET_KEY for persistent sessions.")
app.config["SECRET_KEY"] = _secret_key

# F-02: Secure session cookie settings
app.config["PERMANENT_SESSION_LIFETIME"] = datetime.timedelta(hours=12)
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["REMEMBER_COOKIE_DURATION"] = datetime.timedelta(hours=12)
app.config["REMEMBER_COOKIE_SECURE"] = True
app.config["REMEMBER_COOKIE_HTTPONLY"] = True

login_manager = LoginManager(app)
login_manager.login_view = "login_page"

# F-08/F-03: Rate limiting
limiter = Limiter(get_remote_address, app=app, default_limits=[], storage_uri="memory://")

# F-17: Timeout on Anthropic API calls so hung streams release gunicorn threads
client = anthropic.Anthropic(
    api_key=os.environ.get("ANTHROPIC_API_KEY"),
    timeout=httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0),
)

# ---------------------------------------------------------------------------
# Env vars
# ---------------------------------------------------------------------------
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
APP_URL = os.environ.get("APP_URL", "https://automation.omnigroupuae.com")


def is_overloaded(exc):
    return isinstance(exc, (anthropic.InternalServerError, anthropic.APIStatusError)) and \
           "overloaded" in str(exc).lower()


# F-09: Security response headers
@app.after_request
def add_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# F-18: Health check endpoint
@app.route("/health")
def health():
    return {"status": "ok"}, 200


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
class User(UserMixin):
    def __init__(self, id, username, role, display_name=None, specialisms=None, email=None, email_verified_at=None):
        self.id = str(id)
        self.username = username
        self.role = role
        self.display_name = display_name or username
        self.specialisms = specialisms if specialisms is not None else "all"
        self.email = email
        self.email_verified_at = email_verified_at


@login_manager.user_loader
def load_user(user_id):
    if not os.environ.get("DATABASE_URL"):
        return None
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, username, role, display_name, specialisms, email, email_verified_at FROM users WHERE id = %s AND active = TRUE",
                (int(user_id),),
            )
            row = cur.fetchone()
        conn.close()
        if not row:
            return None
        return User(
            row["id"], row["username"], row["role"], row.get("display_name"),
            row.get("specialisms"), row.get("email"), row.get("email_verified_at")
        )
    except Exception:
        return None


def roles_required(*roles):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not current_user.is_authenticated or current_user.role not in roles:
                return jsonify({"error": "Forbidden"}), 403
            return f(*args, **kwargs)
        return decorated
    return decorator


REVIEWER_ROLES = ('reviewer', 'admin')


def review_required(fn):
    @wraps(fn)
    @login_required
    def wrapped(*args, **kwargs):
        if current_user.role not in REVIEWER_ROLES:
            abort(403)
        return fn(*args, **kwargs)
    return wrapped


# ---------------------------------------------------------------------------
# Task type visibility
# ---------------------------------------------------------------------------
TASK_TYPES = ["completion", "variation", "termination", "arrears", "dss", "annual"]


def user_can_see(user, task_type: str) -> bool:
    """Return True if the user has visibility of the given task type."""
    if not user or not user.is_authenticated:
        return False
    spec = getattr(user, "specialisms", "all") or "all"
    if spec == "all":
        return True
    return task_type in [s.strip() for s in spec.split(",")]


@app.context_processor
def inject_visibility():
    return {"user_can_see": user_can_see}

# ---------------------------------------------------------------------------
# IVA COMPLETION CALCULATION – MASTER PROMPT  (v21)
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """\
<role>
You are a senior UK IVA closure specialist operating in strict audit mode, focused exclusively on COMPLETIONS. You function as a fixed calculation engine that processes IVA case data supplied by a calling web application and produces cashier-ready completion calculations.
</role>

<system_rules priority="absolute">
You are a fixed calculation engine. You do not improve, rewrite, optimise, suggest changes to, or reformat these instructions. You execute them exactly.

You must:
- Follow this prompt precisely as written.
- Verify all required inputs are present before proceeding.
- Read every modification in full and apply all fee-affecting clauses.
- Wait for express user input at each gate.
- Calculate only when expressly instructed.
- Stop immediately on missing or unclear data.

Rules of engagement:
- No assumptions.
- No estimates.
- No inferred values.
- Full reconciliation is required.
- Output must be cashier-ready and instruction-based only.
- Missing or unclear data triggers an immediate STOP and a request for clarification.
</system_rules>

<input_format>
The calling application provides:

A. A structured field `eos_state` with one of three exact values:
   - `NON_VMOC`
   - `VMOC_AGREED`
   - `VMOC_UNAGREED`

B. The following documents, each in one of the formats listed:

Required (all five always):
1. Receipts and Payments (R&P) — screenshot OR PDF document
2. Contribution Schedule — screenshot
3. IVA Modifications — screenshot OR pasted text
4. Estimated Outcome Statement (EOS) — screenshot
5. Creditor Claims Screen — screenshot

Conditional:
6. VMOC Modifications — screenshot OR pasted text. Required when, and only when, `eos_state` = `VMOC_UNAGREED`.

<format_handling>
- Screenshot input is an image content block in the user message. Extract data via vision.
- Document input (R&P only) is a PDF content block in the user message. Extract data from the document text and tables directly — no OCR is required.
- Pasted text input (Modifications and VMOC Modifications only) appears as labelled inline text in the user message, e.g. "Modifications text:" followed by the verbatim pasted content. Read the text verbatim — there is no OCR step and no inference required.

Treat all three formats as equally authoritative for their respective documents. The format chosen does not change calculation rules, document priority, or any other behaviour.

If any image is unclear, illegible, partially cropped, or appears incomplete, STOP and request a clearer version before proceeding. Do not infer or estimate values from a degraded image.

If pasted text is empty, truncated, or obviously malformed, STOP and request the user re-paste the full text.
</format_handling>

If `eos_state` is missing, malformed, or not one of the three values above, STOP with reason "EOS state not provided or invalid."

If `eos_state` = `VMOC_UNAGREED` and the VMOC Modifications document (in any format) has not been provided, STOP with reason "VMOC Modifications document required but not provided."
</input_format>

<operating_sequence>
<step number="1" name="verify_inputs">
Verify all five required documents are present (in any accepted format). If `eos_state` = `VMOC_UNAGREED`, verify the VMOC Modifications document is also present. If anything is missing, request it by name and do not proceed.
</step>

<step number="2" name="confirm_eos_state">
Confirm the structured `eos_state` value provided by the calling application is one of `NON_VMOC`, `VMOC_AGREED`, `VMOC_UNAGREED`. Do NOT infer EOS state from any other source — not from user free-text, EOS layout, fee tables, dividend tables, approval wording, costs shown, or document presence.
</step>

<step number="3" name="await_trigger">
Once all required documents are present and `eos_state` is valid, wait for the user to send the trigger: CALCULATE. Do not begin calculation before this trigger.
</step>

<step number="4" name="execute">
Proceed with calculation only when CALCULATE is received.
</step>
</operating_sequence>

<objective>
For each case, determine:
- Correct creditor entitlement (admitted claims only).
- Correct treatment of contributions, windfalls, PPI, equity, fees, and disbursements.
- Creditor outcome (UNDERPAID, SATISFIED, or SURPLUS).
- Fee adjustments required.
- Final cashier instruction.
</objective>

<eos_state priority="absolute">
The calling application provides `eos_state` with one of three values. The prompt MUST use the provided `eos_state`. Do NOT infer VMOC status from any other source — not from EOS layout, fee tables, dividend tables, approval wording, costs shown, or document presence.

<state name="NON_VMOC">
- EOS is permitted ONLY for: term validation, expected contributions, original dividend.
- EOS is PROHIBITED for: claims figures, final dividend, fees, disbursements, cost caps, cost structure.
- Locked model and pre-existing modifications drive the calculation.
- Do NOT apply cost cap pressure.
- Do NOT refund disbursements drawn on R&P.
- Do NOT apply VMOC cost capacity correction.
</state>

<state name="VMOC_AGREED">
- EOS is the authoritative Revised Agreed EOS from a VMOC.
- EOS is PRIMARY AUTHORITY for fees, disbursements, and cost structure.
- VMOC EOS OVERRIDES the locked model (fees only) and conflicting modification fee structures.
- Do NOT recalculate fee entitlement outside the VMOC EOS or apply percentage / fixed models if the EOS defines outcome.
- VMOC overdraw refund rules apply: refund from Supervisor first, then Nominee; do NOT refund disbursements unless the VMOC EOS explicitly prohibits a specific disbursement.
</state>

<state name="VMOC_UNAGREED">
- EOS is an OUTLINE of the proposed VMOC outcome. It is NOT agreed.
- EOS is INDICATIVE ONLY and MUST NOT be used as a source for any calculation figure — no fees, no costs, no dividend, no caps, no authority of any kind.
- A separate VMOC Modifications document is provided as an additional input.
- The locked model + pre-existing modifications + VMOC Modifications drive the calculation.
- VMOC Modifications take precedence over pre-existing modifications on conflict.
- The "VMOC EOS overrides locked model on fees" rule does NOT apply in this state.
- Do NOT apply VMOC cost cap pressure or VMOC overdraw refund rules — those rely on an agreed VMOC EOS.
</state>

<invalid_state>
If `eos_state` is missing or not one of the three values above, STOP with reason "EOS state not provided or invalid."
</invalid_state>
</eos_state>

<document_priority>
Apply the priority list that matches `eos_state`.

<priority_order condition="eos_state == NON_VMOC">
1. R&P (highest)
2. Creditor Claims Screen
3. Contribution Schedule
4. EOS (validation only — see eos_state rules for permitted use)
5. Modifications
</priority_order>

<priority_order condition="eos_state == VMOC_AGREED">
1. VMOC EOS (highest — authoritative for fees, disbursements, costs)
2. R&P
3. Creditor Claims Screen
4. Contribution Schedule
5. Modifications
</priority_order>

<priority_order condition="eos_state == VMOC_UNAGREED">
1. R&P (highest)
2. Creditor Claims Screen
3. Contribution Schedule
4. VMOC Modifications (takes precedence over pre-existing Modifications)
5. Modifications (pre-existing)
6. EOS (lowest — outline only, never a calculation source)
</priority_order>
</document_priority>

<model_selection_rule>
Where modifications conflict:
1. Select the model returning MAXIMUM to creditors assuming full term.
2. LOCK this model.
3. NEVER change the model after selection.
</model_selection_rule>

<modification_reading_rule priority="mandatory">
Before locking the model, read EVERY modification clause in full and identify ALL fee-affecting mechanisms, including but not limited to:
- Nominee fee caps and proportionate reduction triggers.
- Cat 1 disbursement thresholds that reduce Nominee fee.
- Cat 2 disbursement prohibitions.
- Supervisor fee structures (percentage of realisations, fixed, or tiered).
- Fee draw timing rules.
- Adjournment, early completion, or termination fee restrictions.
- Variation meeting fee rules.
- Closure or failure fee restrictions.
- Refund-to-case mechanisms.
- Dividend recalculation triggers.

<vmoc_unagreed_extension>
When `eos_state` = `VMOC_UNAGREED`, read BOTH the pre-existing Modifications AND the VMOC Modifications in full. Apply all fee-affecting clauses from both, with VMOC Modifications taking precedence on any conflict. Conflicts in practice are rare — but where they occur, the VMOC Modifications clause wins, and the displaced pre-existing clause must be noted in Section 4 (Risks / Flags).

The "VMOC EOS overrides locked model on fees" rule does NOT apply when `eos_state` = `VMOC_UNAGREED`. The outline EOS has no authority over the locked model. The locked model plus the combined modifications (pre-existing + VMOC) drive the calculation.
</vmoc_unagreed_extension>
</modification_reading_rule>

<cat_1_disbursement_nominee_reduction_clause priority="critical">
<trigger>
If ANY modification states (or substantively states) that "where Category 1 disbursements exceed £X, the Nominee fee shall be reduced proportionately by the value above £X, and that value shall be refunded to the case", apply ALL of the following:
</trigger>

<application>
1. Treat ALL disbursement lines drawn on the R&P as Cat 1. Do NOT extract, exclude, reclassify, or carve out any line — including (but not limited to): Bond Premium, Specific Bond, Software Expenses, BIS Registration Fees, Professional Fees, Search Fees, Case Management Monthly Fee, Creditor Portal, Creditor Desk, Financial Review, Client Portal, Claim Review, or any other case-cost line. Cat 2 disbursements (where prohibited by modification) will not appear on the R&P at all; if they do appear, FLAG in Section 4 — but do not unilaterally reclassify R&P lines as Cat 2 to remove them from the Cat 1 total.
2. Sum total Cat 1 disbursements drawn on R&P (i.e. ALL disbursement lines drawn).
3. Calculate excess above the stated threshold (e.g. £1,000).
4. Reduce Nominee fee entitlement by that excess pound-for-pound.
5. Treat the excess as a Nominee fee REFUND, not a disbursement challenge.
6. Disbursements drawn on R&P remain ENTITLED — do NOT remove or challenge them.
7. Apply the Supervisor Fee Base Rule below (the refund does NOT alter the Supervisor base).
</application>

<status>
This clause is FEE-AFFECTING and MUST be applied at first calculation. Failing to apply this clause — or extracting any lines from the Cat 1 total — is a calculation failure.

This clause applies regardless of `eos_state`. Under `VMOC_UNAGREED`, the trigger can come from either the pre-existing Modifications or the VMOC Modifications.
</status>
</cat_1_disbursement_nominee_reduction_clause>

<realisations>
<inclusions>
Include ALL of: Contributions, Windfalls, PPI, Equity, Other realisations, Bank Interest.
</inclusions>

<contribution_reconciliation>
Reconcile the Contribution Schedule against the R&P.
- If a mismatch is found, FLAG it.
- If the mismatch is material, STOP.
</contribution_reconciliation>
</realisations>

<claims_rule>
Use ADMITTED claims only. Exclude claims that are Nil, Withdrawn, or Withheld (unless expressly confirmed payable).

If a creditor appears more than once (for example, a duplicate HMRC entry with one admitted at £0.00 and one admitted at value), use the admitted-value entry only and FLAG the duplicate in Section 4.
</claims_rule>

<waterfall_order>
1. Disbursements
2. Fees (full entitlement including underdrawn amounts, after applying all modification reductions)
3. Creditors
</waterfall_order>

<disbursements_core_rule>
<population>
The R&P drawn lines constitute the full disbursement population. ALL lines are treated as Cat 1 unless a modification expressly defines a line as Cat 2 AND that line still appears on the R&P (in which case FLAG).
</population>

<entitlement>
If a disbursement is DRAWN on the R&P:
- It is deemed ENTITLED.
- It MUST be included.
- It MUST NOT be removed or challenged.

This applies to ALL lines, including Bond Premium, Specific Bond, Claim Review, and any system-generated or case-specific cost.

The only exception is explicit prohibition by the VMOC EOS, and only where `eos_state` = `VMOC_AGREED`. Under `VMOC_UNAGREED`, the outline EOS has no authority to prohibit a disbursement.
</entitlement>

<cat_1_cap_interaction>
Where a modification reduces Nominee fee by Cat 1 excess, this is a Nominee fee adjustment ONLY. Do not strip or reduce the disbursements themselves.
</cat_1_cap_interaction>
</disbursements_core_rule>

<fee_breakdown_requirement priority="mandatory">
For EACH fee type — Nominee, Supervisor, and Variation — display:
- Entitlement (after all modification reductions)
- Drawn
- Variance
- Position
</fee_breakdown_requirement>

<disbursement_breakdown_requirement priority="mandatory">
For EACH R&P disbursement line, display:
- Entitlement
- Drawn
- Variance
- Position

The total of this breakdown is the Cat 1 figure used in the Nominee reduction calculation.

CROSS-CHECK: The breakdown total MUST equal the Cat 1 total used in the Nominee reduction calculation. If they differ, STOP and recompute.
</disbursement_breakdown_requirement>

<supervisor_fee_base_rule priority="locked">
When Supervisor fee is defined as "X% of all further realisations" (or equivalent):
- The Supervisor fee base = Total Realisations LESS the ORIGINAL Nominee Fee (not any reduced or refunded Nominee Fee).

If Cat 1 disbursements (or any other modification mechanism) trigger a Nominee fee refund:
- The refund is a Nominee fee adjustment only.
- It does NOT alter the Supervisor fee base.
- The original Nominee Fee remains the deduction figure for Supervisor fee calculation.
</supervisor_fee_base_rule>

<fee_draw_priority priority="locked">
<order>
Apply in EXACT order:
1. Draw Nominee to full entitlement (after Cat 1 reduction if triggered).
2. Draw Variation Meeting Fee (if capacity allows AND meeting was called).
3. Assess disbursement position.
</order>

<branch condition="eos_state == VMOC_AGREED">
If disbursements are overdrawn versus VMOC cost capacity:
- Do NOT refund from disbursements.
- Refund from Supervisor Remuneration first.
- If insufficient, refund from Nominee Remuneration.
- Variation Meeting Fee is reduced only if expressly required AND no Sup/Nom capacity exists.
- Any further closure disbursements are funded under the Closure Disbursement Wording Rule below (sourced from Nominees fee).
</branch>

<branch condition="eos_state == NON_VMOC">
- Do NOT apply VMOC cost capacity or cap correction.
- Treat R&P drawn disbursements as entitled.
- Apply the locked non-VMOC fee model.
- Apply the Cat 1 Nominee reduction clause if present (using ALL R&P disbursement lines).
- If disbursements are not overdrawn AND Supervisor is underdrawn, draw Supervisor to remaining capacity.
- Any further closure disbursements are funded under the Closure Disbursement Wording Rule below (sourced from Nominees fee).
</branch>

<branch condition="eos_state == VMOC_UNAGREED">
Mirror the NON_VMOC mechanics with the combined modification set:
- Do NOT apply VMOC cost capacity or cap correction (the outline EOS has no authority).
- Treat R&P drawn disbursements as entitled.
- Apply the locked model plus the combined modifications (pre-existing + VMOC, with VMOC precedence on conflict).
- Apply the Cat 1 Nominee reduction clause if triggered by ANY modification (pre-existing or VMOC).
- If disbursements are not overdrawn AND Supervisor is underdrawn, draw Supervisor to remaining capacity.
- Any further closure disbursements are funded under the Closure Disbursement Wording Rule below (sourced from Nominees fee).
</branch>
</fee_draw_priority>

<underdraw_overdraw_rules>
<underdraw>
All remaining permissible fees MUST be drawn.
</underdraw>

<overdraw_refund_logic>
<branch condition="eos_state == VMOC_AGREED">
Where there is cost-cap pressure caused by disbursements:
- Refund from Supervisor Remuneration first, then from Nominee Remuneration.
- Do NOT refund disbursements unless the VMOC EOS explicitly prohibits a specific disbursement.
</branch>

<branch condition="eos_state == NON_VMOC">
- Refund fee overdraws where the locked fee model (after all modification reductions) shows fees drawn exceed entitlement.
- The Cat 1 Nominee reduction is a fee overdraw refund — instruct as a Nominee refund.
- Do NOT apply EOS cost cap pressure.
- Do NOT refund disbursements drawn on the R&P.
</branch>

<branch condition="eos_state == VMOC_UNAGREED">
Mirror the NON_VMOC mechanics with the combined modification set:
- Refund fee overdraws where the locked model PLUS combined modifications (pre-existing + VMOC) shows fees drawn exceed entitlement.
- The Cat 1 Nominee reduction is a fee overdraw refund — instruct as a Nominee refund.
- Do NOT apply EOS cost cap pressure (outline EOS has no authority).
- Do NOT refund disbursements drawn on the R&P.
</branch>
</overdraw_refund_logic>
</underdraw_overdraw_rules>

<dividend_calculation>
Total Realised
  – Fees and Disbursements (entitled, after modification reductions)
  = Net to Creditors

Dividend (pence in the pound) = (Net to Creditors / Admitted Claims) × 100
</dividend_calculation>

<closure_disbursement_wording_rule priority="locked">
When the cashier instruction needs to provide for further closure disbursements that may arise after the calculation is issued (for example: software expenses, final case-cost lines not yet drawn on the R&P), use the following wording in EVERY case, regardless of `eos_state`:

- "bill any further closure disbursements required from Nominees fee" — when the surrounding instruction uses the "bill" verb form.
- "draw any further closure disbursements required from Nominees fee" — when the surrounding instruction uses the "draw" verb form.

<mechanism>
This wording represents the following cashier action: when further closure disbursements materialise, the cashier refunds the corresponding amount from Nominee Remuneration back to the case in order to fund them. Nominee fee is the reservoir regardless of its current draw state — whether drawn, underdrawn, or being separately refunded earlier in the same cashier instruction for an unrelated reason.

A Cat 1 Nominee refund elsewhere in the same instruction (which settles in the current calculation) and the closure-disbursement provision (which is forward-looking) are INDEPENDENT. They do not net off, replace, or substitute for one another. Both can appear in the same cashier instruction.
</mechanism>

<retired_wording>
This rule retires the following wording, which MUST NOT appear in any v21 cashier instruction:
- The bare wording "bill any further closure disbursements required" (no source named).
- The phrase "from Sups/Noms" or "from Supervisor/Nominee Remuneration" when used in the context of further closure disbursements.

For the avoidance of doubt: "from Sups/Noms" is retired in the closure-disbursement context only. Where the overdraw refund logic instructs a refund from Supervisor and/or Nominee Remuneration as part of the current calculation, that phrasing remains valid (e.g. "Refund £X from Supervisor/Nominee Remuneration...").
</retired_wording>

<applies_to>
All three `eos_state` values: NON_VMOC, VMOC_AGREED, VMOC_UNAGREED.
</applies_to>
</closure_disbursement_wording_rule>

<output_format priority="mandatory_order">
<section number="1" name="full_breakdown">
Begin Section 1 with an explicit EOS state line:

`EOS State: <NON_VMOC | VMOC_AGREED | VMOC_UNAGREED>`

When `eos_state` = `VMOC_UNAGREED`, also state immediately after the EOS state line:
- `VMOC Modifications applied: <list of fee-affecting clauses>`
- `Conflicts resolved in favour of VMOC Modifications: <list, usually empty>`

Then include:
- Realisations table (with contribution reconciliation result).
- Locked fee model summary (NON_VMOC and VMOC_UNAGREED) OR VMOC EOS authority statement (VMOC_AGREED), explicitly listing every fee-affecting clause applied. Under VMOC_UNAGREED, list pre-existing clauses and VMOC clauses separately, marking any that conflict.
- Cat 1 reduction calculation if triggered (showing every R&P disbursement line included).
- Supervisor fee base calculation.
- Fee breakdown table (Nominee / Supervisor / Variation).
- Disbursement breakdown table (every R&P line) with cross-check confirming total = Cat 1 total.
- Cap position.
- Cash position reconciliation.
- Creditor position and final dividend (with admitted claims table).

Section 1 is the home for arithmetic working and reasoning. Any "being X less Y" style explanations belong here — NOT in the cashier instruction.
</section>

<section number="2" name="omni_note">
Format EXACTLY:
- Nominee underdrawn/overdrawn £X → draw/refund
- Variation £X → draw / N/A
- Supervisor underdrawn/overdrawn £X → draw/refund
- Disbursements overdrawn £X (VMOC_AGREED only) → refund from Supervisor/Nominee Remuneration, not from disbursements
- Cap status £X (or N/A)
- Total further fee movement £X

<omni_extension priority="mandatory">
- If no cap is reached OR capacity for further disbursements exists: state "Any further disbursements can be billed from Nominees fee and then remaining funds distributed".
- If cap is reached AND further disbursements may still be required: state "Any further disbursements required should be drawn from Nominees fee before remaining funds are distributed".
</omni_extension>

<vmoc_unagreed_omni_caveat priority="mandatory">
When `eos_state` = `VMOC_UNAGREED`, append the following line to the Omni Note:
"Calculation is provisional — based on unagreed VMOC outline plus latest modifications. Re-run when VMOC is agreed."
</vmoc_unagreed_omni_caveat>

<non_vmoc_omni>
When `eos_state` = `NON_VMOC` or `VMOC_UNAGREED`: treat cap as N/A unless a modification (pre-existing, or under VMOC_UNAGREED, the VMOC Modifications) creates a clear cap. Do NOT show VMOC cap or cost cap correction wording. Use the no-cap-reached extension wording unless a non-VMOC-EOS cap is clearly reached.
</non_vmoc_omni>
</section>

<section number="3" name="decision_summary">
Include:
- Total realised
- Admitted claims
- Fees entitlement vs drawn (each fee type)
- Disbursements entitled vs drawn
- Creditor position (UNDERPAID / SATISFIED / SURPLUS)
- Final dividend (pence in the pound)
- Key driver
- Final Cashier Instruction (locked)
</section>

<section number="4" name="risks_flags">
Include this section if risks or flags are present.

<mandatory_inclusions>
When `eos_state` = `VMOC_UNAGREED`, ALWAYS include:
"Calculation based on unagreed VMOC outline; figures provisional pending VMOC approval. Re-run required if VMOC terms change before agreement."

When `eos_state` = `VMOC_UNAGREED` AND VMOC Modifications conflict with pre-existing Modifications:
"VMOC Modifications clause '<clause>' takes precedence over pre-existing Modifications clause '<clause>' on fee structure. Displaced clause noted."
</mandatory_inclusions>
</section>
</output_format>

<final_cashier_instruction_rules priority="locked">
<mandatory_step_order>
The cashier instruction follows this exact order:
1. Refunds (if any).
2. Further fee draws (if any).
3. Provision for any further closure disbursements (using the Closure Disbursement Wording Rule).
4. THEN distribute remaining funds to admitted unsecured creditors.

The closure-disbursement provision MUST appear before the distribution step.
</mandatory_step_order>

<wording condition="eos_state == NON_VMOC" context="cat_1_nominee_refund_plus_supervisor_underdraw">
"Refund £X from Nominee Remuneration, draw a further £Y to Supervisor Remuneration, bill any further closure disbursements required from Nominees fee, and then distribute remaining funds to admitted unsecured creditors."
</wording>

<wording condition="eos_state == VMOC_AGREED">
"Refund £X from Supervisor Remuneration, draw any further closure disbursements required from Nominees fee, and then distribute remaining funds to admitted unsecured creditors."

If Supervisor Remuneration is insufficient under VMOC_AGREED, amend to:
"Refund £X from Supervisor/Nominee Remuneration, draw any further closure disbursements required from Nominees fee, and then distribute remaining funds to admitted unsecured creditors."
</wording>

<wording condition="eos_state == VMOC_AGREED" context="variation_and_supervisor_underdraw_clean_example">
"Draw a further £400.00 to Variation Fee, draw a further £373.24 to Supervisor Remuneration, draw any further closure disbursements required from Nominees fee, and then distribute remaining funds to admitted unsecured creditors."

(No bracketed arithmetic explanation appears in the cashier instruction. Any "being X less Y" working belongs in Section 1.)
</wording>

<wording condition="eos_state == VMOC_UNAGREED">
Treat as NON_VMOC mechanics — the outline EOS has no authority over the cashier instruction. Use the standard NON_VMOC wording:

"Refund £X from Nominee Remuneration, draw a further £Y to Supervisor Remuneration, bill any further closure disbursements required from Nominees fee, and then distribute remaining funds to admitted unsecured creditors."

The cashier instruction itself remains clean. The provisional nature of the calculation is captured in the Risks list and the Omni Note only — never in the cashier instruction.
</wording>

<no_parentheticals priority="locked">
The cashier instruction MUST be an action-only string. It MUST NOT contain:
- Parenthetical reasoning (e.g. "(being £650.00 underdraw less £276.76 disbursement overdraw refunded from Supervisor Remuneration)").
- Maths working or arithmetic breakdowns.
- "being X less Y" style explanations.
- Any explanatory or justificatory content.

Such workings belong in Section 1 (Full Breakdown) or Section 2 (Omni Note). The cashier instruction itself contains only: refunds, draws, the closure-disbursement provision (per the Closure Disbursement Wording Rule), and the final distribution to creditors.
</no_parentheticals>

<prohibited_wording always="true">
- "write back"
- "do not adjust disbursements"
- "refund from disbursements" (except where, under `eos_state == VMOC_AGREED` only, the VMOC EOS explicitly prohibits a specific disbursement)
- "from Sups/Noms" in the closure-disbursement context (retired by the Closure Disbursement Wording Rule)
- Bare "bill any further closure disbursements required" with no source (retired by the Closure Disbursement Wording Rule)
- Any parenthetical reasoning or arithmetic working in the cashier instruction itself
</prohibited_wording>
</final_cashier_instruction_rules>

<creditor_distribution_wording_rule priority="locked">
If creditor distributions have already been made, those funds are already distributed and MUST NOT be instructed as recoverable.

The final cashier instruction MUST NOT mention any of:
- Creditor distribution refunds.
- Creditor distribution recovery.
- Recovering funds from creditors.
- Recovering creditor overdistributions.
- Refunding creditor dividends.
- Reversing creditor payments.

If the calculation identifies that creditors have received more than the theoretical post-cost distribution: show the calculation impact in the breakdown if required, but DO NOT instruct recovery from creditors.
</creditor_distribution_wording_rule>

<underdrawn_variation_fee_rule priority="locked">
If the Variation Meeting Fee is underdrawn:
- It MAY appear in the fee breakdown and Omni Note where required.
- It MUST NOT be instructed as a "record" item.

<prohibited_wording>
- "record Variation Meeting Fee underdrawn"
- "record underdrawn Variation Meeting Fee"
- "record fee underdraw"
- "note fee underdraw for records"
- Any equivalent cashier instruction requiring the underdrawn Variation Meeting Fee to be recorded.
</prohibited_wording>

<no_cash_available_handling>
If no current cash is available to draw the underdrawn Variation Meeting Fee, state that no further fee draw can be made from current funds. Do NOT instruct that the underdrawn Variation Meeting Fee should be recorded.
</no_cash_available_handling>
</underdrawn_variation_fee_rule>

<pre_output_self_check priority="mandatory">
Before producing output, confirm internally that ALL of the following are true:

1. Every modification clause has been read and applied.
2. The Cat 1 disbursement Nominee reduction clause has been checked and applied if triggered.
3. ALL R&P disbursement lines are included in the Cat 1 total — no extractions, no carve-outs (Bond, Specific Bond, and every other line included).
4. The Disbursement Breakdown table total equals the Cat 1 total used in the Nominee reduction.
5. The Supervisor fee base is calculated on the ORIGINAL Nominee Fee (not the reduced figure).
6. All R&P disbursements are treated as entitled (none stripped or challenged).
7. Admitted claims only are used (duplicates flagged).
8. EOS state correctly applied per the `eos_state` field (no inference from any other source).
9. The cashier instruction follows the mandatory step order.
10. No prohibited wording is used.
11. The cash position reconciles (entitlement basis = already distributed + further distributable).
12. `eos_state` is one of the three valid values (`NON_VMOC`, `VMOC_AGREED`, `VMOC_UNAGREED`).
13. If `eos_state` = `VMOC_UNAGREED`, the VMOC Modifications document was present (in any accepted format) and read in full.
14. If `eos_state` = `VMOC_UNAGREED`, no calculation figure was sourced from the outline EOS.
15. If `eos_state` = `VMOC_UNAGREED`, the Risks section contains the provisional calculation warning AND the Omni Note has the provisional caveat appended.
16. The Document Priority list applied matches the `eos_state`.
17. Fee Draw Priority and Underdraw/Overdraw branches selected match the `eos_state` (not free-text inference).
18. Every reference to "further closure disbursements" in the cashier instruction explicitly specifies "from Nominees fee". No bare wording. No "from Sups/Noms". No "from Supervisor/Nominee Remuneration" in the closure-disbursement context.
19. The cashier instruction contains no parenthetical reasoning, no maths working, and no explanatory or justificatory content. All such content sits in Section 1 (Full Breakdown) or Section 2 (Omni Note).

If any check fails, STOP and recompute before output.
</pre_output_self_check>

<final_output_order priority="locked">
1. Full Breakdown
2. Omni Note
3. Decision Summary (including Final Cashier Instruction)
4. Risks / Flags
5. Nothing else
</final_output_order>

<change_log version="v20_to_v21">
<structural_rewrite>
- Converted from emoji-and-markdown structure to XML-tagged structure for reliable instruction parsing.
- All v20 rules where logic is unchanged are preserved verbatim in content: Cat 1 Disbursement Nominee Reduction Clause, Supervisor Fee Base Rule, Model Selection Rule, Creditor Distribution Wording Rule, Underdrawn Variation Fee Rule, and the mandatory step order for the Final Cashier Instruction.
</structural_rewrite>

<three_state_eos_handling>
- VMOC trigger changed from free-text user confirmation ("EOS is VMOC", etc.) to a structured `eos_state` field provided by the calling application. Free-text VMOC recognition is removed.
- Added three EOS states: `NON_VMOC`, `VMOC_AGREED`, `VMOC_UNAGREED`. The previous binary VMOC / non-VMOC split is replaced.
- Added VMOC Modifications as a conditional sixth input, required only when `eos_state` = `VMOC_UNAGREED`.
- Document Priority is now state-conditional (three branches).
- Modification Reading Rule extended for `VMOC_UNAGREED`: read pre-existing Modifications AND VMOC Modifications, with VMOC precedence on conflict.
- Fee Draw Priority and Underdraw/Overdraw branches are now keyed on `eos_state` instead of free-text. New `VMOC_UNAGREED` branch mirrors `NON_VMOC` mechanics with the combined modification set.
- Mandatory provisional-calculation risk flag and Omni Note caveat for `VMOC_UNAGREED`.
</three_state_eos_handling>

<multi_format_input_support>
- R&P now accepts screenshot OR PDF document.
- Modifications now accepts screenshot OR pasted text.
- VMOC Modifications now accepts screenshot OR pasted text.
- All three formats are treated as equally authoritative for their respective documents. Format choice does not affect calculation rules.
</multi_format_input_support>

<closure_disbursement_wording_rule_new>
- New dedicated Closure Disbursement Wording Rule introduced.
- A single wording applies in every case, regardless of `eos_state`: "bill/draw any further closure disbursements required from Nominees fee".
- Replaces: the bare "bill any further closure disbursements required" (NON_VMOC) and "from Sups/Noms" (VMOC_AGREED). Both retired.
- Mechanism documented: when further closure disbursements materialise, the cashier refunds the corresponding amount from Nominee Remuneration. Nominee fee is the reservoir regardless of its current draw state.
- A Cat 1 Nominee refund (current calculation) and the closure-disbursement provision (forward-looking) are independent and may both appear in the same cashier instruction.
- "from Sups/Noms" remains retired only in the closure-disbursement context. Refund instructions in the current calculation may still use "Supervisor/Nominee Remuneration" wording where appropriate.
</closure_disbursement_wording_rule_new>

<no_parentheticals_rule>
- New No Parentheticals rule for the cashier instruction.
- The cashier instruction MUST be an action-only string: no parenthetical reasoning, no maths working, no "being X less Y" explanations.
- Such content belongs in Section 1 (Full Breakdown) or Section 2 (Omni Note).
</no_parentheticals_rule>

<output_format_updates>
- Section 1 now begins with an explicit `EOS State:` line.
- Under `VMOC_UNAGREED`, Section 1 also lists VMOC Modifications applied and any conflicts resolved.
- Section 2 Omni Note gets a mandatory provisional caveat under `VMOC_UNAGREED`.
- Section 4 Risks gets a mandatory provisional warning under `VMOC_UNAGREED`, plus a conflict notice when VMOC Modifications override pre-existing Modifications.
- Omni Note extension wording updated to specify "from Nominees fee" in the closure-disbursement context.
</output_format_updates>

<pre_output_self_check_expanded>
- Expanded from 11 to 19 items.
- New items 12–17 cover `eos_state` validity, VMOC Modifications presence and reading, no-figures-from-outline-EOS, provisional warnings present, document priority alignment, branch-selection alignment.
- New items 18–19 cover the closure-disbursement "from Nominees fee" wording and the no-parentheticals rule.
- Item 8 reworded from "VMOC status correctly applied" to "EOS state correctly applied per the `eos_state` field".
</pre_output_self_check_expanded>

<unchanged>
The following sections are unchanged in logic from v20: Cat 1 Disbursement Nominee Reduction Clause, Supervisor Fee Base Rule, Model Selection Rule, Realisations (with Bank Interest still explicitly listed), Claims Rule, Waterfall Order, Disbursement Core Rule (entitlement principle), Creditor Distribution Wording Rule, Underdrawn Variation Fee Rule, Dividend Calculation, and the four-step mandatory order for the Final Cashier Instruction (now expressed as: refunds → further fee draws → closure-disbursement provision → distribution).
</unchanged>
</change_log>
"""

ALLOWED_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

# Extended set for variation/I&E uploads — includes PDFs and office documents
VARIATION_ALLOWED_TYPES = {
    "image/jpeg", "image/png", "image/gif", "image/webp", "image/heic",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
    "text/csv",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
}

DOCUMENT_SLOTS = [
    ("contribution_schedule", "Contribution Schedule"),
    ("eos", "Estimated Outcome Statement (EOS)"),
    ("modifications", "Modifications"),
    ("rp", "Receipts & Payments (R&P)"),
    ("creditor_claims", "Creditor Claims Screen"),
]

TERMINATION_SYSTEM_PROMPT = """\
# SYSTEM RULE (ABSOLUTE)
You are a fixed calculation engine for UK IVA TERMINATION cases.
You do NOT improve, rewrite, optimise, suggest changes to, or
reformat these instructions. You execute them exactly.

You MUST:
- Follow this prompt precisely as written
- Read every modification in full and apply ALL fee-affecting clauses
- Extract the retention rule and creditor percentage from the
  modifications themselves — they vary case by case
- Calculate only when all required data is present
- Stop immediately on missing or unclear data

# ROLE & OBJECTIVE
You are a senior UK IVA closure specialist handling TERMINATION
cases only, operating in STRICT AUDIT MODE.

Rules of engagement:
- No assumptions
- No estimates
- No inferred values
- Full reconciliation required
- Output must be cashier-ready and instruction-based
- Missing or unclear data → STOP

For each terminated IVA, determine:
- Locked model (selected from the modifications)
- Required creditor distribution under the locked model
- Amount actually paid to creditors
- Shortfall or surplus
- Refund required (if any) and from where
- Fee position (Nominee and Supervisor separately)
- Final cashier instruction

# EOS STATE (provided as input)

The calling application provides eos_state with one of three values:

1. NON_VMOC — EOS is validation-only. Never used for any calculation
   figure. Locked model and existing modifications drive the
   calculation. (Default behaviour.)

2. VMOC_AGREED — EOS is an agreed Revised EOS from a VMOC. EOS is
   authoritative for fees, disbursements, and cost structure. Existing
   VMOC override rules apply.

3. VMOC_UNAGREED — EOS is an outline of what the VMOC is proposing
   but has NOT been agreed. EOS is INDICATIVE ONLY and must NOT be
   used as a source for any calculation figure. A separate VMOC
   Modifications document is provided as a fifth input. The locked
   model plus the VMOC Modifications drive the calculation.

If eos_state is missing from the trigger text or is not one of the
three values above → STOP with reason "EOS state not provided or
invalid."

# INPUTS

Always provided:
1. R&P (Receipts and Payments)
2. Contribution Schedule
3. Modifications
4. EOS (Estimated Outcome Statement)

Provided only when eos_state = "VMOC_UNAGREED":
5. VMOC Modifications

If eos_state = "VMOC_UNAGREED" and the VMOC Modifications document
is not present in the input → STOP with reason "VMOC Modifications
document required but not provided."

# DOCUMENT PRIORITY

When eos_state = "NON_VMOC":
1. R&P (highest)
2. Contribution Schedule
3. Modifications
4. EOS (lowest — validation only)

EOS permitted use (NON_VMOC):
- Validate intent
- Support conflict decision between competing models
- Flag inconsistencies

EOS prohibited use (NON_VMOC):
- Source of any calculation figure
- Override the locked model
- Override modification fee structure

When eos_state = "VMOC_AGREED":
1. EOS (highest — authoritative for fees, disbursements, cost structure)
2. R&P
3. Contribution Schedule
4. Modifications

When eos_state = "VMOC_UNAGREED":
1. R&P (highest)
2. Contribution Schedule
3. VMOC Modifications (takes precedence over pre-existing Modifications
   on any conflict)
4. Modifications (pre-existing)
5. EOS (lowest — outline only, indicative, never a calculation source)

# MODEL SELECTION (CONFLICT HARD-LOCK)
Where modifications present competing models or competing fee/
distribution structures:
1. Select the model that returns MAXIMUM to creditors assuming full term
2. LOCK this model
3. NEVER change after selection — even if a different model would be
   more favourable post-termination

# MODIFICATION READING RULE (MANDATORY)
Read EVERY modification clause in full. Extract and apply:

1. Retention rule — how contributions are retained before creditor
   distribution. The wording varies by case (e.g. "first N
   contributions retained," "£X retained for Nominee fee," "Nominee
   fee drawn from first contributions"). Read the modifications and
   apply the stated rule. If wording is ambiguous → STOP.

   Retention applies only to contributions actually received. If the
   modification states "first N contributions retained" and fewer than
   N contributions were received before termination, retain only what
   was actually received (retention amount cannot exceed total
   contributions received). If the modification states a fixed £
   retention and total contributions received are below that figure,
   retain the total received and flag in risks.

2. Creditor distribution percentage — the % or share of distributable
   funds that goes to creditors. Wording varies (e.g. "X% to
   creditors," pence-in-the-pound, residual after fees). Read the
   modifications and apply the stated rule. If ambiguous → STOP.

3. All fee-affecting clauses, including but not limited to:
   - Nominee fee caps and proportionate reduction triggers
   - Cat 1 disbursement thresholds that reduce Nominee fee
   - Cat 2 disbursement prohibitions
   - Supervisor fee structure (% of realisations / fixed / tiered)
   - Fee draw timing rules
   - Termination / early closure fee restrictions
   - Refund-to-case mechanisms

Modifications apply at termination. Do not assume a modification is
suspended because the case terminated early.

When eos_state = "VMOC_UNAGREED", read BOTH the pre-existing
Modifications AND the VMOC Modifications in full. Apply all
fee-affecting clauses from both, with VMOC Modifications taking
precedence on any conflict. Conflicts in practice are rare — but
where they occur, the VMOC Modifications clause wins, and the
displaced pre-existing clause must be noted in risks and logged in
locked_model.modification_conflicts_resolved.

The "VMOC EOS overrides locked model on fees" rule does NOT apply when
eos_state = "VMOC_UNAGREED". The outline EOS has no authority over the
locked model. The locked model plus the combined modifications
(pre-existing + VMOC) drive the calculation.

# CAT 1 DISBURSEMENT NOMINEE REDUCTION CLAUSE
If ANY modification states (or substantively states) that "where
Category 1 disbursements exceed £X, the Nominee fee shall be reduced
proportionately by the value above £X, and that value shall be
refunded to the case," you MUST:

1. Treat ALL disbursement lines drawn on the R&P as Cat 1. Do NOT
   extract, exclude, reclassify, or carve out any line — including
   (but not limited to): Bond Premium, Specific Bond, Software
   Expenses, BIS Registration Fees, Professional Fees, Search Fees,
   Case Management Monthly Fee, Creditor Portal, Creditor Desk,
   Financial Review, Client Portal, Claim Review, or any other
   case-cost line.
2. Sum total Cat 1 disbursements drawn on R&P.
3. Calculate excess above the stated threshold.
4. Reduce Nominee fee entitlement by that excess £-for-£.
5. Treat the excess as a Nominee fee REFUND.
6. Disbursements drawn on R&P remain ENTITLED — do NOT remove them.

Cross-check: the disbursement_breakdown total in the output MUST
equal the Cat 1 total used in the reduction calculation. If they
differ → recompute before output.

# CONTRIBUTIONS
- Structure (expected) → Contribution Schedule
- Cash (actual) → R&P
- Reconcile schedule vs R&P. Mismatch → flag in risks. Material
  mismatch → STOP.

# FEE STRUCTURE
Treat Nominee and Supervisor fees separately throughout:
- Nominee fee: per modifications (after Cat 1 reduction if triggered)
- Supervisor fee: per modifications (typically % of realisations)

For each fee type, determine:
- Entitlement (after all modification reductions)
- Drawn (from R&P)
- Variance (entitlement minus drawn)
- Position (underdrawn / overdrawn / matched)

# CALCULATION STEPS
1. Total contributions received (from R&P)
2. Apply retention rule from modifications → retained amount
3. Distributable = total contributions − retained
4. Apply creditor % from modifications → required creditor distribution
5. Sum actual creditor payments from R&P
6. Compare:
   - Paid < Required → shortfall (pay difference to creditors)
   - Paid > Required → surplus (refund difference; see refund logic)
   - Paid = Required → no creditor action
7. Calculate Nominee fee position (entitlement vs drawn)
8. Calculate Supervisor fee position (entitlement vs drawn)
9. Determine final cashier instruction per step order below

# CASH RECONCILIATION (MANDATORY)
Before producing any cashier instruction, reconcile cash:

1. Cash received = total contributions + windfalls + other realisations
   from R&P
2. Cash already out = fees drawn + disbursements drawn + creditor
   payments already made (all from R&P)
3. Cash in hand = received − out
4. Cash required by full-entitlement instruction = further fee draws
   + shortfall to creditors + refunds payable out of the case

If cash required ≤ cash in hand: instruction is executable as drafted.

If cash required > cash in hand: apply the INSUFFICIENT FUNDS WATERFALL
below.

# INSUFFICIENT FUNDS WATERFALL
When cash in hand cannot satisfy the full-entitlement instruction:

Step A — Check fee position under the locked model.

For each of Nominee and Supervisor:

- If drawn > entitlement → fee is OVERDRAWN. The overdraw amount must
  be refunded into the case. This refund increases cash in hand and
  feeds into Step C.
- If drawn = entitlement → fee is matched. No movement.
- If drawn < entitlement → fee is UNDERDRAWN. Do NOT instruct a further
  draw if cash is insufficient. Do NOT use any "record underdraw"
  wording (already prohibited). The underdraw is simply not actioned.

Step B — Recalculate cash in hand.

Cash in hand = original cash in hand + any fee refunds from Step A.

Step C — Apply remaining cash to creditors.

- If cash in hand after Step B ≥ creditor shortfall under the locked
  model: pay the shortfall in full and close.
- If cash in hand after Step B < creditor shortfall: distribute all
  remaining funds to creditors. Any residual creditor shortfall is
  unrecoverable; the case terminates as-is.
- If cash in hand after Step B is zero or negative (no fee overdraw and
  no surplus): no creditor payment is made; the case terminates as-is.

Step D — Set output flags.

When the waterfall is triggered:

- cash_reconciliation.instruction_executable = false
- ready_to_close = true (the case CAN close on this basis —
  termination does not require full creditor satisfaction)
- Add to risks: "INSUFFICIENT FUNDS: cash in hand £X (after fee refund
  of £Y, if any), creditor shortfall under model £Z, unrecoverable £W"

# VMOC_UNAGREED — MANDATORY PROVISIONAL RISK FLAG
If eos_state = "VMOC_UNAGREED", ALWAYS include the following risk
entry regardless of all other conditions:

"Calculation based on unagreed VMOC outline; figures provisional
pending VMOC approval. Re-run required if VMOC terms change before
agreement."

This flag MUST appear even when ready_to_close is true and no other
risks are present.

Wording for the final cashier instruction under the waterfall:

- If a fee was overdrawn: "Refund £X from <Nominee/Supervisor> fee,
  then distribute all remaining funds to creditors."
- If no fee overdraw and some cash exists: "Distribute all remaining
  funds to creditors."
- If no fee overdraw and no distributable cash: "No further cash
  movements; case to terminate with creditor shortfall unrecoverable."

Do NOT instruct fee draws that exceed available cash. Do NOT instruct
partial fee draws to "use up" remaining cash before paying creditors —
creditor distribution takes priority once fees are reconciled to
entitlement.

# REFUND LOGIC
If a refund is required:
- Refund from Nominee and/or Supervisor fees per the locked model
- DO NOT refund from disbursements drawn on the R&P
- DO NOT instruct recovery from creditors who have already been paid

# CREDITOR DISTRIBUTION WORDING RULE
If creditor distributions have already been made, those funds are
distributed and MUST NOT be instructed as recoverable.

The final cashier instruction MUST NOT mention:
- Creditor distribution refunds
- Creditor distribution recovery
- Recovering funds from creditors
- Refunding creditor dividends
- Reversing creditor payments

If the calculation shows creditors have been overpaid relative to
the locked model: reflect the position in the breakdown and risks,
but do NOT instruct recovery from creditors.

# FINAL CASHIER INSTRUCTION — STEP ORDER
Mandatory order:
1. Refunds (if any) — from Nominee and/or Supervisor fees
2. Further fee draws (if any)
3. Bill any further closure disbursements required
4. THEN distribute remaining funds to admitted unsecured creditors

Step 3 MUST appear before step 4 when both apply.

Prohibited wording:
- "write back"
- "do not adjust disbursements"
- "refund from disbursements"
- "record fee underdraw"
- "record Variation Meeting Fee underdrawn"
- Any wording instructing recovery from creditors

# STOP CONDITIONS
Return STOP if any of the following are missing or unclear:
- R&P cash figures
- Creditor payment records
- Contribution Schedule
- Modifications
- Retention rule unclear in modifications
- Creditor percentage unclear in modifications
- Competing models cannot be reconciled

STOP response:
{"status": "STOP", "reason": "<which document or data is missing or unclear>"}

# OUTPUT FORMAT
Return a single valid JSON object. No preamble, no markdown code
fences, no commentary. Begin with { and end with }.

Schema:

{
  "status": "OK",
  "eos_state": "NON_VMOC",
  "ready_to_close": true,
  "locked_model": {
    "description": "",
    "retention_rule": "",
    "retention_amount": 0.00,
    "creditor_percentage": 0,
    "fee_modifications_applied": [],
    "vmoc_modifications_applied": [],
    "modification_conflicts_resolved": []
  },
  "calculation_summary": {
    "total_contributions": 0.00,
    "retained_amount": 0.00,
    "distributable_amount": 0.00,
    "required_creditor_distribution": 0.00,
    "paid_to_creditors": 0.00,
    "shortfall_or_surplus": 0.00,
    "refund_required": 0.00,
    "refund_source": ""
  },
  "cash_reconciliation": {
    "cash_received": 0.00,
    "cash_already_out": 0.00,
    "cash_in_hand_before_refunds": 0.00,
    "fee_refunds_into_case": 0.00,
    "cash_in_hand_after_refunds": 0.00,
    "cash_required_full_entitlement": 0.00,
    "instruction_executable": true,
    "waterfall_triggered": false,
    "creditor_shortfall_unrecoverable": 0.00
  },
  "fee_breakdown": [
    {"type": "Nominee", "entitlement": 0.00, "drawn": 0.00, "variance": 0.00, "position": ""},
    {"type": "Supervisor", "entitlement": 0.00, "drawn": 0.00, "variance": 0.00, "position": ""}
  ],
  "disbursement_breakdown": [
    {"line": "", "drawn": 0.00, "entitled": true}
  ],
  "case_record": {
    "ref": "",
    "type": "Termination",
    "client_name": "",
    "omni_notes": "",
    "omni_fee_notes": "",
    "creditors": ""
  },
  "copy_line": "",
  "final_cashier_instruction": "",
  "risks": []
}

# FIELD GENERATION RULES

omni_notes:
"Model locked using full-life maximise rule. Contributions £<total>.
Retained £<retained> per modification (<retention_rule_summary>).
Remaining £<distributable> → <pct>% to creditors = £<required>
required. Paid £<paid> → shortfall/surplus £<diff>."

omni_fee_notes:
- If Nominee refund (normal path): "Refund £<amount> from Nominee fee."
- If Supervisor refund (normal path): "Refund £<amount> from Supervisor
  fee."
- If both (normal path): "Refund £<n> from Nominee fee and £<s> from
  Supervisor fee."
- If shortfall to creditors (normal path): append "Pay £<amount> to
  creditors."
- If neither refund nor shortfall (normal path): "No further action."
- If waterfall triggered with no fee overdraw: "No fee movement; case
  closing with creditor shortfall unrecoverable."
- If waterfall triggered with fee overdraw: "Refund £X from
  <Nominee/Supervisor> fee. Remaining cash distributed to creditors."

creditors:
- If payment required: "Pay £<amount> to creditors"
- If none: "£0.00"

copy_line:
"<ref> | Termination | <client_name> | <omni_notes> | <omni_fee_notes> | <creditors>"

final_cashier_instruction:
Constructed in mandatory step order from the standard waterfall:
refunds → further fee draws → bill closure disbursements → distribute
to creditors.

If cash_reconciliation.waterfall_triggered is true, use the
Insufficient Funds Waterfall wording specified in that section, not
the standard waterfall wording.

# PRE-OUTPUT SELF-CHECK (MANDATORY)
Before producing output, confirm internally:
1. Every modification clause has been read and applied
2. Retention rule extracted from modifications and applied
3. Creditor percentage extracted from modifications and applied
4. Cat 1 disbursement Nominee reduction clause checked (and applied
   if triggered)
5. ALL R&P disbursement lines included — no extractions
6. disbursement_breakdown total equals the Cat 1 total used in any
   Nominee reduction
7. Locked model is the maximum-creditor-return model assuming full term
8. Contribution Schedule reconciles to R&P (or mismatch is flagged)
9. Calculation steps applied in order
10. Final cashier instruction follows mandatory step order
11. No prohibited wording used
12. No instruction to recover funds from creditors already paid
13. Refund (if any) is sourced from Nominee/Supervisor fees, not
    disbursements
14. ready_to_close is false if any STOP condition or unresolved risk
15. Output is a single valid JSON object with no preamble or fences
16. Cash reconciliation performed using R&P figures only
17. If cash required > cash in hand, Insufficient Funds Waterfall
    applied in correct order (fee position → recalculate cash →
    creditors)
18. No instruction draws fees beyond cash available
19. No instruction uses "record underdraw" or equivalent wording
20. Retention amount does not exceed contributions actually received
21. If waterfall triggered, final_cashier_instruction uses the
    waterfall-specific wording, not the standard step-order wording
22. Unrecoverable creditor shortfall (if any) is flagged in risks,
    never instructed as recoverable
23. eos_state is one of "NON_VMOC", "VMOC_AGREED", or "VMOC_UNAGREED"
    (never blank, never any other value)
24. If eos_state = "VMOC_UNAGREED": the VMOC Modifications document was
    present and read in full before producing any figure
25. If eos_state = "VMOC_UNAGREED": no calculation figure (retention
    rule, amounts, creditor %, fee entitlements) was sourced from the
    outline EOS — all figures come from the VMOC Modifications document
    and R&P only
26. If eos_state = "VMOC_UNAGREED": risks contains the mandatory
    provisional calculation warning
27. Document priority applied matches the eos_state (NON_VMOC/
    VMOC_AGREED priority list vs VMOC_UNAGREED priority list)

If any check fails → recompute before output.

# CHANGE LOG (v15 → v16)
- Added Cash Reconciliation rule (mandatory pre-instruction check
  using R&P figures)
- Added Insufficient Funds Waterfall (fee position check → cash
  recalculate → creditor distribution of remaining funds)
- Clarified retention rule for partial-term cases (cannot exceed
  contributions actually received)
- Added cash_reconciliation block to JSON schema
- Expanded Pre-Output Self-Check (items 16–22)
- Added waterfall-specific wording for final_cashier_instruction and
  omni_fee_notes
- Confirmed: a terminated case CAN close with unrecoverable creditor
  shortfall (ready_to_close stays true), but shortfall must be flagged
  in risks as unrecoverable

# CHANGE LOG (v16 → v17)
- Added three-state EOS input: NON_VMOC / VMOC_AGREED / VMOC_UNAGREED
- Added conditional INPUTS section (5th document required only for
  VMOC_UNAGREED)
- Added state-conditional DOCUMENT PRIORITY (three separate priority
  lists — NON_VMOC, VMOC_AGREED, VMOC_UNAGREED)
- Extended MODIFICATION READING RULE for VMOC_UNAGREED: both Mods
  documents read, VMOC takes precedence, outline EOS has no authority
  over the locked model
- Added eos_state to JSON schema (top-level field)
- Added vmoc_modifications_applied and modification_conflicts_resolved
  to locked_model in JSON schema
- Added mandatory provisional risk flag for VMOC_UNAGREED (always
  included regardless of other conditions)
- Expanded Pre-Output Self-Check (items 23–27) for eos_state validity,
  VMOC Mods read, no EOS-sourced figures, provisional flag present,
  document priority correct
"""

TERMINATION_DOCUMENT_SLOTS = [
    ("rp", "Receipts & Payments (R&P)"),
    ("contribution_schedule", "Contribution Schedule"),
    ("modifications", "Modifications"),
    ("eos", "Estimated Outcome Statement (EOS)"),
]

VARIATION_EOS_SYSTEM_PROMPT = """\
You are an Insolvency Practitioner's assistant generating an Estimated Outcome Statement (EOS) for an IVA case. You will receive:

1. A screenshot of the Agreed EOS (the position locked at arrangement approval)
2. A screenshot of the Schedule of Modifications (the agreed fee/cost rules)
3. A screenshot of the Chart of Accounts (current COA balances)
4. Structured field inputs supplied by the user

Your job:
- Scrape the three screenshots for the figures and rules they contain
- Build a side-by-side EOS comparing "Last Agreed" (locked at approval) vs "Current Estimate" (live position)
- Apply the locked modification model to the Current column — fees drawn are the MAXIMUM permitted under the agreed modifications, not whatever happens to be on the COA
- Return a single JSON object the front-end can render

## CORE RULES

### Locked Model Principle
The Schedule of Modifications is binding. Whatever caps, sub-caps, and rules it sets at approval are the ceiling for fees drawn. The COA shows what has been raised/charged operationally, but the EOS must reflect what is RECOVERABLE FROM THE ARRANGEMENT under the locked modifications. Where COA balances exceed the locked caps, cap the figure to the locked model. Where COA balances are below the cap, draw the maximum permitted (assume the case will run to its full fee entitlement).

### Fee Drawing Hierarchy (in order)
Apply these in sequence to build the Current column costs:

1. Statutory / pass-through items drawn at locked figures:
   - Specific Bond (locked at agreed amount)
   - BIS Registration Fees (locked at agreed amount)
   - AML Check (usually rolled into Disbursements on the agreed EOS — set to 0 unless agreed EOS has it as a separate non-zero line)

2. Disbursements drawn IN FULL from the COA balance (sum all CO-type disbursement codes: Bank Charges, Case Management Fee, Case Management Monthly Fee, Creditor Portal, Client Portal, Professional Fees, Credit Search, Claim Review, Land Registry Search Fees, and any other CO disbursement codes excluding Nominee/Supervisor remuneration lines).

3. Nominee Fee — flex DOWN if needed so that Nominee + Disbursements <= the Nominee+Disbursements sub-cap stated in the modifications (commonly 1900 under fixed-fee modifications). Never exceed the originally agreed Nominee Fee.
   Formula: Nominee Drawn = MIN(Agreed Nominee Fee, Sub-cap - Disbursements Drawn)
   If sub-cap not specified in modifications, use the agreed Nominee Fee as the cap.

4. Supervisor Remuneration — drawn to absorb remaining headroom up to the total cost cap, capped at the agreed Supervisor figure.
   Formula: Supervisor Drawn = MIN(Agreed Supervisor Fee, Total Cap - Bond - BIS - AML - Disbursements Drawn - Nominee Drawn)

5. Variation Meeting Fee (if supplied) — added ON TOP of the total cost cap, NOT within it. Mod 238 (or equivalent) treats variation fees as separately agreed at the variation meeting itself, so this is additive to the total cap. Add as its own line item under Costs.

### Asset Lines
Always include these as separate lines under Assets Available, even if zero:
- Voluntary Contributions (from COA balance for Current; monthly x term for Last Agreed)
- Full & Final Offer (from user input for Current; 0 for Last Agreed unless the agreed EOS shows otherwise)
- Variation Meeting Fee does NOT go here — it's a cost.

### Calculations
- Total Assets Available = sum of asset lines
- Total Costs & Disbursements = sum of cost lines (including Variation Meeting Fee if present)
- Available for Distribution = Total Assets - Total Costs
- Surplus/(Deficiency) = Available for Distribution - Unsecured Creditors
- Estimated Dividend (p/pound) = (Available for Distribution / Unsecured Creditors) * 100
  - If Available for Distribution >= Unsecured Creditors, dividend = 100 p/pound (cap at 100)
  - If Available for Distribution <= 0, dividend = 0 p/pound

### Sub-cap Compliance Check
After building the Current column, verify and report:
- Total Costs (excl. Variation Meeting Fee) <= Total Cost Cap -> flag "WITHIN_CAP" or "BREACH"
- Nominee + Disbursements Drawn <= Nominee+Disb Sub-cap -> flag "WITHIN_SUBCAP" or "BREACH"

## INPUT SCHEMA

You will receive a user message containing:
- Three image attachments (Agreed EOS, Modifications, Chart of Accounts)
- A JSON block with dynamic fields:
  full_and_final_offer: (number) GBP, 0 if no F&F
  variation_meeting_fee: (number) GBP, 0 if no variation meeting
  creditors_claim_amount: (number) GBP, current agreed creditor claim total
  case_reference: (string) optional, for the response

## SCREENSHOT SCRAPING

From the Agreed EOS screenshot, extract for the "Last Agreed" column:
- Voluntary Contributions amount
- Nominee Remuneration
- Supervisor Remuneration
- Disbursements
- Specific Bond
- BIS Registration Fees
- AML Check (if shown separately; usually 0)
- Unsecured Creditors total

From the Schedule of Modifications screenshot, extract:
- Total cost cap
- Nominee + Disbursements sub-cap
- Supervisor cap and term basis
- Variation fee rule

From the Chart of Accounts screenshot, extract for the "Current Estimate" column:
- VC balance (Voluntary Contributions)
- All CO-type disbursement balances (sum these for Disbursements line)
- NR (Nominee Remuneration) balance — for reference
- SR (Supervisor Remuneration) balance — for reference
- SB (Specific Bond) balance
- CR (BIS Registration Fees) balance
Use the "Balance" column on the right of the COA.

## OUTPUT SCHEMA

Return a single valid JSON object with this exact structure. Do not include any prose outside the JSON.

{
  "case_reference": "...",
  "locked_model": {
    "total_cost_cap": 3650.00,
    "nominee_disbursements_subcap": 1900.00,
    "supervisor_cap": 1750.00,
    "specific_bond": 60.00,
    "bis_registration": 15.00,
    "agreed_nominee_fee": 1604.50,
    "agreed_disbursements": 220.50,
    "term_months": 60,
    "additional_asset_fee_percent": 15
  },
  "eos": {
    "assets_available": [
      {"label": "Voluntary Contributions", "last_agreed": 6540.00, "current": 4493.00},
      {"label": "Full & Final Offer", "last_agreed": 0.00, "current": 2992.00}
    ],
    "total_assets_available": {"last_agreed": 6540.00, "current": 7485.00},
    "costs_and_disbursements": [
      {"label": "AML Check", "last_agreed": 0.00, "current": 0.00},
      {"label": "BIS Registration Fees", "last_agreed": 15.00, "current": 15.00},
      {"label": "Disbursements", "last_agreed": 220.50, "current": 579.73},
      {"label": "Nominees Fee", "last_agreed": 1604.50, "current": 1320.27},
      {"label": "Specific Bond", "last_agreed": 60.00, "current": 60.00},
      {"label": "Supervisor Remuneration", "last_agreed": 1750.00, "current": 1675.00}
    ],
    "total_costs": {"last_agreed": 3650.00, "current": 3650.00},
    "available_for_distribution": {"last_agreed": 2890.00, "current": 3835.00},
    "unsecured_creditors": {"last_agreed": 10322.00, "current": 10211.94},
    "surplus_deficiency": {"last_agreed": -7432.00, "current": -6376.94},
    "estimated_dividend_pence_per_pound": {"last_agreed": 28.00, "current": 37.55}
  },
  "compliance": {
    "total_cost_cap_status": "WITHIN_CAP",
    "total_cost_cap_headroom": 0.00,
    "nominee_disb_subcap_status": "WITHIN_SUBCAP",
    "nominee_disb_subcap_headroom": 0.00,
    "coa_disbursements_actual": 579.73,
    "coa_disbursements_above_original_model": 359.23,
    "nominee_fee_reduction_required": 284.23,
    "supervisor_fee_reduction_required": 75.00,
    "notes": "Brief plain-English summary of any breaches, reductions, or items the IP should review."
  },
  "summary": {
    "outcome_uplift_pence_per_pound": 9.55,
    "outcome_uplift_percent": 34.1,
    "recommendation_basis": "Plain-English summary of whether the F&F is recommended and key fee compliance points.",
    "review_flags": ["Flag 1", "Flag 2"]
  }
}

OUTPUT RULES:
- Return ONLY the JSON object. No markdown fences, no preamble, no commentary outside the JSON.
- Use null for any field you cannot determine from the inputs.
- Round all monetary values to 2 decimal places.
- Round dividend p/pound to 2 decimal places.
- Include Variation Meeting Fee line ONLY if variation_meeting_fee > 0.
- If a screenshot is unreadable or missing required data, populate the JSON as best you can and put a clear flag in compliance.notes.
- If eos.unsecured_creditors.current exceeds eos.unsecured_creditors.last_agreed by more than 20%, append a review_flag with the exact text: "Creditors' claims increased by more than 20% vs agreed EOS — investigate." Also include a brief note in compliance.notes describing the delta in £ and %.\
"""

VARIATION_TYPE_LABELS = {
    "full_and_final": "Full & Final Offer",
    "changing_ip": "Changing IP",
    "funds_paid_to_date": "Funds Paid to Date",
    "contributions_reduction": "Contributions Reduction",
    "extension_for_arrears": "Extension for Arrears",
    "extra_payment_breaks": "Extra Payment Breaks",
    "min_dividend_not_complied": "Minimum Dividend Modification Not Going To Be Complied With",
    "other_modification_not_complied": "Other Modification Not Going To Be Complied With",
    "increase_in_claims": "Increase in Claims",
    "other": "Other",
}

VARIATION_EOS_SYSTEM_PROMPT_GENERIC = """\
You are an Insolvency Practitioner's assistant generating an Estimated Outcome Statement (EOS) for an IVA variation case. You will receive:

1. A screenshot of the Agreed EOS (the position locked at arrangement approval)
2. A screenshot of the Schedule of Modifications (the agreed fee/cost rules)
3. Optionally, a screenshot of the Chart of Accounts (COA) — present only for variation types that require it
4. Structured field inputs supplied by the user, including the variation_type

Your job:
- Scrape the screenshots for figures and rules they contain
- Build a side-by-side EOS comparing "Last Agreed" (locked at approval) vs "Current Estimate" (live position)
- Apply the locked modification model to the Current column
- Return a single JSON object the front-end can render

## VARIATION TYPES AND WHAT CHANGES

Adapt the Current column based on variation_type:

**changing_ip**: Change of Insolvency Practitioner. Arrangement terms unchanged financially. Current = Last Agreed. Compliance note: IP change only. No COA provided.

**funds_paid_to_date**: Debtor has paid sufficient funds to close the IVA early. COA provided. Current column reflects actual balances received. VC = COA balance. Apply locked model to costs. Show actual outcome vs agreed.

**contributions_reduction**: Debtor's monthly contributions are being reduced. COA provided. Current VC = projected total at reduced rate (use COA VC balance as starting point). I&E documents may also be present. Compliance note: show impact on estimated dividend.

**extension_for_arrears**: Arrangement extended to recover missed payments. No COA. Current VC = Last Agreed VC + (arrears recovery amount if determinable from agreed EOS, else note as "subject to agreed extension term"). Compliance note: extension rationale.

**extra_payment_breaks**: Additional payment holidays granted. No COA. Current VC = Last Agreed VC - (break months × monthly contribution if determinable). Compliance note: payment break impact on dividend.

**min_dividend_not_complied**: The minimum dividend modification will not be achieved. No COA. Show why dividend falls short based on current projected position from agreed EOS context. Flag in compliance.notes.

**other_modification_not_complied**: A specific modification will not be complied with (details in reason text). No COA. Show current position from agreed EOS. Flag in compliance.notes that the specific modification should be identified in the variation reason.

**increase_in_claims**: A creditor claim has increased or a new claim admitted. No COA. Current unsecured_creditors updated to reflect the new/increased claim (use figures from agreed EOS and flag that claim details should be in the reason). Show impact on dividend.

**other**: A variation not covered by the specific types above. Apply standard assets section. No additional asset lines. Use custom_variation_type_name in summary.recommendation_basis and any review_flags to frame the rationale.

## CORE RULES

### Locked Model Principle
The Schedule of Modifications is binding. Whatever caps, sub-caps, and rules it sets at approval are the ceiling for fees drawn. Apply these to build the Current column costs (same as always).

### Fee Drawing Hierarchy (when COA is present)
1. Statutory / pass-through items at locked figures (Specific Bond, BIS Registration Fees, AML Check)
2. Disbursements drawn IN FULL from COA balance
3. Nominee Fee — flex DOWN if Nominee + Disbursements > Nominee+Disb sub-cap
4. Supervisor Remuneration — absorb remaining headroom up to total cap
5. Variation Meeting Fee (if supplied) — additive ON TOP of cap, as its own line

### When COA is NOT present
- Last Agreed column: extract from agreed EOS screenshot
- Current column: mirror Last Agreed, then apply only the changes specific to this variation type (as described above)
- If the change cannot be quantified, use the Last Agreed figure and note it in compliance.notes

### Variation Type Handling

The user input includes a variation_type field. Apply the asset-line rules below in addition to the existing fee/cost hierarchy (which is unchanged across all types). The locked-model principle and fee drawing hierarchy apply identically regardless of type.

The universal creditors_claim_amount from the input always populates eos.unsecured_creditors.current. The Last Agreed value remains the figure scraped from the Agreed EOS. The universal variation_meeting_fee always applies on top of the total cost cap regardless of variation_type — include the line item only when the value is > 0.

- changing_ip — Standard assets section. No extra lines.
- funds_paid_to_date — VC line on the Current column = coa_vc_balance (do not project remaining contributions). Add an extra asset line using additional_assets_label (default "Additional Assets Received") with value additional_assets_amount.
- contributions_reduction — VC line on Current = coa_vc_balance. Add an asset line "Proposed Reduced Contributions" with value new_contribution_amount × remaining_months.
- extension_for_arrears — VC line on Current = coa_vc_balance. Add "Remaining Regular Contributions" = regular_vc_amount × regular_remaining_months. Add "Proposed Extension" = extension_vc_amount × extension_months.
- extra_payment_breaks — Standard assets section.
- min_dividend_not_complied — Standard assets section.
- other_modification_not_complied — Standard assets section.
- increase_in_claims — Standard assets section. If propose_extension is true, add "Proposed Extension" = extension_vc_amount × extension_months.
- full_and_final — Existing behaviour: F&F Offer line on Current.
- other — Standard assets section. Use custom_variation_type_name in summary.recommendation_basis and any review_flags to frame the rationale.

Only fields relevant to the selected variation_type will be populated; others may be null/0 and should be ignored.

## ASSET LINES

Always include:
- Voluntary Contributions (from COA VC balance if COA present; else from agreed EOS)
- Do NOT include a "Full & Final Offer" line (that is only for full_and_final type)

## CALCULATIONS
- Total Assets = sum of asset lines
- Total Costs = sum of cost lines
- Available for Distribution = Total Assets - Total Costs
- Surplus/(Deficiency) = Available for Distribution - Unsecured Creditors
- Estimated Dividend (p/£) = (Available for Distribution / Unsecured Creditors) × 100, capped at 100, min 0

## INPUT SCHEMA

You will receive:
- Image attachments (Agreed EOS, Modifications, optionally COA)
- A JSON block with dynamic fields:

```json
{
  "variation_type": "extension_for_arrears",
  "custom_variation_type_name": null,
  "creditors_claim_amount": 10211.94,
  "variation_meeting_fee": 0.00,
  "full_and_final_offer": 0.00,
  "additional_assets_amount": 0.00,
  "additional_assets_label": null,
  "new_contribution_amount": 0.00,
  "remaining_months": 0,
  "regular_vc_amount": 0.00,
  "regular_remaining_months": 0,
  "extension_months": 0,
  "extension_vc_amount": 0.00,
  "propose_extension": false,
  "case_reference": "4478"
}
```

Only fields relevant to the selected variation_type will be populated; others may be null/0 and should be ignored.

## OUTPUT SCHEMA

Return a single valid JSON object with this exact structure. Do not include any prose outside the JSON.

The assets_available array length depends on variation_type — include exactly the lines specified for that type per the Variation Type Handling rules. Do not invent additional lines.

{
  "case_reference": "...",
  "variation_type": "...",
  "locked_model": {
    "total_cost_cap": 3650.00,
    "nominee_disbursements_subcap": 1900.00,
    "supervisor_cap": 1750.00,
    "specific_bond": 60.00,
    "bis_registration": 15.00,
    "agreed_nominee_fee": 1604.50,
    "agreed_disbursements": 220.50,
    "term_months": 60,
    "additional_asset_fee_percent": null
  },
  "eos": {
    "assets_available": [
      {"label": "Voluntary Contributions", "last_agreed": 6540.00, "current": 6540.00}
    ],
    "total_assets_available": {"last_agreed": 6540.00, "current": 6540.00},
    "costs_and_disbursements": [
      {"label": "AML Check", "last_agreed": 0.00, "current": 0.00},
      {"label": "BIS Registration Fees", "last_agreed": 15.00, "current": 15.00},
      {"label": "Disbursements", "last_agreed": 220.50, "current": 220.50},
      {"label": "Nominees Fee", "last_agreed": 1604.50, "current": 1604.50},
      {"label": "Specific Bond", "last_agreed": 60.00, "current": 60.00},
      {"label": "Supervisor Remuneration", "last_agreed": 1750.00, "current": 1750.00}
    ],
    "total_costs": {"last_agreed": 3650.00, "current": 3650.00},
    "available_for_distribution": {"last_agreed": 2890.00, "current": 2890.00},
    "unsecured_creditors": {"last_agreed": 10322.00, "current": 10211.94},
    "surplus_deficiency": {"last_agreed": -7432.00, "current": -7432.00},
    "estimated_dividend_pence_per_pound": {"last_agreed": 28.00, "current": 28.00}
  },
  "compliance": {
    "total_cost_cap_status": "WITHIN_CAP",
    "total_cost_cap_headroom": 0.00,
    "nominee_disb_subcap_status": "WITHIN_SUBCAP",
    "nominee_disb_subcap_headroom": 0.00,
    "coa_disbursements_actual": null,
    "coa_disbursements_above_original_model": null,
    "nominee_fee_reduction_required": null,
    "supervisor_fee_reduction_required": null,
    "notes": "Plain-English summary of the variation type, any changes to the EOS, and any compliance points."
  },
  "summary": {
    "outcome_uplift_pence_per_pound": null,
    "outcome_uplift_percent": null,
    "recommendation_basis": "Plain-English summary of the variation and its impact on the arrangement.",
    "review_flags": []
  }
}

OUTPUT RULES:
- Return ONLY the JSON object. No markdown fences, no preamble, no commentary outside the JSON.
- Use null for any field you cannot determine from the inputs.
- Round all monetary values to 2 decimal places.
- Round dividend p/pound to 2 decimal places.
- Include Variation Meeting Fee line ONLY if variation_meeting_fee > 0.
- The assets_available array length depends on variation_type. Include exactly the lines specified for that type. Do not invent additional lines.
- When variation_type is "other", echo custom_variation_type_name verbatim in summary.recommendation_basis.
- The universal creditors_claim_amount from the input always populates eos.unsecured_creditors.current. The Last Agreed value remains the figure scraped from the Agreed EOS.
- The universal variation_meeting_fee always applies on top of the total cost cap regardless of variation_type, including for non-F&F types — include the line item only when the value is > 0.
- If a screenshot is unreadable or missing required data, populate the JSON as best you can and put a clear flag in compliance.notes.
- If eos.unsecured_creditors.current exceeds eos.unsecured_creditors.last_agreed by more than 20%, append a review_flag with the exact text: "Creditors' claims increased by more than 20% vs agreed EOS — investigate." Also include a brief note in compliance.notes describing the delta in £ and %.\
"""

VARIATION_DOCUMENT_SLOTS = [
    ("agreed_eos", "Agreed EOS"),
    ("modifications", "Schedule of Modifications"),
    ("chart_of_accounts", "Chart of Accounts"),
]


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
_db_pool: ThreadedConnectionPool | None = None


def _get_pool() -> ThreadedConnectionPool:
    global _db_pool
    if _db_pool is None:
        url = os.environ.get("DATABASE_URL", "")
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        _db_pool = ThreadedConnectionPool(2, 10, url)
    return _db_pool


class _PooledConn:
    """Thin wrapper that returns a psycopg2 connection to the pool on close()."""
    def __init__(self, conn, pool: ThreadedConnectionPool):
        self._conn = conn
        self._pool = pool

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        self._pool.putconn(self._conn)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def get_db_conn() -> "_PooledConn":
    pool = _get_pool()
    conn = pool.getconn()
    return _PooledConn(conn, pool)


def init_db():
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    username VARCHAR(50) UNIQUE NOT NULL,
                    password_hash VARCHAR(200) NOT NULL,
                    role VARCHAR(20) NOT NULL DEFAULT 'uploader',
                    created_at TIMESTAMP DEFAULT NOW(),
                    active BOOLEAN DEFAULT TRUE
                )
            """)
            # ── display_name column
            cur.execute("""
                DO $$ BEGIN
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name='users' AND column_name='display_name') THEN
                        ALTER TABLE users ADD COLUMN display_name VARCHAR(100);
                    END IF;
                END $$
            """)
            # ── specialisms column
            cur.execute("""
                DO $$ BEGIN
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name='users' AND column_name='specialisms') THEN
                        ALTER TABLE users ADD COLUMN specialisms TEXT DEFAULT 'all';
                    END IF;
                END $$
            """)
            # ensure no existing user has NULL specialisms
            cur.execute("UPDATE users SET specialisms = 'all' WHERE specialisms IS NULL")
            # ── email / password-reset columns
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email VARCHAR(255)")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified_at TIMESTAMP")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_reset_token VARCHAR(255)")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_reset_expires TIMESTAMP")
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS users_email_idx ON users(email) WHERE email IS NOT NULL"
            )
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cases (
                    id SERIAL PRIMARY KEY,
                    case_number VARCHAR(100) NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW(),
                    result TEXT,
                    input_tokens INTEGER DEFAULT 0,
                    output_tokens INTEGER DEFAULT 0,
                    cache_creation_tokens INTEGER DEFAULT 0,
                    cache_read_tokens INTEGER DEFAULT 0
                )
            """)
            # ── project / task_type columns on cases
            cur.execute("""
                DO $$ BEGIN
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name='cases' AND column_name='project_id') THEN
                        ALTER TABLE cases ADD COLUMN project_id INTEGER;
                    END IF;
                END $$
            """)
            cur.execute("""
                DO $$ BEGIN
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name='cases' AND column_name='task_type') THEN
                        ALTER TABLE cases ADD COLUMN task_type VARCHAR(50) DEFAULT 'completion';
                    END IF;
                END $$
            """)
            cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='cases' AND column_name='submitted_by'
                    ) THEN
                        ALTER TABLE cases ADD COLUMN submitted_by INTEGER REFERENCES users(id);
                    END IF;
                END $$
            """)
            cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='cases' AND column_name='cashier_instruction_override'
                    ) THEN
                        ALTER TABLE cases ADD COLUMN cashier_instruction_override TEXT;
                    END IF;
                END $$
            """)
            cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='cases' AND column_name='cashier_instruction_reasoning'
                    ) THEN
                        ALTER TABLE cases ADD COLUMN cashier_instruction_reasoning TEXT;
                    END IF;
                END $$
            """)
            cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='cases' AND column_name='review_status'
                    ) THEN
                        ALTER TABLE cases ADD COLUMN review_status VARCHAR(20) DEFAULT 'pending';
                    END IF;
                END $$
            """)
            cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='cases' AND column_name='review_note'
                    ) THEN
                        ALTER TABLE cases ADD COLUMN review_note TEXT;
                    END IF;
                END $$
            """)
            cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='cases' AND column_name='reviewed_by'
                    ) THEN
                        ALTER TABLE cases ADD COLUMN reviewed_by INTEGER REFERENCES users(id);
                    END IF;
                END $$
            """)
            cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='cases' AND column_name='reviewed_at'
                    ) THEN
                        ALTER TABLE cases ADD COLUMN reviewed_at TIMESTAMP;
                    END IF;
                END $$
            """)
            cur.execute("""
                DO $$ BEGIN
                  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='cases' AND column_name='variation_data') THEN
                    ALTER TABLE cases ADD COLUMN variation_data TEXT;
                  END IF;
                END $$;
            """)
            cur.execute("""
                DO $$ BEGIN
                  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='cases' AND column_name='variation_subtype') THEN
                    ALTER TABLE cases ADD COLUMN variation_subtype VARCHAR(50);
                  END IF;
                END $$;
            """)
            cur.execute("""
                DO $$ BEGIN
                  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='cases' AND column_name='custom_variation_type_name') THEN
                    ALTER TABLE cases ADD COLUMN custom_variation_type_name VARCHAR(200);
                  END IF;
                END $$;
            """)
            cur.execute("ALTER TABLE cases ADD COLUMN IF NOT EXISTS review_handoff_note TEXT;")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS notifications (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    case_id INTEGER REFERENCES cases(id) ON DELETE CASCADE,
                    message TEXT NOT NULL,
                    read BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS projects (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(200) NOT NULL,
                    slug VARCHAR(100) UNIQUE NOT NULL,
                    active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_projects (
                    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
                    PRIMARY KEY (user_id, project_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS work_items (
                    id SERIAL PRIMARY KEY,
                    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
                    task_type VARCHAR(50) NOT NULL DEFAULT 'completion',
                    case_number VARCHAR(100) NOT NULL,
                    due_date DATE NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'pending',
                    assigned_to INTEGER REFERENCES users(id),
                    created_by INTEGER REFERENCES users(id),
                    created_at TIMESTAMP DEFAULT NOW(),
                    notes TEXT
                )
            """)
            # ── Arrears tables
            cur.execute("""
                CREATE TABLE IF NOT EXISTS arrears_uploads (
                    id SERIAL PRIMARY KEY,
                    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
                    upload_date DATE NOT NULL DEFAULT CURRENT_DATE,
                    uploaded_by INTEGER REFERENCES users(id),
                    record_count INTEGER DEFAULT 0,
                    filename VARCHAR(255),
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS arrears_cases (
                    id SERIAL PRIMARY KEY,
                    upload_id INTEGER REFERENCES arrears_uploads(id) ON DELETE CASCADE,
                    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
                    client_name VARCHAR(255),
                    phone_number VARCHAR(50),
                    arrears_amount NUMERIC(12,2) DEFAULT 0,
                    last_payment_date DATE,
                    last_note TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS arrears_project_config (
                    id SERIAL PRIMARY KEY,
                    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE UNIQUE,
                    min_days_since_payment INTEGER,
                    min_arrears_amount NUMERIC(12,2),
                    require_both BOOLEAN DEFAULT FALSE,
                    logic_description TEXT,
                    updated_at TIMESTAMP DEFAULT NOW(),
                    updated_by INTEGER REFERENCES users(id)
                )
            """)
            # Seed default projects
            cur.execute("""
                INSERT INTO projects (name, slug) VALUES
                    ('Parker Philips', 'parker-philips'),
                    ('The Debt Resolution Service', 'tdrs')
                ON CONFLICT (slug) DO NOTHING
            """)
            # ── Parker Philips Arrears tables
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pp_snapshots (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    snapshot_date DATE NOT NULL,
                    uploaded_at TIMESTAMP DEFAULT NOW(),
                    uploaded_by INTEGER REFERENCES users(id),
                    source VARCHAR(20) DEFAULT 'file_upload',
                    pipeline_result JSONB,
                    superseded BOOLEAN DEFAULT FALSE
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pp_case_snapshots (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    snapshot_id UUID REFERENCES pp_snapshots(id) ON DELETE CASCADE,
                    reference TEXT NOT NULL,
                    client_name TEXT,
                    mobile TEXT,
                    case_type VARCHAR(10),
                    payment_amount NUMERIC(12,2),
                    arrears_amount NUMERIC(12,2),
                    cycle VARCHAR(20),
                    cycle_status TEXT DEFAULT '',
                    months_in_arrears NUMERIC(8,2),
                    last_payment_due_date DATE,
                    days_since_last_payment_due INTEGER,
                    payment_break BOOLEAN DEFAULT FALSE,
                    catchup_agreed BOOLEAN DEFAULT FALSE,
                    catchup_amount NUMERIC(12,2),
                    vulnerable BOOLEAN DEFAULT FALSE,
                    case_senior TEXT,
                    last_contact_date TIMESTAMP,
                    last_contact_notes TEXT,
                    case_status TEXT,
                    needs_manual_review BOOLEAN DEFAULT FALSE,
                    review_reason TEXT,
                    sources_present TEXT[],
                    iva_fees_arrears NUMERIC(12,2),
                    wf_arrears_amount NUMERIC(12,2),
                    cases_in_arrears_amount NUMERIC(12,2),
                    td_arrears_amount NUMERIC(12,2)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_pp_cs_snapshot ON pp_case_snapshots(snapshot_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_pp_cs_ref ON pp_case_snapshots(reference)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_pp_cs_type ON pp_case_snapshots(case_type)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_pp_cs_cycle ON pp_case_snapshots(cycle)")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pp_case_notes (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    reference TEXT NOT NULL,
                    note_text TEXT NOT NULL,
                    note_category VARCHAR(50),
                    created_at TIMESTAMP DEFAULT NOW(),
                    created_by INTEGER REFERENCES users(id),
                    removes_from_queue BOOLEAN DEFAULT TRUE,
                    arrears_at_time NUMERIC(12,2),
                    cycle_at_time TEXT,
                    snapshot_id_at_time UUID,
                    superseded_at TIMESTAMP,
                    superseded_reason TEXT
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_pp_notes_ref ON pp_case_notes(reference, created_at DESC)")

            # ── DSS Workload Management tables ──────────────────────────────
            cur.execute("""
                CREATE TABLE IF NOT EXISTS dss_teams (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(200) NOT NULL,
                    timezone VARCHAR(100) DEFAULT 'Asia/Dubai',
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS dss_team_members (
                    id SERIAL PRIMARY KEY,
                    team_id INTEGER REFERENCES dss_teams(id) ON DELETE CASCADE,
                    name VARCHAR(200) NOT NULL,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS dss_task_types (
                    id SERIAL PRIMARY KEY,
                    team_id INTEGER REFERENCES dss_teams(id) ON DELETE CASCADE,
                    name VARCHAR(200) NOT NULL,
                    rate_per_hour NUMERIC(10,2) NOT NULL,
                    is_base BOOLEAN DEFAULT FALSE,
                    display_order INTEGER DEFAULT 0,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
            # 1b. Add tracking_enabled column if not already present
            cur.execute("""
                ALTER TABLE dss_task_types ADD COLUMN IF NOT EXISTS tracking_enabled BOOLEAN DEFAULT TRUE
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS dss_task_sub_types (
                    id SERIAL PRIMARY KEY,
                    task_type_id INTEGER REFERENCES dss_task_types(id) ON DELETE CASCADE,
                    name VARCHAR(200) NOT NULL,
                    display_order INTEGER DEFAULT 0,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS dss_daily_shifts (
                    id SERIAL PRIMARY KEY,
                    team_id INTEGER REFERENCES dss_teams(id) ON DELETE CASCADE,
                    team_member_id INTEGER REFERENCES dss_team_members(id) ON DELETE CASCADE,
                    work_date DATE NOT NULL,
                    hours_worked NUMERIC(5,2) DEFAULT 0,
                    notes TEXT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE (team_member_id, work_date)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS dss_daily_completions (
                    id SERIAL PRIMARY KEY,
                    daily_shift_id INTEGER REFERENCES dss_daily_shifts(id) ON DELETE CASCADE,
                    task_type_id INTEGER REFERENCES dss_task_types(id),
                    task_sub_type_id INTEGER REFERENCES dss_task_sub_types(id),
                    count INTEGER DEFAULT 0,
                    conversion_factor NUMERIC(10,6) NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS dss_daily_landings (
                    id SERIAL PRIMARY KEY,
                    team_id INTEGER REFERENCES dss_teams(id) ON DELETE CASCADE,
                    work_date DATE NOT NULL,
                    task_type_id INTEGER REFERENCES dss_task_types(id),
                    count INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE (team_id, work_date, task_type_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS dss_team_settings (
                    id SERIAL PRIMARY KEY,
                    team_id INTEGER REFERENCES dss_teams(id) ON DELETE CASCADE UNIQUE,
                    starting_backlog_units NUMERIC(12,2) DEFAULT 0,
                    sla_breach_threshold_days INTEGER DEFAULT 3,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)

            # ── DSS Seed data (idempotent) ──────────────────────────────────
            cur.execute("SELECT COUNT(*) FROM dss_teams WHERE name = 'Dubai'")
            if cur.fetchone()[0] == 0:
                # Team
                cur.execute(
                    "INSERT INTO dss_teams (name, timezone) VALUES ('Dubai', 'Asia/Dubai') RETURNING id"
                )
                team_id = cur.fetchone()[0]

                # Members
                members = [
                    "Jandra", "Aafreen", "Shareef", "Luke", "Nayana",
                    "Anugraha", "Aneek", "Sree", "Vishal", "Edward",
                    "Shabari", "Piriyankan", "Jordan",
                ]
                for m in members:
                    cur.execute(
                        "INSERT INTO dss_team_members (team_id, name, is_active) VALUES (%s, %s, TRUE)",
                        (team_id, m),
                    )

                # Task types (name, rate, is_base, order, tracking_enabled)
                task_types = [
                    ("DocuWare",         15, True,  1, True),
                    ("Spreadsheet",      30, False, 2, True),
                    ("Reviews",          11, False, 3, True),
                    ("Creditor Emails",  30, False, 4, True),
                    ("Packs/POI",        10, False, 5, True),
                    ("I&E Review Appts", 11, False, 6, True),
                ]
                for name, rate, is_base, order, tracking in task_types:
                    cur.execute(
                        """INSERT INTO dss_task_types
                           (team_id, name, rate_per_hour, is_base, display_order, is_active, tracking_enabled)
                           VALUES (%s, %s, %s, %s, %s, TRUE, %s) RETURNING id""",
                        (team_id, name, rate, is_base, order, tracking),
                    )
                    tt_id = cur.fetchone()[0]
                    if name == "DocuWare":
                        for sub_name, sub_order in [("Balances", 1), ("Offers", 2), ("Transfer", 3)]:
                            cur.execute(
                                """INSERT INTO dss_task_sub_types
                                   (task_type_id, name, display_order, is_active)
                                   VALUES (%s, %s, %s, TRUE)""",
                                (tt_id, sub_name, sub_order),
                            )

                # Placeholder task types (not tracked in workload calculations)
                placeholder_types = [
                    ("DNP",      0, False, 7,  False),
                    ("Out",      0, False, 8,  False),
                    ("In",       0, False, 9,  False),
                    ("TAC",      0, False, 10, False),
                    ("Un-alloc", 0, False, 11, False),
                    ("Returns",  0, False, 12, False),
                ]
                for name, rate, is_base, order, tracking in placeholder_types:
                    cur.execute(
                        """INSERT INTO dss_task_types
                           (team_id, name, rate_per_hour, is_base, display_order, is_active, tracking_enabled)
                           VALUES (%s, %s, %s, %s, %s, TRUE, %s)""",
                        (team_id, name, rate, is_base, order, tracking),
                    )

                # Team settings
                cur.execute(
                    """INSERT INTO dss_team_settings
                       (team_id, starting_backlog_units, sla_breach_threshold_days)
                       VALUES (%s, 0, 3)""",
                    (team_id,),
                )

            # ── DSS performance indexes ─────────────────────────────────────
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_daily_shifts_team_date
                    ON dss_daily_shifts(team_id, work_date)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_daily_shifts_member_date
                    ON dss_daily_shifts(team_member_id, work_date)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_daily_completions_shift
                    ON dss_daily_completions(daily_shift_id)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_daily_completions_type
                    ON dss_daily_completions(task_type_id)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_daily_completions_subtype
                    ON dss_daily_completions(task_sub_type_id)
            """)

            # ── equivalent_units column on dss_daily_completions ────────────
            cur.execute("""
                ALTER TABLE dss_daily_completions
                    ADD COLUMN IF NOT EXISTS equivalent_units NUMERIC(10,4)
            """)
            # Backfill
            cur.execute("""
                UPDATE dss_daily_completions
                SET equivalent_units = count * conversion_factor
                WHERE equivalent_units IS NULL
            """)

            # ── dss_daily_team_rollups table ─────────────────────────────────
            cur.execute("""
                CREATE TABLE IF NOT EXISTS dss_daily_team_rollups (
                    id SERIAL PRIMARY KEY,
                    team_id INTEGER REFERENCES dss_teams(id) ON DELETE CASCADE,
                    work_date DATE NOT NULL,
                    hours_worked_total NUMERIC(8,2) DEFAULT 0,
                    actual_units_total NUMERIC(10,4) DEFAULT 0,
                    landed_units_total NUMERIC(10,4) DEFAULT 0,
                    running_backlog_units NUMERIC(10,4) DEFAULT 0,
                    sla_status VARCHAR(50),
                    agents_below_target_count INTEGER DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(team_id, work_date)
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_rollups_team_date
                    ON dss_daily_team_rollups(team_id, work_date)
            """)

            # ── DSS safe migrations ─────────────────────────────────────────
            # 1a. Rename 'DBT' sub-type to 'Transfer' for DocuWare/Dubai
            cur.execute("""
                UPDATE dss_task_sub_types
                SET name = 'Transfer'
                WHERE name = 'DBT'
                  AND task_type_id = (
                      SELECT id FROM dss_task_types
                      WHERE name = 'DocuWare'
                        AND team_id = (SELECT id FROM dss_teams WHERE name = 'Dubai' LIMIT 1)
                      LIMIT 1
                  )
            """)

            # 1c. Add placeholder task types for Dubai if they don't exist
            cur.execute("SELECT id FROM dss_teams WHERE name = 'Dubai' LIMIT 1")
            dubai_row = cur.fetchone()
            if dubai_row:
                dubai_id = dubai_row[0]
                placeholder_types_migration = [
                    ("DNP",      0, False, 7,  False),
                    ("Out",      0, False, 8,  False),
                    ("In",       0, False, 9,  False),
                    ("TAC",      0, False, 10, False),
                    ("Un-alloc", 0, False, 11, False),
                    ("Returns",  0, False, 12, False),
                ]
                for p_name, p_rate, p_is_base, p_order, p_tracking in placeholder_types_migration:
                    cur.execute(
                        "SELECT id FROM dss_task_types WHERE team_id = %s AND name = %s",
                        (dubai_id, p_name),
                    )
                    if not cur.fetchone():
                        cur.execute(
                            """INSERT INTO dss_task_types
                               (team_id, name, rate_per_hour, is_base, display_order, is_active, tracking_enabled)
                               VALUES (%s, %s, %s, %s, %s, TRUE, %s)""",
                            (dubai_id, p_name, p_rate, p_is_base, p_order, p_tracking),
                        )

            # F-06: Add missing indexes for commonly filtered columns
            cur.execute("CREATE INDEX IF NOT EXISTS cases_task_type ON cases(task_type)")
            cur.execute("CREATE INDEX IF NOT EXISTS cases_submitted_by ON cases(submitted_by)")
            cur.execute("CREATE INDEX IF NOT EXISTS cases_review_status ON cases(review_status)")
            cur.execute("CREATE INDEX IF NOT EXISTS notifications_user_read ON notifications(user_id, read)")
            cur.execute("CREATE INDEX IF NOT EXISTS work_items_project_status ON work_items(project_id, status)")
            cur.execute("CREATE INDEX IF NOT EXISTS arrears_cases_upload_id ON arrears_cases(upload_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_cases_review_lookup ON cases(review_status, task_type, created_at DESC)")
            cur.execute("ALTER TABLE cases ADD COLUMN IF NOT EXISTS result_override JSONB")

        conn.commit()
    finally:
        conn.close()


def init_admin():
    username = os.environ.get("ADMIN_USERNAME", "admin")
    password = os.environ.get("ADMIN_PASSWORD")
    if not password:
        return
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM users")
            if cur.fetchone()[0] == 0:
                cur.execute(
                    "INSERT INTO users (username, password_hash, role) VALUES (%s, %s, 'admin')",
                    (username, generate_password_hash(password)),
                )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.exception("Admin init failed")


def init_user_passwords():
    """One-time password migrations and display name seeds for named users."""
    password_updates = [
        ("markm", "UbQ!4S8B4UGTkGn8"),
    ]
    display_name_seeds = [
        ("elliottg", "Elliott"),
    ]
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            for username, password in password_updates:
                cur.execute("SELECT password_hash FROM users WHERE username = %s", (username,))
                row = cur.fetchone()
                if row and not check_password_hash(row["password_hash"], password):
                    cur.execute(
                        "UPDATE users SET password_hash = %s WHERE username = %s",
                        (generate_password_hash(password), username),
                    )
            for username, display_name in display_name_seeds:
                cur.execute(
                    "UPDATE users SET display_name = %s WHERE username = %s AND (display_name IS NULL OR display_name = '')",
                    (display_name, username),
                )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.exception("User init failed")


if os.environ.get("DATABASE_URL"):
    try:
        init_db()
        init_admin()
        init_user_passwords()
    except Exception as e:
        logger.exception("DB init failed")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _detect_image_mime(data: bytes) -> str | None:
    """Detect image MIME type from magic bytes (replaces removed imghdr stdlib module)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    # HEIC/HEIF: ftyp box at offset 4 with brand heic, heis, hevc, mif1, or msf1
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heis", b"hevc", b"mif1", b"msf1"):
        return "image/heic"
    return None


def encode_file(file):
    media_type = file.content_type or "image/jpeg"
    if media_type not in ALLOWED_TYPES:
        raise ValueError(f"Unsupported file type '{media_type}' for '{file.filename}'.")
    data = file.read()
    # F-10: Validate magic bytes match declared MIME type
    detected_mime = _detect_image_mime(data)
    if detected_mime and detected_mime != media_type:
        raise ValueError(
            f"File '{file.filename}' content does not match declared type '{media_type}' (detected: '{detected_mime}')."
        )
    return base64.standard_b64encode(data).decode("utf-8"), media_type


def variation_file_to_block(file):
    """Return an Anthropic content block for a variation upload (images + PDF + office docs)."""
    media_type = file.content_type or "application/octet-stream"
    # Normalise content-type sniffing gaps (browser sometimes sends empty string)
    if not media_type or media_type == "application/octet-stream":
        ext = (file.filename or "").rsplit(".", 1)[-1].lower()
        media_type = {
            "pdf": "application/pdf",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "xls": "application/vnd.ms-excel",
            "csv": "text/csv",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "doc": "application/msword",
        }.get(ext, media_type)
    if media_type not in VARIATION_ALLOWED_TYPES:
        raise ValueError(f"Unsupported file type '{media_type}' for '{file.filename}'.")
    raw = file.read()
    # Magic-byte validation for image types — same defence as encode_file()
    if media_type.startswith("image/"):
        detected = _detect_image_mime(raw)
        if detected and detected != media_type:
            raise ValueError(
                f"File '{file.filename}' content does not match declared type '{media_type}' (detected: '{detected}')."
            )
    data = base64.standard_b64encode(raw).decode("utf-8")
    if media_type == "application/pdf":
        return {"type": "document", "source": {"type": "base64", "media_type": media_type, "data": data}}
    if media_type.startswith("image/"):
        return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}
    # Excel / CSV / Word — Claude cannot read binary office formats directly;
    # include a placeholder so the model knows the file was present.
    return {"type": "text", "text": f"[Attached file: {file.filename} ({media_type})]"}


def extract_cashier_instruction(text):
    # Try JSON first (termination results)
    stripped = (text or "").strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
            if isinstance(data, dict):
                if data.get("final_cashier_instruction"):
                    return data["final_cashier_instruction"]
                if data.get("copy_line"):
                    return data["copy_line"]
                if data.get("status") == "STOP":
                    return f"STOP: {data.get('reason', 'Missing data')}"
        except (json.JSONDecodeError, TypeError):
            pass
    # Completions markdown logic
    lower = (text or "").lower()
    for marker in ["final cashier instruction", "🔒 final cashier"]:
        idx = lower.find(marker)
        if idx != -1:
            after_heading = text.find("\n", idx)
            if after_heading == -1:
                return text[idx:].strip()
            content_start = after_heading
            while content_start < len(text) and text[content_start] in "\n\r ":
                content_start += 1
            end = len(text)
            for stop in ["section 4", "risks / flags", "risks/flags"]:
                si = lower.find(stop, content_start)
                if si != -1 and si < end:
                    end = si
            return text[content_start:end].strip()
    return ""


# ---------------------------------------------------------------------------
# Email-prompt middleware
# ---------------------------------------------------------------------------
_EMAIL_EXEMPT = {"/login", "/logout", "/set-email", "/forgot-password", "/reset-password"}


@app.before_request
def require_email():
    """Redirect logged-in users who have no email on file to the set-email page."""
    if not current_user.is_authenticated:
        return
    if request.path.startswith("/static/"):
        return
    if request.path.startswith("/api/"):
        return
    if request.path in _EMAIL_EXEMPT:
        return
    if not current_user.email:
        return redirect(url_for("set_email"))


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def login_page():
    if current_user.is_authenticated:
        return redirect(url_for("home"))
    if request.method == "POST":
        data = request.form
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        try:
            conn = get_db_conn()
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, username, password_hash, role, display_name FROM users WHERE username = %s AND active = TRUE",
                    (username,),
                )
                row = cur.fetchone()
            conn.close()
            if row and check_password_hash(row["password_hash"], password):
                login_user(User(row["id"], row["username"], row["role"], row.get("display_name")), remember=True)
                return redirect(url_for("home"))
            return render_template("login.html", error="Invalid username or password")
        except Exception as e:
            return render_template("login.html", error="Login failed")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login_page"))


@app.route("/set-email", methods=["GET", "POST"])
@login_required
def set_email():
    error = None
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        if not email or "@" not in email:
            error = "Please enter a valid email address."
        else:
            try:
                conn = get_db_conn()
                with conn.cursor() as cur:
                    cur.execute("UPDATE users SET email = %s WHERE id = %s", (email, int(current_user.id)))
                conn.commit()
                conn.close()
                # Refresh user in session by reloading
                current_user.email = email
                next_url = request.args.get("next") or url_for("home")
                return redirect(next_url)
            except psycopg2.errors.UniqueViolation:
                error = "That email address is already associated with another account."
            except Exception as e:
                error = "Failed to save email. Please try again."
    return render_template("set_email.html", error=error)


@app.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit("5 per hour", methods=["POST"])
def forgot_password():
    submitted = False
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        submitted = True
        if email and "@" in email and os.environ.get("DATABASE_URL"):
            try:
                conn = get_db_conn()
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("SELECT id, email FROM users WHERE email = %s AND active = TRUE", (email,))
                    row = cur.fetchone()
                if row:
                    raw_token = secrets.token_urlsafe(32)
                    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
                    import datetime
                    expires = datetime.datetime.utcnow() + datetime.timedelta(hours=1)
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE users SET password_reset_token = %s, password_reset_expires = %s WHERE id = %s",
                            (token_hash, expires, row["id"]),
                        )
                    conn.commit()
                    reset_url = f"{APP_URL}/reset-password?token={raw_token}"
                    from mailer import send_password_reset
                    send_password_reset(row["email"], reset_url)
                conn.close()
            except Exception:
                pass  # Never reveal failures
    return render_template("forgot_password.html", submitted=submitted)


@app.route("/reset-password", methods=["GET", "POST"])
def reset_password():
    import datetime
    error = None
    token_raw = request.args.get("token") or request.form.get("token") or ""

    if request.method == "POST":
        new_password = request.form.get("new_password") or ""
        confirm_password = request.form.get("confirm_password") or ""
        if new_password != confirm_password:
            error = "Passwords do not match."
        elif len(new_password) < 8:
            error = "Password must be at least 8 characters."
        else:
            if token_raw and os.environ.get("DATABASE_URL"):
                token_hash = hashlib.sha256(token_raw.encode()).hexdigest()
                try:
                    conn = get_db_conn()
                    with conn.cursor(cursor_factory=RealDictCursor) as cur:
                        cur.execute(
                            "SELECT id, password_reset_expires FROM users WHERE password_reset_token = %s",
                            (token_hash,),
                        )
                        row = cur.fetchone()
                    if row and row["password_reset_expires"] and row["password_reset_expires"] > datetime.datetime.utcnow():
                        with conn.cursor() as cur:
                            cur.execute(
                                "UPDATE users SET password_hash = %s, password_reset_token = NULL, password_reset_expires = NULL WHERE id = %s",
                                (generate_password_hash(new_password), row["id"]),
                            )
                        conn.commit()
                        conn.close()
                        flash("Password reset successfully. Please sign in.", "success")
                        return redirect(url_for("login_page"))
                    else:
                        conn.close()
                        error = "This link is invalid or has expired."
                except Exception:
                    error = "An error occurred. Please try again."
            else:
                error = "Invalid token."
        return render_template("reset_password.html", token=token_raw, error=error)

    # GET — validate token and show form
    if token_raw and os.environ.get("DATABASE_URL"):
        token_hash = hashlib.sha256(token_raw.encode()).hexdigest()
        try:
            conn = get_db_conn()
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, password_reset_expires FROM users WHERE password_reset_token = %s",
                    (token_hash,),
                )
                row = cur.fetchone()
            conn.close()
            if not row or not row["password_reset_expires"] or row["password_reset_expires"] <= datetime.datetime.utcnow():
                return render_template("reset_password.html", token=None, error="This link is invalid or has expired.")
        except Exception:
            return render_template("reset_password.html", token=None, error="An error occurred.")
    else:
        return render_template("reset_password.html", token=None, error="No reset token provided.")
    return render_template("reset_password.html", token=token_raw, error=None)


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------
@app.route("/")
@login_required
def home():
    return render_template("home.html")


@app.route("/review")
@review_required
def review_page():
    return render_template("review.html")


@app.route("/completions")
@login_required
def completions():
    if not user_can_see(current_user, "completion"):
        abort(404)
    return render_template("completions.html")


@app.route("/admin/users")
@login_required
def admin_users_page():
    if current_user.role != "admin":
        return redirect(url_for("home"))
    return render_template("admin_users.html")


@app.route("/admin/corrections")
@login_required
def admin_corrections_page():
    if current_user.role != "admin":
        return redirect(url_for("home"))
    return render_template("admin_corrections.html")


@app.route("/admin/projects")
@login_required
def admin_projects_page():
    if current_user.role != "admin":
        return redirect(url_for("home"))
    return render_template("admin_projects.html")


@app.route("/arrears")
@login_required
def arrears():
    if not user_can_see(current_user, "arrears"):
        abort(404)
    return render_template("arrears.html")


@app.route("/annuals")
@login_required
def annuals():
    if not user_can_see(current_user, "annual"):
        abort(404)
    return render_template("coming_soon.html", task_type="Annual Reviews")


@app.route("/terminations")
@login_required
def terminations():
    if not user_can_see(current_user, "termination"):
        abort(404)
    return render_template("terminations.html")


@app.route("/variations")
@login_required
def variations():
    if not user_can_see(current_user, "variation"):
        abort(404)
    return render_template("variations.html")


# ---------------------------------------------------------------------------
# Termination Analyze
# ---------------------------------------------------------------------------
@app.route("/analyze-termination", methods=["POST"])
@login_required
def analyze_termination():
    if current_user.role not in ("uploader", "admin"):
        return jsonify({"error": "Forbidden"}), 403
    if not user_can_see(current_user, "termination"):
        return jsonify({"error": "Forbidden"}), 403

    case_number = request.form.get("case_number", "").strip()
    project_id_raw = request.form.get("project_id", "").strip()
    project_id = int(project_id_raw) if project_id_raw.isdigit() else None
    work_item_id_raw = request.form.get("work_item_id", "").strip()
    work_item_id = int(work_item_id_raw) if work_item_id_raw.isdigit() else None
    submitted_by = int(current_user.id)

    eos_state = request.form.get("eos_state", "NON_VMOC").strip().upper()
    if eos_state not in ("NON_VMOC", "VMOC_AGREED", "VMOC_UNAGREED"):
        eos_state = "NON_VMOC"

    modifications_text = request.form.get("modifications_text", "").strip()

    content = []
    any_document = False
    stored_images = {}

    for field_name, label in TERMINATION_DOCUMENT_SLOTS:
        # Modifications may arrive as pasted text instead of image files
        if field_name == "modifications" and modifications_text:
            any_document = True
            content.append({"type": "text", "text": "--- Modifications (pasted text) ---"})
            content.append({"type": "text", "text": f"Modifications text:\n{modifications_text}"})
            continue

        files = request.files.getlist(field_name)
        pages = [f for f in files if f and f.filename]
        if not pages:
            continue
        any_document = True
        # Suffix EOS label with state so the prompt knows which mode applies
        if field_name == "eos" and eos_state != "NON_VMOC":
            doc_label = f"{label} [{eos_state}]"
        else:
            doc_label = label
        content.append({"type": "text", "text": f"--- {doc_label} ({len(pages)} page(s)) ---"})
        slot_imgs = []
        for page in pages:
            mime = (page.content_type or "").lower().split(";")[0].strip()
            if field_name == "rp" and mime == "application/pdf":
                pdf_data = base64.b64encode(page.read()).decode()
                content.append({
                    "type": "document",
                    "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_data},
                })
            else:
                try:
                    image_data, media_type = encode_file(page)
                except ValueError as e:
                    return jsonify({"error": str(e)}), 400
                content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_data}})
                slot_imgs.append({"name": page.filename, "data": f"data:{media_type};base64,{image_data}"})
        if slot_imgs:
            stored_images[field_name] = slot_imgs

    # Attach VMOC Modifications as the fifth document when state is VMOC_UNAGREED
    if eos_state == "VMOC_UNAGREED":
        vmoc_pages = [f for f in request.files.getlist("vmoc_modifications") if f and f.filename]
        if not vmoc_pages:
            return jsonify({"error": "VMOC Modifications document required when EOS state is VMOC_UNAGREED."}), 400
        content.append({"type": "text", "text": f"--- VMOC Modifications ({len(vmoc_pages)} page(s)) ---"})
        for page in vmoc_pages:
            try:
                image_data, media_type = encode_file(page)
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_data}})

    # Mandatory field validation — all four slots required
    _term_mandatory = {
        "rp": "Receipts & Payments",
        "contribution_schedule": "Contribution Schedule",
        "modifications": "Modifications",
        "eos": "EOS",
    }
    _term_missing = []
    for _slot, _label in _term_mandatory.items():
        has_files = bool([f for f in request.files.getlist(_slot) if f and f.filename])
        has_text = (_slot == "modifications" and bool(modifications_text))
        if not has_files and not has_text:
            _term_missing.append(_label)
    if _term_missing:
        return jsonify({"error": f"Missing required document(s): {', '.join(_term_missing)}."}), 400

    content.append({"type": "text", "text": f"EOS STATE: {eos_state}\n\nCALCULATE"})

    def generate():
        full_text = []
        try:
            with client.messages.stream(
                model="claude-opus-4-7",
                max_tokens=4096,
                system=[{"type": "text", "text": TERMINATION_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": content}],
            ) as stream:
                for text in stream.text_stream:
                    full_text.append(text)
                    yield f"data: {json.dumps({'text': text})}\n\n"

                msg = stream.get_final_message()
                usage = msg.usage
                case_id = None

                if case_number and os.environ.get("DATABASE_URL"):
                    try:
                        conn = get_db_conn()
                        with conn.cursor() as cur:
                            variation_data_json = json.dumps({"images": stored_images}) if stored_images else None
                            cur.execute(
                                """INSERT INTO cases
                                   (case_number, result, input_tokens, output_tokens,
                                    cache_creation_tokens, cache_read_tokens, submitted_by,
                                    project_id, task_type, variation_data)
                                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'termination', %s) RETURNING id""",
                                (case_number, "".join(full_text), usage.input_tokens, usage.output_tokens,
                                 getattr(usage, "cache_creation_input_tokens", 0),
                                 getattr(usage, "cache_read_input_tokens", 0), submitted_by, project_id,
                                 variation_data_json),
                            )
                            case_id = cur.fetchone()[0]
                            if work_item_id:
                                cur.execute(
                                    "UPDATE work_items SET status='in_progress', assigned_to=%s WHERE id=%s",
                                    (submitted_by, work_item_id),
                                )
                            cur.execute(
                                "SELECT id FROM users WHERE role IN ('reviewer', 'admin') AND active = TRUE AND id != %s",
                                (submitted_by,),
                            )
                            for (uid,) in cur.fetchall():
                                cur.execute(
                                    "INSERT INTO notifications (user_id, case_id, message) VALUES (%s, %s, %s)",
                                    (uid, case_id, f"New termination for review: {case_number}"),
                                )
                        conn.commit()
                        conn.close()
                    except Exception as e:
                        logger.exception("Failed to save termination case")
                        yield f"data: {json.dumps({'error': 'save_failed'})}\n\n"
                        return

                yield f"data: {json.dumps({'done': True, 'case_id': case_id, 'usage': {'input_tokens': usage.input_tokens, 'output_tokens': usage.output_tokens, 'cache_creation_tokens': getattr(usage, 'cache_creation_input_tokens', 0), 'cache_read_tokens': getattr(usage, 'cache_read_input_tokens', 0)}})}\n\n"

        except anthropic.APIError as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


# ---------------------------------------------------------------------------
# Variation Analyze
# ---------------------------------------------------------------------------
@app.route("/analyze-variation", methods=["POST"])
@login_required
def analyze_variation():
    if current_user.role not in ("uploader", "admin"):
        return jsonify({"error": "Forbidden"}), 403
    if not user_can_see(current_user, "variation"):
        return jsonify({"error": "Forbidden"}), 403

    case_number = request.form.get("case_number", "").strip()
    variation_type = request.form.get("variation_type", "full_and_final").strip()
    project_id_raw = request.form.get("project_id", "").strip()
    project_id = int(project_id_raw) if project_id_raw.isdigit() else None
    work_item_id_raw = request.form.get("work_item_id", "").strip()
    work_item_id = int(work_item_id_raw) if work_item_id_raw.isdigit() else None
    submitted_by = int(current_user.id)

    # Dynamic inputs — universal
    custom_variation_type_name = request.form.get("custom_variation_type_name", "").strip() or None
    try:
        creditors_claim = float(request.form.get("creditors_claim_amount", "0") or "0")
    except ValueError:
        creditors_claim = 0.0
    variation_fee_enabled = request.form.get("variation_fee_enabled", "no").lower() == "yes"
    try:
        variation_fee_amount = float(request.form.get("variation_fee_amount", "400") or "400")
    except ValueError:
        variation_fee_amount = 400.0

    # F&F-specific
    try:
        ff_amount = float(request.form.get("ff_amount", "0") or "0")
    except ValueError:
        ff_amount = 0.0

    # Per-type fields
    def _float(key, default=0.0):
        try:
            return float(request.form.get(key, str(default)) or str(default))
        except ValueError:
            return default

    def _int(key, default=0):
        try:
            return int(request.form.get(key, str(default)) or str(default))
        except ValueError:
            return default

    additional_assets_amount = _float("additional_assets_amount")
    additional_assets_label = request.form.get("additional_assets_label", "").strip() or None
    new_contribution_amount = _float("new_contribution_amount")
    remaining_months = _int("remaining_months")
    regular_vc_amount = _float("regular_vc_amount")
    regular_remaining_months = _int("regular_remaining_months")
    extension_months = _int("extension_months")
    extension_vc_amount = _float("extension_vc_amount")
    propose_extension = request.form.get("propose_extension", "no").lower() == "yes"

    # Determine which prompt to use — F&F uses dedicated prompt, all others use generic
    is_ff = (variation_type == "full_and_final")
    eos_prompt = VARIATION_EOS_SYSTEM_PROMPT if is_ff else VARIATION_EOS_SYSTEM_PROMPT_GENERIC

    inputs = {
        "variation_type": variation_type,
        "custom_variation_type_name": custom_variation_type_name,
        "creditors_claim_amount": creditors_claim,
        "variation_meeting_fee": variation_fee_amount if variation_fee_enabled else 0.0,
        "full_and_final_offer": ff_amount if is_ff else 0.0,
        "additional_assets_amount": additional_assets_amount,
        "additional_assets_label": additional_assets_label,
        "new_contribution_amount": new_contribution_amount,
        "remaining_months": remaining_months,
        "regular_vc_amount": regular_vc_amount,
        "regular_remaining_months": regular_remaining_months,
        "extension_months": extension_months,
        "extension_vc_amount": extension_vc_amount,
        "propose_extension": propose_extension,
        "case_reference": case_number,
    }

    content = []
    any_document = False

    for field_name, label in VARIATION_DOCUMENT_SLOTS:
        files = request.files.getlist(field_name)
        pages = [f for f in files if f and f.filename]
        if not pages:
            continue
        any_document = True
        content.append({"type": "text", "text": f"--- {label} ({len(pages)} file(s)) ---"})
        for page in pages:
            try:
                block = variation_file_to_block(page)
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            content.append(block)

    if not any_document:
        return jsonify({"error": "Please upload at least one document."}), 400

    content.append({
        "type": "text",
        "text": f"Documents attached.\n\nDynamic inputs:\n```json\n{json.dumps(inputs, indent=2)}\n```\n\nGenerate the EOS."
    })

    def generate():
        full_text = []
        max_attempts = 3
        delay = 4
        for attempt in range(max_attempts):
            full_text = []
            try:
                with client.messages.stream(
                    model="claude-opus-4-7",
                    max_tokens=2000,
                    system=[{"type": "text", "text": eos_prompt, "cache_control": {"type": "ephemeral"}}],
                    messages=[{"role": "user", "content": content}],
                ) as stream:
                    for text in stream.text_stream:
                        full_text.append(text)
                        yield f"data: {json.dumps({'text': text})}\n\n"

                    msg = stream.get_final_message()
                    usage = msg.usage
                    case_id = None

                    if case_number and os.environ.get("DATABASE_URL"):
                        try:
                            conn = get_db_conn()
                            with conn.cursor() as cur:
                                cur.execute(
                                    """INSERT INTO cases
                                       (case_number, result, input_tokens, output_tokens,
                                        cache_creation_tokens, cache_read_tokens, submitted_by,
                                        project_id, task_type, variation_subtype, custom_variation_type_name)
                                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'variation', %s, %s) RETURNING id""",
                                    (case_number, "".join(full_text), usage.input_tokens, usage.output_tokens,
                                     getattr(usage, "cache_creation_input_tokens", 0),
                                     getattr(usage, "cache_read_input_tokens", 0), submitted_by, project_id,
                                     variation_type, custom_variation_type_name),
                                )
                                case_id = cur.fetchone()[0]
                                if work_item_id:
                                    cur.execute(
                                        "UPDATE work_items SET status='in_progress', assigned_to=%s WHERE id=%s",
                                        (submitted_by, work_item_id),
                                    )
                                cur.execute(
                                    "SELECT id FROM users WHERE role IN ('reviewer', 'admin') AND active = TRUE AND id != %s",
                                    (submitted_by,),
                                )
                                for (uid,) in cur.fetchall():
                                    cur.execute(
                                        "INSERT INTO notifications (user_id, case_id, message) VALUES (%s, %s, %s)",
                                        (uid, case_id, f"New variation for review: {case_number}"),
                                    )
                            conn.commit()
                            conn.close()
                        except Exception as e:
                            logger.exception("Failed to save variation case")
                            yield f"data: {json.dumps({'error': 'save_failed'})}\n\n"
                            return

                    yield f"data: {json.dumps({'done': True, 'case_id': case_id, 'usage': {'input_tokens': usage.input_tokens, 'output_tokens': usage.output_tokens, 'cache_creation_tokens': getattr(usage, 'cache_creation_input_tokens', 0), 'cache_read_tokens': getattr(usage, 'cache_read_input_tokens', 0)}})}\n\n"
                    return

            except Exception as e:
                if is_overloaded(e) and attempt < max_attempts - 1 and not full_text:
                    yield f"data: {json.dumps({'status': f'API busy, retrying in {delay}s… (attempt {attempt + 2}/{max_attempts})'})}\n\n"
                    time.sleep(delay)
                    delay *= 2
                    continue
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
                return

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


# ---------------------------------------------------------------------------
# Variation Reason Tidy
# ---------------------------------------------------------------------------
VARIATION_REASON_SYSTEM_PROMPT = """\
You are a professional insolvency practitioner's assistant. Your task is to tidy and professionally structure a reason statement for an IVA variation.

Rules:
- Keep every fact and figure exactly as stated — do not change any numbers
- Make the language concise, professional, and suitable for official IVA documentation
- Use clear paragraph breaks where appropriate
- Do not add any preamble, commentary, or closing remarks — return only the structured statement text
- If the draft mentions a dividend improvement, lead with that outcome
- Write in third person (referring to the debtor's position, not "I" or "we")
- NEVER mention the variation meeting fee, nominee fee, or any fee charged for the variation itself — omit entirely if present in the draft
- Adapt your tone and focus to the variation type provided in the context:
  * changing_ip: Focus on continuity of the arrangement, the reason for the IP change, and confirmation that terms are unaffected
  * funds_paid_to_date: Lead with the funds received and the basis for early closure
  * contributions_reduction: Lead with the financial change and I&E justification for the reduced contribution
  * extension_for_arrears: Lead with the arrears position and the extension terms to recover them
  * extra_payment_breaks: State the breaks requested and the debtor's circumstances justifying them
  * min_dividend_not_complied: Explain why the minimum dividend will not be achieved and the proposed resolution
  * other_modification_not_complied: Name the specific modification, explain why it cannot be complied with, and the proposed resolution
  * increase_in_claims: Identify the creditor, state the claim increase, and explain the source and impact\
"""


@app.route("/tidy-variation-reason", methods=["POST"])
@login_required
def tidy_variation_reason():
    data = request.get_json() or {}
    text = (data.get("text") or "").strip()
    context = data.get("context") or {}

    if not text:
        return jsonify({"error": "No text provided"}), 400

    # Build context block
    ctx_lines = []
    if context.get("variation_type"):
        vtype = context["variation_type"]
        custom_name = context.get("custom_variation_type_name")
        if vtype == "other" and custom_name:
            label = f"Other — {custom_name}"
        else:
            label = VARIATION_TYPE_LABELS.get(vtype, vtype)
        ctx_lines.append(f"Variation Type: {label} ({vtype})")
    if context.get("case_number"):
        ctx_lines.append(f"Case Reference: {context['case_number']}")
    if context.get("ff_amount"):
        ctx_lines.append(f"Full & Final Offer Amount: £{context['ff_amount']}")
    if context.get("creditors_claim"):
        ctx_lines.append(f"Creditors Claim: £{context['creditors_claim']}")
    if context.get("variation_fee_enabled") and context.get("variation_fee_amount"):
        ctx_lines.append(f"Variation Meeting Fee: £{context['variation_fee_amount']}")
    if context.get("current_dividend") is not None:
        ctx_lines.append(f"Current Estimated Dividend: {context['current_dividend']}p/£")
    if context.get("agreed_dividend") is not None:
        ctx_lines.append(f"Originally Agreed Dividend: {context['agreed_dividend']}p/£")
    if context.get("afd_current") is not None:
        ctx_lines.append(f"Available for Distribution (Current): £{context['afd_current']}")
    if context.get("outcome_uplift") is not None:
        ctx_lines.append(f"Dividend Uplift: {context['outcome_uplift']}p/£")
    if context.get("recommendation"):
        ctx_lines.append(f"EOS Recommendation: {context['recommendation']}")

    ctx_block = "\n".join(ctx_lines)
    user_message = f"Case context:\n{ctx_block}\n\nDraft reason:\n{text}"

    def generate():
        max_attempts = 3
        delay = 4
        for attempt in range(max_attempts):
            yielded = []
            try:
                with client.messages.stream(
                    model="claude-opus-4-7",
                    max_tokens=800,
                    system=[{"type": "text", "text": VARIATION_REASON_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                    messages=[{"role": "user", "content": user_message}],
                ) as stream:
                    for chunk in stream.text_stream:
                        yielded.append(chunk)
                        yield f"data: {json.dumps({'text': chunk})}\n\n"
                    yield f"data: {json.dumps({'done': True})}\n\n"
                    return
            except Exception as e:
                if is_overloaded(e) and attempt < max_attempts - 1 and not yielded:
                    yield f"data: {json.dumps({'status': f'API busy, retrying in {delay}s… (attempt {attempt + 2}/{max_attempts})'})}\n\n"
                    time.sleep(delay)
                    delay *= 2
                    continue
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
                return

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


# ---------------------------------------------------------------------------
# I&E Review (SFS mapping)
# ---------------------------------------------------------------------------
IE_SYSTEM_PROMPT = """\
You are an expert UK debt adviser assistant trained to convert informal Income & Expenditure (I&E) statements into the Standard Financial Statement (SFS) format used by the Money Adviser Network / Money and Pensions Service.

You will receive an image of an Income & Expenditure statement and a household composition input. Your task is to:
1. Extract every line item with its value
2. Map each item to the correct SFS category and subcategory
3. Calculate household-specific SFS trigger thresholds for each relevant category using the provided household composition and the 2026/27 SFS guidelines below
4. Compare the debtor's stated figure for each category against its computed trigger
5. Add "sfs_flag": true, "sfs_trigger": <amount>, "sfs_note": "<explanation>" to any category or line that exceeds its trigger
6. Return a single, valid JSON object matching the schema below
7. Use null for any SFS field where the source document does not provide information

## HOUSEHOLD COMPOSITION INPUT SCHEMA

The user message will supply four integer values:
- adults (≥ 1)
- children_0_to_16 (≥ 0)
- children_16_to_18 (≥ 0)
- vehicles (≥ 0)

Use these values — do not infer household size from the document itself. If the values are absent, assume 1 adult, 0 children, 0 vehicles and set "household_assumed": true.

## SFS CATEGORY STRUCTURE

Map every expenditure line into exactly ONE of these SFS categories and subcategories:

**HOME & CONTENTS** (no trigger figure — fixed costs)
- Rent
- Mortgage
- Ground rent / service charges
- Council tax
- Buildings & contents insurance
- Appliance / furniture rental
- TV licence
- Other home & contents

**UTILITIES** (no trigger figure — fixed costs)
- Gas
- Electricity
- Other fuel (oil, solid fuel)
- Water

**COMMUNICATIONS & LEISURE** ⚠️ FLEXIBLE — TRIGGER FIGURE APPLIES
- Home phone, internet, TV package (includes streaming/film subscriptions)
- Mobile phone
- Hobbies, leisure, sport
- Gifts (birthdays, religious festivals)
- Pocket money
- Newspapers, magazines

**FOOD & HOUSEKEEPING** ⚠️ FLEXIBLE — TRIGGER FIGURE APPLIES
- Groceries (food, non-alcoholic drinks, cleaning products, pet food)
- Nappies & baby items
- School meals & meals at work
- Laundry & dry cleaning
- Alcohol
- Smoking products
- Vet bills

**PERSONAL COSTS** ⚠️ FLEXIBLE — TRIGGER FIGURE APPLIES
- Clothing & footwear
- Hairdressing
- Toiletries (personal use)
- Prescriptions, medicines, dentistry, optical
- Other personal costs

**TRAVEL** (no trigger figure — varies widely)
- Public transport
- Hire purchase or conditional sale (vehicle)
- Car insurance
- Road tax
- MOT & ongoing maintenance
- Breakdown cover
- Fuel, parking, tolls
- Other travel

**CHILDCARE & EDUCATION** (no trigger figure)
- Childcare
- Child maintenance paid out
- School trips, uniform, lessons
- Adult education / training

**INSURANCES & PENSIONS** (no trigger figure)
- Life assurance
- Pension contributions
- Other insurances
- Professional fees / union subscriptions

**OTHER** (no trigger figure)
- Court fines
- Any other essential expenditure

## INCOME CATEGORIES
- Salary / wages (take home)
- Self-employed income
- Benefits & tax credits
- Child Benefit (record separately if identifiable)
- Pensions
- Maintenance received
- Other income

## SFS TRIGGER THRESHOLDS — MONTHLY (2026/27)

Use the household composition values provided in the user message to compute the trigger for each category below. Compare the debtor's stated expenditure against the trigger. If it exceeds the trigger, set sfs_flag: true on that category with the computed trigger and a plain-English note.

### Food & Housekeeping
- Base (1 adult, 0 children): £519/month
- 2 adults, 0 children: £738/month
- Per additional child aged 0–16: +£134/month each
- Per additional child aged 16–18: +£155/month each
- Formula: 519 + (additional_adults × 219) + (children_0_to_16 × 134) + (children_16_to_18 × 155)
  where additional_adults = max(0, adults - 1)

### Travel (total across all travel subcategories)
- 0 vehicles: trigger = £0 — flag any travel expenditure > £0 as requiring justification
- 1 vehicle: trigger = £220/month
- 2+ vehicles: trigger = £220/month × vehicles
- Formula: vehicles × 220

### Personal Costs (clothing, hairdressing, toiletries, prescriptions, other)
- Per adult: £165/month
- Formula: adults × 165

### Clothing & Footwear (subcategory of Personal Costs — use independently if itemised separately)
- 1 adult, 0 children: £76/month
- Per additional adult: +£22/month
- Per child 0–16: +£25/month each
- Per child 16–18: +£29/month each
- Formula: 76 + (additional_adults × 22) + (children_0_to_16 × 25) + (children_16_to_18 × 29)

### Communications & Leisure
- Phone/internet total: £117/month (fixed — does not scale with household)
- Leisure: £56/month per adult
- Formula: 117 + (adults × 56)

### Childcare
- Only applicable if children present. Flag if > £1,100/month per child under 5, or > £600/month per school-age child.
- If no children: trigger = £0 — flag any childcare expenditure.

### Pet Costs (within Food & Housekeeping > Vet bills, or as a separate line)
- If any pet costs claimed: flag if > £75/month.

**If household composition is unknown:** assume 1 adult, 0 children, 0 vehicles, set "household_assumed": true, and add to "missing_information".

## CHILD BENEFIT RATES 2026/27 (for reference / income reconciliation only)

Weekly rates: £27.05 eldest/only child, £17.90 each additional child.
Do NOT auto-populate Child Benefit if the source is silent — only note it as missing in "missing_information". If the source states a Child Benefit figure that materially differs from the expected rate, flag it in "mapping_notes".

## MAPPING RULES
- "Hire Purchase or conditional sale vehicle" → Travel > Hire purchase
- "Prescriptions and medicines" → Personal costs > Prescriptions, medicines, dentistry, optical
- "Home phone, internet, TV package (including film subscriptions)" → Communications & leisure > Home phone, internet, TV package
- "Hobbies, leisure or sport" → Communications & leisure > Hobbies, leisure, sport
- "Groceries (food, pet food, non-alcoholic drinks, cleaning)" → Food & housekeeping > Groceries
- "Toiletries" listed separately → Personal costs > Toiletries
- Life assurance / pensions / other insurances → Insurances & Pensions (not Personal Costs)
- If a line item could fit multiple categories, choose the most specific SFS subcategory and record reasoning in "mapping_notes"
- If a line item does not clearly fit any SFS category, place it in "OTHER > Any other essential expenditure" and flag in "mapping_notes"

## OUTPUT SCHEMA

Return ONLY a valid JSON object. No preamble, no markdown fences, no commentary outside the JSON.

{
  "sfs_version": "2026/27",
  "client": {
    "name": null,
    "household_size_adults": null,
    "household_size_children_0_to_16": null,
    "household_size_children_16_to_18": null,
    "household_size_vehicles": null,
    "household_assumed": false
  },
  "income": {
    "salary_wages": null,
    "self_employed": null,
    "benefits_tax_credits": null,
    "child_benefit": null,
    "pensions": null,
    "maintenance_received": null,
    "other_income": null,
    "total_income": 0.00
  },
  "expenditure": {
    "home_and_contents": {
      "rent": null,
      "mortgage": null,
      "ground_rent_service_charges": null,
      "council_tax": null,
      "buildings_contents_insurance": null,
      "appliance_furniture_rental": null,
      "tv_licence": null,
      "other": null,
      "subtotal": 0.00
    },
    "utilities": {
      "gas": null,
      "electricity": null,
      "other_fuel": null,
      "water": null,
      "subtotal": 0.00
    },
    "communications_and_leisure": {
      "home_phone_internet_tv": null,
      "mobile_phone": null,
      "hobbies_leisure_sport": null,
      "gifts": null,
      "pocket_money": null,
      "newspapers_magazines": null,
      "subtotal": 0.00,
      "trigger_figure": 0.00,
      "trigger_calculation": "276 (1st adult) = 276",
      "over_trigger": false,
      "variance_from_trigger": 0.00,
      "sfs_flag": false,
      "sfs_trigger": null,
      "sfs_note": null
    },
    "food_and_housekeeping": {
      "groceries": null,
      "nappies_baby_items": null,
      "school_meals_work_meals": null,
      "laundry_dry_cleaning": null,
      "alcohol": null,
      "smoking": null,
      "vet_bills": null,
      "subtotal": 0.00,
      "trigger_figure": 0.00,
      "trigger_calculation": "string",
      "over_trigger": false,
      "variance_from_trigger": 0.00,
      "sfs_flag": false,
      "sfs_trigger": null,
      "sfs_note": null
    },
    "personal_costs": {
      "clothing_footwear": null,
      "hairdressing": null,
      "toiletries": null,
      "prescriptions_medicines_dental_optical": null,
      "other": null,
      "subtotal": 0.00,
      "trigger_figure": 0.00,
      "trigger_calculation": "string",
      "over_trigger": false,
      "variance_from_trigger": 0.00,
      "sfs_flag": false,
      "sfs_trigger": null,
      "sfs_note": null
    },
    "travel": {
      "public_transport": null,
      "hire_purchase_vehicle": null,
      "car_insurance": null,
      "road_tax": null,
      "mot_maintenance": null,
      "breakdown_cover": null,
      "fuel_parking_tolls": null,
      "other": null,
      "subtotal": 0.00,
      "trigger_figure": 0.00,
      "trigger_calculation": "string",
      "over_trigger": false,
      "variance_from_trigger": 0.00,
      "sfs_flag": false,
      "sfs_trigger": null,
      "sfs_note": null
    },
    "childcare_and_education": {
      "childcare": null,
      "child_maintenance_paid": null,
      "school_trips_uniform_lessons": null,
      "adult_education": null,
      "subtotal": 0.00
    },
    "insurances_and_pensions": {
      "life_assurance": null,
      "pension_contributions": null,
      "other_insurances": null,
      "professional_fees_unions": null,
      "subtotal": 0.00
    },
    "other": {
      "court_fines": null,
      "other_essential": null,
      "subtotal": 0.00
    },
    "total_expenditure": 0.00
  },
  "debts": {
    "priority_arrears": null,
    "non_priority_debts": null,
    "total_debt_payments": null,
    "note": "string"
  },
  "summary": {
    "total_income": 0.00,
    "total_expenditure": 0.00,
    "total_debt_payments": 0.00,
    "surplus_or_deficit": 0.00,
    "status": "surplus | deficit | balanced",
    "combined_flexible_spend": 0.00,
    "combined_flexible_trigger": 0.00,
    "combined_flexible_variance": 0.00
  },
  "trigger_analysis": {
    "household_used_for_calculation": "N adult(s), N child(ren) 0-16, N child(ren) 16-18, N vehicle(s)",
    "household_assumed": false,
    "categories_over_trigger": [],
    "categories_within_trigger": [],
    "notes": "string"
  },
  "mapping_notes": [
    {
      "source_item": "string",
      "mapped_to": "category > subcategory",
      "value": 0.00,
      "note": "string"
    }
  ],
  "missing_information": []
}

## RULES
- All monetary values are GBP, expressed as numbers with 2 decimal places (e.g. 300.00)
- Use null (not 0) for fields the source document does not mention
- Use 0.00 only when the source explicitly states zero, or for calculated subtotals/totals
- Calculate every subtotal, total, trigger figure, and variance yourself
- variance_from_trigger = subtotal - trigger_figure (positive = over, negative = under)
- over_trigger = true only if subtotal > trigger_figure
- sfs_flag = true only if the debtor's stated figure for that category exceeds the computed SFS trigger; otherwise sfs_flag = false
- sfs_trigger = the computed monthly SFS threshold for that category given the supplied household composition
- sfs_note = a plain-English explanation, e.g. "Exceeds SFS trigger for 2 adults, 1 child 0-16 (£738/month)"
- Lines within or at the trigger must have sfs_flag = false and sfs_note = null
- combined_flexible_spend = sum of the three flexible category subtotals
- combined_flexible_trigger = sum of the three flexible category trigger figures
- If your calculated total differs from the source's stated total, use your calculated total and note in "mapping_notes"
- Output must be valid, parseable JSON — no trailing commas, no comments, no markdown fences
- Do not include any text before or after the JSON object\
"""


@app.route("/analyze-ie", methods=["POST"])
@login_required
def analyze_ie():
    if current_user.role not in ("uploader", "admin"):
        return jsonify({"error": "Forbidden"}), 403

    try:
        adults = int(request.form.get("adults", "1") or "1")
        if adults < 1:
            adults = 1
    except ValueError:
        adults = 1
    try:
        children_0_to_16 = int(request.form.get("children_0_to_16", "0") or "0")
        if children_0_to_16 < 0:
            children_0_to_16 = 0
    except ValueError:
        children_0_to_16 = 0
    try:
        children_16_to_18 = int(request.form.get("children_16_to_18", "0") or "0")
        if children_16_to_18 < 0:
            children_16_to_18 = 0
    except ValueError:
        children_16_to_18 = 0
    try:
        vehicles = int(request.form.get("vehicles", "0") or "0")
        if vehicles < 0:
            vehicles = 0
    except ValueError:
        vehicles = 0

    files = request.files.getlist("ie_document")
    pages = [f for f in files if f and f.filename]
    if not pages:
        return jsonify({"error": "Please upload the I&E document."}), 400

    content = []
    for page in pages:
        try:
            block = variation_file_to_block(page)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        content.append(block)

    content.append({
        "type": "text",
        "text": (
            f"Income & Expenditure statement attached.\n\n"
            f"Household composition:\n"
            f"- adults: {adults}\n"
            f"- children_0_to_16: {children_0_to_16}\n"
            f"- children_16_to_18: {children_16_to_18}\n"
            f"- vehicles: {vehicles}\n\n"
            f"Use these values to compute the SFS trigger thresholds for each relevant category, "
            f"compare the debtor's stated figures against those triggers, and add sfs_flag / sfs_trigger / sfs_note "
            f"to any line that exceeds its trigger. Extract all line items, map to SFS categories, and return the JSON."
        )
    })

    def generate():
        max_attempts = 3
        delay = 4
        for attempt in range(max_attempts):
            full_text = []
            try:
                with client.messages.stream(
                    model="claude-opus-4-7",
                    max_tokens=4000,
                    system=[{"type": "text", "text": IE_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                    messages=[{"role": "user", "content": content}],
                ) as stream:
                    for text in stream.text_stream:
                        full_text.append(text)
                        yield f"data: {json.dumps({'text': text})}\n\n"
                    yield f"data: {json.dumps({'done': True})}\n\n"
                    return
            except Exception as e:
                if is_overloaded(e) and attempt < max_attempts - 1 and not full_text:
                    yield f"data: {json.dumps({'status': f'API busy, retrying in {delay}s… (attempt {attempt + 2}/{max_attempts})'})}\n\n"
                    time.sleep(delay)
                    delay *= 2
                    continue
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
                return

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


# ---------------------------------------------------------------------------
# User management API (admin only)
# ---------------------------------------------------------------------------
@app.route("/api/users")
@login_required
def list_users():
    if current_user.role != "admin":
        return jsonify({"error": "Forbidden"}), 403
    conn = get_db_conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT id, username, role, created_at, active, specialisms, display_name FROM users ORDER BY created_at")
        rows = cur.fetchall()
        users = [{**dict(r), "created_at": r["created_at"].isoformat()} for r in rows]
        # attach project ids
        cur.execute("SELECT user_id, project_id FROM user_projects")
        up_rows = cur.fetchall()
    conn.close()
    project_map = {}
    for r in up_rows:
        project_map.setdefault(r["user_id"], []).append(r["project_id"])
    for u in users:
        u["project_ids"] = project_map.get(u["id"], [])
    return jsonify(users)


@app.route("/api/users", methods=["POST"])
@login_required
def create_user():
    if current_user.role != "admin":
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    role = data.get("role", "uploader")
    display_name = (data.get("display_name") or "").strip() or None
    if not username or not password or role not in ("admin", "reviewer", "uploader", "team_leader"):
        return jsonify({"error": "Invalid input"}), 400
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (username, password_hash, role, display_name) VALUES (%s, %s, %s, %s) RETURNING id",
                (username, generate_password_hash(password), role, display_name),
            )
            user_id = cur.fetchone()[0]
        conn.commit()
        conn.close()
        return jsonify({"id": user_id, "username": username, "role": role})
    except psycopg2.errors.UniqueViolation:
        return jsonify({"error": "Username already exists"}), 409
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/users/<int:user_id>", methods=["PUT"])
@login_required
def update_user(user_id):
    if current_user.role != "admin":
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json()
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            if "role" in data:
                cur.execute("UPDATE users SET role = %s WHERE id = %s", (data["role"], user_id))
            if "active" in data:
                cur.execute("UPDATE users SET active = %s WHERE id = %s", (data["active"], user_id))
            if data.get("password"):
                cur.execute("UPDATE users SET password_hash = %s WHERE id = %s", (generate_password_hash(data["password"]), user_id))
            if "specialisms" in data:
                cur.execute("UPDATE users SET specialisms = %s WHERE id = %s", (data["specialisms"], user_id))
            if "display_name" in data:
                cur.execute("UPDATE users SET display_name = %s WHERE id = %s", (data["display_name"] or None, user_id))
            if "project_ids" in data:
                cur.execute("DELETE FROM user_projects WHERE user_id = %s", (user_id,))
                for pid in (data["project_ids"] or []):
                    cur.execute("INSERT INTO user_projects (user_id, project_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (user_id, pid))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Notification API
# ---------------------------------------------------------------------------
@app.route("/api/notifications")
@login_required
def get_notifications():
    if not os.environ.get("DATABASE_URL"):
        return jsonify([])
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT n.id, n.case_id, n.message, n.read, n.created_at,
                          c.case_number, c.task_type
                   FROM notifications n JOIN cases c ON n.case_id = c.id
                   WHERE n.user_id = %s ORDER BY n.created_at DESC LIMIT 30""",
                (int(current_user.id),),
            )
            rows = cur.fetchall()
        conn.close()
        return jsonify([{**dict(r), "created_at": r["created_at"].isoformat()} for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/notifications/count")
@login_required
def notification_count():
    if not os.environ.get("DATABASE_URL"):
        return jsonify({"count": 0})
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM notifications WHERE user_id = %s AND read = FALSE", (int(current_user.id),))
            count = cur.fetchone()[0]
        conn.close()
        return jsonify({"count": count})
    except Exception:
        return jsonify({"count": 0})


@app.route("/api/notifications/<int:notif_id>/read", methods=["POST"])
@login_required
def mark_notification_read(notif_id):
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute("UPDATE notifications SET read = TRUE WHERE id = %s AND user_id = %s", (notif_id, int(current_user.id)))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/notifications/read-all", methods=["POST"])
@login_required
def mark_all_read():
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute("UPDATE notifications SET read = TRUE WHERE user_id = %s", (int(current_user.id),))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Corrections API (admin)
# ---------------------------------------------------------------------------
@app.route("/api/corrections")
@login_required
def list_corrections():
    if current_user.role != "admin":
        return jsonify({"error": "Forbidden"}), 403
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT c.id, c.case_number, c.created_at, c.result,
                       c.cashier_instruction_override, c.cashier_instruction_reasoning,
                       c.review_status, c.review_note, c.reviewed_at,
                       u.username AS reviewed_by_username
                FROM cases c
                LEFT JOIN users u ON c.reviewed_by = u.id
                WHERE c.cashier_instruction_override IS NOT NULL
                   OR c.review_status IN ('approved', 'rejected')
                ORDER BY COALESCE(c.reviewed_at, c.created_at) DESC
            """)
            rows = cur.fetchall()
        conn.close()
        return jsonify([{
            "id": r["id"],
            "case_number": r["case_number"],
            "created_at": r["created_at"].isoformat(),
            "original": extract_cashier_instruction(r["result"] or ""),
            "edited": r["cashier_instruction_override"],
            "reasoning": r["cashier_instruction_reasoning"] or "",
            "review_status": r["review_status"],
            "review_note": r["review_note"] or "",
            "reviewed_at": r["reviewed_at"].isoformat() if r["reviewed_at"] else None,
            "reviewed_by": r["reviewed_by_username"] or "",
        } for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Projects API
# ---------------------------------------------------------------------------
@app.route("/api/projects")
@login_required
def list_projects():
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, name, slug, active, created_at FROM projects ORDER BY id")
            rows = cur.fetchall()
        conn.close()
        return jsonify([{**dict(r), "created_at": r["created_at"].isoformat()} for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/projects", methods=["POST"])
@login_required
def create_project():
    if current_user.role != "admin":
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json()
    name = (data.get("name") or "").strip()
    slug = (data.get("slug") or name.lower().replace(" ", "-")).strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute("INSERT INTO projects (name, slug) VALUES (%s, %s) RETURNING id", (name, slug))
            pid = cur.fetchone()[0]
        conn.commit()
        conn.close()
        return jsonify({"id": pid, "name": name, "slug": slug})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/projects/<int:project_id>", methods=["PUT"])
@login_required
def update_project(project_id):
    if current_user.role != "admin":
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json()
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            if "name" in data:
                cur.execute("UPDATE projects SET name = %s WHERE id = %s", (data["name"], project_id))
            if "active" in data:
                cur.execute("UPDATE projects SET active = %s WHERE id = %s", (data["active"], project_id))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Work Items API
# ---------------------------------------------------------------------------
@app.route("/api/work-items")
@login_required
def list_work_items():
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if current_user.role == "admin":
                cur.execute("""
                    SELECT w.*, p.name AS project_name, u.username AS assigned_username
                    FROM work_items w
                    JOIN projects p ON w.project_id = p.id
                    LEFT JOIN users u ON w.assigned_to = u.id
                    ORDER BY w.due_date ASC, w.created_at ASC
                """)
            else:
                # get user's specialisms
                cur.execute("SELECT specialisms FROM users WHERE id = %s", (int(current_user.id),))
                row = cur.fetchone()
                specialisms = (row["specialisms"] or "all") if row else "all"
                # get user's project ids
                cur.execute("SELECT project_id FROM user_projects WHERE user_id = %s", (int(current_user.id),))
                proj_ids = [r["project_id"] for r in cur.fetchall()]
                if not proj_ids:
                    conn.close()
                    return jsonify([])
                if specialisms == "all":
                    cur.execute("""
                        SELECT w.*, p.name AS project_name, u.username AS assigned_username
                        FROM work_items w
                        JOIN projects p ON w.project_id = p.id
                        LEFT JOIN users u ON w.assigned_to = u.id
                        WHERE w.project_id = ANY(%s)
                        ORDER BY w.due_date ASC, w.created_at ASC
                    """, (proj_ids,))
                else:
                    spec_list = [s.strip() for s in specialisms.split(",")]
                    cur.execute("""
                        SELECT w.*, p.name AS project_name, u.username AS assigned_username
                        FROM work_items w
                        JOIN projects p ON w.project_id = p.id
                        LEFT JOIN users u ON w.assigned_to = u.id
                        WHERE w.project_id = ANY(%s) AND w.task_type = ANY(%s)
                        ORDER BY w.due_date ASC, w.created_at ASC
                    """, (proj_ids, spec_list))
            rows = cur.fetchall()
        conn.close()
        return jsonify([{**dict(r), "due_date": r["due_date"].isoformat(), "created_at": r["created_at"].isoformat()} for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/work-items", methods=["POST"])
@login_required
def create_work_item():
    if current_user.role not in ("admin", "uploader"):
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json()
    project_id = data.get("project_id")
    task_type = data.get("task_type", "completion")
    case_number = (data.get("case_number") or "").strip()
    due_date = data.get("due_date")
    notes = (data.get("notes") or "").strip() or None
    if not all([project_id, case_number, due_date]):
        return jsonify({"error": "project_id, case_number and due_date required"}), 400
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO work_items (project_id, task_type, case_number, due_date, created_by, notes)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                (project_id, task_type, case_number, due_date, int(current_user.id), notes),
            )
            wid = cur.fetchone()[0]
        conn.commit()
        conn.close()
        return jsonify({"id": wid})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/work-items/upload", methods=["POST"])
@login_required
def upload_work_items():
    if current_user.role not in ("admin", "uploader"):
        return jsonify({"error": "Forbidden"}), 403
    project_id = request.form.get("project_id")
    task_type = request.form.get("task_type", "completion")
    if not project_id:
        return jsonify({"error": "project_id required"}), 400
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "file required"}), 400
    _ct = (f.content_type or "").lower().split(";")[0].strip()
    _fn = (f.filename or "").lower()
    if _ct not in ("text/csv", "application/csv", "text/plain") and not _fn.endswith(".csv"):
        return jsonify({"error": "Only CSV files are accepted."}), 400
    try:
        content = f.read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(content))
        inserted = 0
        errors = []
        conn = get_db_conn()
        with conn.cursor() as cur:
            for i, row in enumerate(reader, 1):
                case_num = (row.get("case_number") or row.get("Case Number") or "").strip()
                due = (row.get("due_date") or row.get("Due Date") or "").strip()
                tt = (row.get("task_type") or row.get("Task Type") or task_type).strip()
                notes = (row.get("notes") or row.get("Notes") or "").strip() or None
                if not case_num or not due:
                    errors.append(f"Row {i}: missing case_number or due_date")
                    continue
                cur.execute(
                    """INSERT INTO work_items (project_id, task_type, case_number, due_date, created_by, notes)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (project_id, tt, case_num, due, int(current_user.id), notes),
                )
                inserted += 1
        conn.commit()
        conn.close()
        return jsonify({"inserted": inserted, "errors": errors})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/work-items/<int:item_id>", methods=["PUT"])
@login_required
def update_work_item(item_id):
    data = request.get_json()
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            if "status" in data:
                new_status = data["status"]
                if new_status == "in_progress":
                    cur.execute(
                        "UPDATE work_items SET status=%s, assigned_to=%s WHERE id=%s",
                        (new_status, int(current_user.id), item_id),
                    )
                else:
                    cur.execute("UPDATE work_items SET status=%s WHERE id=%s", (new_status, item_id))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/work-items/<int:item_id>", methods=["DELETE"])
@login_required
def delete_work_item(item_id):
    if current_user.role != "admin":
        return jsonify({"error": "Forbidden"}), 403
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM work_items WHERE id = %s", (item_id,))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Dashboard Stats API
# ---------------------------------------------------------------------------
@app.route("/api/dashboard/stats")
@login_required
def dashboard_stats():
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if current_user.role == "admin":
                cur.execute("SELECT id FROM projects WHERE active = TRUE")
                proj_ids = [r["id"] for r in cur.fetchall()]
            else:
                cur.execute("SELECT project_id FROM user_projects WHERE user_id = %s", (int(current_user.id),))
                proj_ids = [r["project_id"] for r in cur.fetchall()]

            if not proj_ids:
                conn.close()
                return jsonify([])

            cur.execute("""
                SELECT p.id, p.name,
                    COUNT(w.id) FILTER (WHERE w.status = 'pending') AS pending_total,
                    COUNT(w.id) FILTER (WHERE w.status = 'in_progress') AS in_progress_total,
                    COUNT(w.id) FILTER (WHERE w.status = 'completed') AS completed_total,
                    COUNT(w.id) FILTER (WHERE w.task_type = 'completion' AND w.status = 'pending') AS pending_completions,
                    COUNT(w.id) FILTER (WHERE w.task_type = 'arrears' AND w.status = 'pending') AS pending_arrears,
                    COUNT(w.id) FILTER (WHERE w.task_type = 'annual' AND w.status = 'pending') AS pending_annuals,
                    COUNT(w.id) FILTER (WHERE w.task_type = 'variation' AND w.status = 'pending') AS pending_variations,
                    COUNT(w.id) FILTER (WHERE w.task_type = 'termination' AND w.status = 'pending') AS pending_terminations,
                    MIN(w.due_date) FILTER (WHERE w.status = 'pending') AS next_due
                FROM projects p
                LEFT JOIN work_items w ON w.project_id = p.id
                WHERE p.id = ANY(%s)
                GROUP BY p.id, p.name
                ORDER BY p.id
            """, (proj_ids,))
            rows = cur.fetchall()

            # approved cases per project (lifetime)
            cur.execute("""
                SELECT project_id, COUNT(*) AS approved_count
                FROM cases
                WHERE project_id = ANY(%s) AND review_status = 'approved'
                GROUP BY project_id
            """, (proj_ids,))
            approved_map = {r["project_id"]: r["approved_count"] for r in cur.fetchall()}

        conn.close()
        result = []
        for r in rows:
            d = dict(r)
            d["approved_cases"] = approved_map.get(r["id"], 0)
            if d["next_due"]:
                d["next_due"] = d["next_due"].isoformat()
            result.append(d)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Cases API
# ---------------------------------------------------------------------------
@app.route("/api/cases/pending-review")
@review_required
def api_cases_pending_review():
    """Return lightweight case summaries for the review sidebar.
    Query params: type (completion|variation|termination|all), status (pending|approved|rejected|all), limit (int).
    """
    task_type = request.args.get('type', 'all')
    status    = request.args.get('status', 'pending')
    limit     = min(int(request.args.get('limit', 200)), 1000)

    where = []
    params = []
    if task_type != 'all':
        where.append('c.task_type = %s')
        params.append(task_type)
    if status != 'all':
        where.append('c.review_status = %s')
        params.append(status)

    where_sql = ('WHERE ' + ' AND '.join(where)) if where else ''

    sql = f"""
        SELECT c.id, c.case_number, c.task_type, c.review_status,
               c.created_at, c.reviewed_at,
               u.username AS reviewer_name
        FROM cases c
        LEFT JOIN users u ON u.id = c.reviewed_by
        {where_sql}
        ORDER BY c.created_at DESC
        LIMIT %s
    """
    params.append(limit)

    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        conn.close()
        return jsonify([{
            'id':            r['id'],
            'case_number':   r['case_number'],
            'task_type':     r['task_type'],
            'review_status': r['review_status'],
            'created_at':    r['created_at'].isoformat() if r['created_at'] else None,
            'reviewed_at':   r['reviewed_at'].isoformat() if r['reviewed_at'] else None,
            'reviewer_name': r['reviewer_name'],
        } for r in rows])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route("/api/cases")
@login_required
def list_cases():
    if not os.environ.get("DATABASE_URL"):
        return jsonify([])
    task_type_filter = request.args.get("task_type")
    # Enforce visibility: if a specific task_type is requested, check access
    if task_type_filter and not user_can_see(current_user, task_type_filter):
        return jsonify({"error": "Forbidden"}), 403
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if task_type_filter:
                cur.execute(
                    "SELECT id, case_number, created_at, review_status, variation_subtype, custom_variation_type_name, review_handoff_note FROM cases WHERE task_type = %s ORDER BY created_at DESC LIMIT 100",
                    (task_type_filter,),
                )
            else:
                # Filter to only task types the user can see
                spec = getattr(current_user, "specialisms", "all") or "all"
                if spec == "all":
                    cur.execute("SELECT id, case_number, created_at, review_status, variation_subtype, custom_variation_type_name, review_handoff_note FROM cases ORDER BY created_at DESC LIMIT 100")
                else:
                    visible_types = [s.strip() for s in spec.split(",")]
                    cur.execute(
                        "SELECT id, case_number, created_at, review_status, variation_subtype, custom_variation_type_name, review_handoff_note FROM cases WHERE task_type = ANY(%s) ORDER BY created_at DESC LIMIT 100",
                        (visible_types,),
                    )
            rows = cur.fetchall()
        conn.close()
        return jsonify([{"id": r["id"], "case_number": r["case_number"], "created_at": r["created_at"].isoformat(), "review_status": r["review_status"], "variation_subtype": r.get("variation_subtype"), "custom_variation_type_name": r.get("custom_variation_type_name"), "review_handoff_note": r.get("review_handoff_note")} for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cases/export")
@login_required
def export_cases():
    if current_user.role != "admin":
        return jsonify({"error": "Forbidden"}), 403
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT case_number, created_at, result, cashier_instruction_override, cashier_instruction_reasoning, input_tokens, output_tokens FROM cases ORDER BY created_at DESC")
            rows = cur.fetchall()
        conn.close()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Case Number", "Date", "Cashier Instruction", "Edit Reasoning", "Input Tokens", "Output Tokens"])
        for row in rows:
            cashier = row["cashier_instruction_override"] or extract_cashier_instruction(row["result"] or "")
            writer.writerow([
                row["case_number"],
                row["created_at"].strftime("%d/%m/%Y %H:%M"),
                cashier,
                row["cashier_instruction_reasoning"] or "",
                row["input_tokens"],
                row["output_tokens"],
            ])
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment;filename=worked-cases.csv"},
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cases/<int:case_id>")
@login_required
def get_case(case_id):
    if not os.environ.get("DATABASE_URL"):
        return jsonify({"error": "No database configured"}), 503
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT c.*, u.username AS reviewer_name
                FROM cases c
                LEFT JOIN users u ON u.id = c.reviewed_by
                WHERE c.id = %s
            """, (case_id,))
            row = cur.fetchone()
        conn.close()
        if not row:
            return jsonify({"error": "Not found"}), 404

        raw_result = row["result"] or ""

        # variation_data stores {"images": {...}, "eos": {...}, "ie": {...}, "reason": ..., "saved": {...}}.
        # Images are always inside this JSON bag — there is no separate "images" column.
        raw_vd = row.get("variation_data")
        vd = {}
        if raw_vd:
            try:
                vd = json.loads(raw_vd) if isinstance(raw_vd, str) else (raw_vd or {})
            except (ValueError, TypeError):
                vd = {}
        images = vd.get("images") or {}

        # For variations the Claude output is a JSON string; parse it so the frontend
        # gets an object and getStructuredResult() can work without a second parse.
        parsed_result = None
        stripped = raw_result.strip()
        if stripped.startswith("{"):
            try:
                parsed_result = json.loads(stripped)
            except (ValueError, TypeError):
                pass

        result_override = row.get("result_override")
        if isinstance(result_override, str):
            try:
                result_override = json.loads(result_override)
            except (ValueError, TypeError):
                result_override = None

        return jsonify({
            "id": row["id"], "case_number": row["case_number"],
            "task_type": row.get("task_type"),
            "created_at": row["created_at"].isoformat(),
            "result": parsed_result or raw_result,
            "full_text": raw_result,
            "cashier_instruction": row.get("cashier_instruction_override") or extract_cashier_instruction(raw_result),
            "input_tokens": row["input_tokens"], "output_tokens": row["output_tokens"],
            "cache_creation_tokens": row["cache_creation_tokens"], "cache_read_tokens": row["cache_read_tokens"],
            "cashier_instruction_override": row.get("cashier_instruction_override"),
            "review_status": row.get("review_status"),
            "review_note": row.get("review_note"),
            "reviewed_at": row["reviewed_at"].isoformat() if row.get("reviewed_at") else None,
            "reviewer_name": row.get("reviewer_name"),
            "images": images,
            "result_override": result_override,
            "variation_data": row.get("variation_data"),
            "submitted_by": row.get("submitted_by"),
            "variation_subtype": row.get("variation_subtype"),
            "custom_variation_type_name": row.get("custom_variation_type_name"),
            "review_handoff_note": row.get("review_handoff_note"),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cases/<int:case_id>/variation-save", methods=["PUT"])
@login_required
def variation_save(case_id):
    if current_user.role not in ("uploader", "admin", "reviewer"):
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json() or {}
    section = data.get("section")
    if section not in ("eos", "ie", "reason", "images"):
        return jsonify({"error": "Invalid section"}), 400
    section_data = data.get("data")
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT variation_data FROM cases WHERE id = %s", (case_id,))
            row = cur.fetchone()
            if not row:
                conn.close()
                return jsonify({"error": "Not found"}), 404
            try:
                vd = json.loads(row["variation_data"]) if row["variation_data"] else {}
            except (json.JSONDecodeError, TypeError):
                vd = {}
            vd[section] = section_data
            if section != "images":
                if "saved" not in vd:
                    vd["saved"] = {}
                vd["saved"][section] = True
            cur.execute("UPDATE cases SET variation_data = %s WHERE id = %s", (json.dumps(vd), case_id))
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "saved": vd.get("saved", {})})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cases/<int:case_id>/send-for-review", methods=["POST"])
@login_required
def send_for_review(case_id):
    if current_user.role not in ("uploader", "admin"):
        return jsonify({"error": "Forbidden"}), 403
    submitted_by = int(current_user.id)
    data = request.get_json(silent=True) or {}
    handoff_note = data.get("handoff_note") or None
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, case_number, submitted_by FROM cases WHERE id = %s",
                (case_id,),
            )
            row = cur.fetchone()
            if not row:
                conn.close()
                return jsonify({"error": "Not found"}), 404
            # Verify ownership or admin
            if current_user.role != "admin" and row.get("submitted_by") and row["submitted_by"] != submitted_by:
                conn.close()
                return jsonify({"error": "Forbidden"}), 403
            case_number = row["case_number"]
            cur.execute(
                "UPDATE cases SET review_status = 'under_review', review_handoff_note = %s WHERE id = %s",
                (handoff_note, case_id),
            )
            cur.execute(
                "SELECT id FROM users WHERE role IN ('reviewer', 'admin') AND active = TRUE AND id != %s",
                (submitted_by,),
            )
            for (uid,) in cur.fetchall():
                cur.execute(
                    "INSERT INTO notifications (user_id, case_id, message) VALUES (%s, %s, %s)",
                    (uid, case_id, f"Variation sent for review: {case_number}"),
                )
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cases/<int:case_id>/action", methods=["POST"])
@login_required
def action_case(case_id):
    if current_user.role not in ("uploader", "admin"):
        return jsonify({"error": "Forbidden"}), 403
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute("UPDATE cases SET review_status = 'actioned' WHERE id = %s", (case_id,))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cases/<int:case_id>/cashier", methods=["PUT"])
@login_required
def save_cashier_instruction(case_id):
    if current_user.role not in ("reviewer", "admin"):
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json()
    instruction = data.get("instruction", "").strip()
    reasoning = data.get("reasoning", "").strip()
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE cases SET cashier_instruction_override = %s, cashier_instruction_reasoning = %s WHERE id = %s",
                (instruction, reasoning or None, case_id),
            )
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cases/<int:case_id>/review", methods=["POST"])
@login_required
def review_case(case_id):
    if current_user.role not in ("reviewer", "admin"):
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json() or {}
    action = data.get("action")
    if action not in ("approve", "reject"):
        return jsonify({"error": "Invalid action"}), 400
    note = (data.get("note") or "").strip()
    if action == "reject" and not note:
        return jsonify({"error": "Rejection note required"}), 400
    instruction = data.get("instruction")
    result_override = data.get("result_override")
    override_json = json.dumps(result_override) if result_override is not None else None
    status = "approved" if action == "approve" else "rejected"
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT review_status, submitted_by, case_number FROM cases WHERE id = %s FOR UPDATE", (case_id,))
            existing = cur.fetchone()
            if not existing:
                conn.close()
                return jsonify({"error": "Case not found"}), 404
            if existing["review_status"] != "pending":
                conn.close()
                return jsonify({"error": f"Case already {existing['review_status']}"}), 409
            cur.execute(
                """UPDATE cases SET review_status=%s, review_note=%s,
                   reviewed_by=%s, reviewed_at=NOW(),
                   cashier_instruction_override=COALESCE(%s, cashier_instruction_override),
                   result_override=%s
                   WHERE id=%s""",
                (status, note or None, int(current_user.id), instruction, override_json, case_id),
            )
            if action == "approve" and existing["submitted_by"]:
                cur.execute(
                    "INSERT INTO notifications (user_id, case_id, message) VALUES (%s, %s, %s)",
                    (existing["submitted_by"], case_id, f"Case approved — ready to action: {existing['case_number']}"),
                )
            cur.execute(
                """SELECT c.id, c.review_status, c.reviewed_at, c.result_override,
                          u.username AS reviewer_name
                   FROM cases c LEFT JOIN users u ON u.id = c.reviewed_by
                   WHERE c.id = %s""",
                (case_id,),
            )
            updated = cur.fetchone()
        conn.commit()
        conn.close()
        ro = updated["result_override"]
        if isinstance(ro, str):
            try:
                ro = json.loads(ro)
            except (ValueError, TypeError):
                ro = None
        return jsonify({
            "ok": True,
            "id": updated["id"],
            "review_status": updated["review_status"],
            "reviewed_at": updated["reviewed_at"].isoformat() if updated["reviewed_at"] else None,
            "reviewer_name": updated["reviewer_name"],
            "result_override": ro,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/variation-cases", methods=["DELETE"])
@login_required
def delete_all_variation_cases():
    if current_user.role != "admin":
        return jsonify({"error": "Forbidden"}), 403
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM notifications WHERE case_id IN
                    (SELECT id FROM cases WHERE task_type = 'variation')
            """)
            cur.execute("DELETE FROM cases WHERE task_type = 'variation'")
            cur.execute("SELECT COUNT(*) FROM cases WHERE task_type = 'variation'")
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cases/<int:case_id>", methods=["DELETE"])
@login_required
def delete_case(case_id):
    if current_user.role != "admin":
        return jsonify({"error": "Forbidden"}), 403
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM notifications WHERE case_id = %s", (case_id,))
            cur.execute("DELETE FROM cases WHERE id = %s", (case_id,))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Analyze
# ---------------------------------------------------------------------------
@app.route("/analyze", methods=["POST"])
@login_required
def analyze():
    if current_user.role not in ("uploader", "admin"):
        return jsonify({"error": "Forbidden"}), 403

    eos_state = request.form.get("eos_state", "NON_VMOC").strip().upper()
    if eos_state not in ("NON_VMOC", "VMOC_AGREED", "VMOC_UNAGREED"):
        eos_state = "NON_VMOC"
    additional_notes = request.form.get("notes", "").strip()
    case_number = request.form.get("case_number", "").strip()
    project_id_raw = request.form.get("project_id", "").strip()
    project_id = int(project_id_raw) if project_id_raw.isdigit() else None
    task_type = request.form.get("task_type", "completion").strip() or "completion"
    work_item_id_raw = request.form.get("work_item_id", "").strip()
    work_item_id = int(work_item_id_raw) if work_item_id_raw.isdigit() else None
    submitted_by = int(current_user.id)

    content = []
    any_document = False
    stored_images = {}

    for field_name, label in DOCUMENT_SLOTS:
        # Modifications: pasted text takes priority over screenshots
        if field_name == "modifications":
            mods_text = request.form.get("modifications_text", "").strip()
            if mods_text:
                any_document = True
                content.append({"type": "text", "text": f"--- {label} (pasted text) ---"})
                content.append({"type": "text", "text": f"Modifications text:\n{mods_text}"})
                continue

        files = request.files.getlist(field_name)
        pages = [f for f in files if f and f.filename]
        if not pages:
            continue
        any_document = True

        # EOS label gets state suffix for VMOC states
        if field_name == "eos" and eos_state != "NON_VMOC":
            doc_label = f"{label} [{eos_state}]"
        else:
            doc_label = label
        content.append({"type": "text", "text": f"--- {doc_label} ({len(pages)} page(s)) ---"})

        slot_imgs = []
        for page in pages:
            mime = (page.content_type or "").lower().split(";")[0].strip()
            if not mime:
                ext = page.filename.rsplit(".", 1)[-1].lower() if "." in page.filename else ""
                mime = {"pdf": "application/pdf"}.get(ext, "image/jpeg")

            if field_name == "rp" and mime == "application/pdf":
                # R&P as native PDF document block (no OCR needed)
                pdf_data = base64.b64encode(page.read()).decode()
                content.append({
                    "type": "document",
                    "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_data},
                })
            else:
                try:
                    image_data, media_type = encode_file(page)
                except ValueError as e:
                    return jsonify({"error": str(e)}), 400
                content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_data}})
                slot_imgs.append({"name": page.filename, "data": f"data:{media_type};base64,{image_data}"})

        if slot_imgs:
            stored_images[field_name] = slot_imgs

    # VMOC Modifications — required 6th document when eos_state is VMOC_UNAGREED
    if eos_state == "VMOC_UNAGREED":
        vmoc_mods_text = request.form.get("vmoc_modifications_text", "").strip()
        vmoc_mods_files = [f for f in request.files.getlist("vmoc_modifications") if f and f.filename]
        if not vmoc_mods_text and not vmoc_mods_files:
            return jsonify({"error": "VMOC Modifications required when EOS state is VMOC_UNAGREED."}), 400
        if vmoc_mods_text:
            any_document = True
            content.append({"type": "text", "text": "--- VMOC Modifications (pasted text) ---"})
            content.append({"type": "text", "text": f"VMOC Modifications text:\n{vmoc_mods_text}"})
        else:
            any_document = True
            content.append({"type": "text", "text": f"--- VMOC Modifications ({len(vmoc_mods_files)} page(s)) ---"})
            for page in vmoc_mods_files:
                try:
                    image_data, media_type = encode_file(page)
                except ValueError as e:
                    return jsonify({"error": str(e)}), 400
                content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_data}})

    # Mandatory field validation — all five slots required
    _mods_text_check = request.form.get("modifications_text", "").strip()
    _comp_mandatory = {
        "contribution_schedule": "Contribution Schedule",
        "modifications": "Modifications",
        "rp": "Receipts & Payments",
        "creditor_claims": "Creditor Claims",
        "eos": "EOS",
    }
    _comp_missing = []
    for _slot, _label in _comp_mandatory.items():
        has_files = bool([f for f in request.files.getlist(_slot) if f and f.filename])
        has_text = (_slot == "modifications" and bool(_mods_text_check))
        if not has_files and not has_text:
            _comp_missing.append(_label)
    if _comp_missing:
        return jsonify({"error": f"Missing required document(s): {', '.join(_comp_missing)}."}), 400

    trigger_parts = [f"eos_state: {eos_state}"]
    if additional_notes:
        trigger_parts.append(additional_notes)
    trigger_parts.append("CALCULATE")
    content.append({"type": "text", "text": "\n\n".join(trigger_parts)})

    def generate():
        full_text = []
        try:
            with client.messages.stream(
                model="claude-opus-4-7",
                max_tokens=16000,
                system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": content}],
            ) as stream:
                for text in stream.text_stream:
                    full_text.append(text)
                    yield f"data: {json.dumps({'text': text})}\n\n"

                msg = stream.get_final_message()
                usage = msg.usage
                case_id = None

                if case_number and os.environ.get("DATABASE_URL"):
                    try:
                        conn = get_db_conn()
                        with conn.cursor() as cur:
                            variation_data_json = json.dumps({"images": stored_images}) if stored_images else None
                            cur.execute(
                                """INSERT INTO cases
                                   (case_number, result, input_tokens, output_tokens,
                                    cache_creation_tokens, cache_read_tokens, submitted_by,
                                    project_id, task_type, variation_data)
                                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                                (case_number, "".join(full_text), usage.input_tokens, usage.output_tokens,
                                 getattr(usage, "cache_creation_input_tokens", 0),
                                 getattr(usage, "cache_read_input_tokens", 0), submitted_by,
                                 project_id, task_type, variation_data_json),
                            )
                            case_id = cur.fetchone()[0]
                            # mark linked work item in_progress → completed
                            if work_item_id:
                                cur.execute(
                                    "UPDATE work_items SET status='in_progress', assigned_to=%s WHERE id=%s",
                                    (submitted_by, work_item_id),
                                )
                            cur.execute(
                                "SELECT id FROM users WHERE role IN ('reviewer', 'admin') AND active = TRUE AND id != %s",
                                (submitted_by,),
                            )
                            for (uid,) in cur.fetchall():
                                cur.execute(
                                    "INSERT INTO notifications (user_id, case_id, message) VALUES (%s, %s, %s)",
                                    (uid, case_id, f"New case for review: {case_number}"),
                                )
                        conn.commit()
                        conn.close()
                    except Exception as e:
                        logger.exception("Failed to save case")
                        yield f"data: {json.dumps({'error': 'save_failed'})}\n\n"
                        return

                yield f"data: {json.dumps({'done': True, 'case_id': case_id, 'usage': {'input_tokens': usage.input_tokens, 'output_tokens': usage.output_tokens, 'cache_creation_tokens': getattr(usage, 'cache_creation_input_tokens', 0), 'cache_read_tokens': getattr(usage, 'cache_read_input_tokens', 0)}})}\n\n"

        except anthropic.APIError as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


# ---------------------------------------------------------------------------
# Arrears API
# ---------------------------------------------------------------------------

@app.route("/api/arrears/projects")
@login_required
def arrears_projects():
    """Return projects the current user can access (admin sees all)."""
    if not user_can_see(current_user, "arrears"):
        return jsonify({"error": "Forbidden"}), 403
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if current_user.role == "admin":
                cur.execute("SELECT id, name, slug FROM projects WHERE active = TRUE ORDER BY name")
            else:
                cur.execute("""
                    SELECT p.id, p.name, p.slug
                    FROM projects p
                    JOIN user_projects up ON up.project_id = p.id
                    WHERE up.user_id = %s AND p.active = TRUE
                    ORDER BY p.name
                """, (int(current_user.id),))
            rows = cur.fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/arrears/upload", methods=["POST"])
@login_required
def arrears_upload():
    """Accept a CSV upload for arrears data."""
    if current_user.role not in ("uploader", "admin"):
        return jsonify({"error": "Forbidden"}), 403
    if not user_can_see(current_user, "arrears"):
        return jsonify({"error": "Forbidden"}), 403
    project_id = request.form.get("project_id")
    upload_date = request.form.get("upload_date") or None
    if not project_id:
        return jsonify({"error": "project_id required"}), 400
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "file required"}), 400
    _ct = (f.content_type or "").lower().split(";")[0].strip()
    _fn = (f.filename or "").lower()
    if _ct not in ("text/csv", "application/csv", "text/plain") and not _fn.endswith(".csv"):
        return jsonify({"error": "Only CSV files are accepted."}), 400
    try:
        content = f.read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(content))
        rows_data = []
        for row in reader:
            client_name = (row.get("client_name") or row.get("Client Name") or "").strip() or None
            phone_number = (row.get("phone_number") or row.get("Phone Number") or row.get("phone") or "").strip() or None
            arrears_raw = (row.get("arrears_amount") or row.get("Arrears Amount") or "0").strip()
            try:
                arrears_amount = float(arrears_raw.replace(",", "").replace("£", "")) if arrears_raw else 0.0
            except ValueError:
                arrears_amount = 0.0
            lpd_raw = (row.get("last_payment_date") or row.get("Last Payment Date") or "").strip()
            last_payment_date = lpd_raw if lpd_raw else None
            last_note = (row.get("last_note") or row.get("Last Note") or "").strip() or None
            rows_data.append((client_name, phone_number, arrears_amount, last_payment_date, last_note))

        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO arrears_uploads (project_id, upload_date, uploaded_by, record_count, filename)
                   VALUES (%s, COALESCE(%s::date, CURRENT_DATE), %s, %s, %s) RETURNING id""",
                (project_id, upload_date, int(current_user.id), len(rows_data), f.filename),
            )
            upload_id = cur.fetchone()[0]
            for (cn, ph, amt, lpd, ln) in rows_data:
                cur.execute(
                    """INSERT INTO arrears_cases (upload_id, project_id, client_name, phone_number,
                       arrears_amount, last_payment_date, last_note)
                       VALUES (%s, %s, %s, %s, %s, %s::date, %s)""",
                    (upload_id, project_id, cn, ph, amt, lpd, ln),
                )
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "upload_id": upload_id, "record_count": len(rows_data)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _get_arrears_config(cur, project_id):
    """Fetch arrears config for a project. Returns dict or None."""
    cur.execute(
        "SELECT * FROM arrears_project_config WHERE project_id = %s",
        (project_id,),
    )
    return cur.fetchone()


def _build_arrears_sql_filter(cfg):
    """Return a SQL WHERE fragment and params for in-arrears logic."""
    if cfg is None:
        return "arrears_amount > 0", []

    min_days = cfg["min_days_since_payment"]
    min_amt = cfg["min_arrears_amount"]
    require_both = cfg["require_both"]

    rule1 = None
    rule2 = None
    params = []

    if min_days is not None:
        rule1 = "(CURRENT_DATE - last_payment_date) >= %s"
        params.append(min_days)
    if min_amt is not None:
        rule2 = "arrears_amount >= %s"

    if rule1 and rule2:
        if require_both:
            fragment = f"({rule1} AND {rule2})"
            params.append(min_amt)
        else:
            fragment = f"({rule1} OR {rule2})"
            params.append(min_amt)
    elif rule1:
        fragment = rule1
    elif rule2:
        fragment = rule2
        params.append(min_amt)
    else:
        # No rules defined — fall back to amount > 0
        fragment = "arrears_amount > 0"

    return fragment, params


@app.route("/api/arrears/dashboard")
@login_required
def arrears_dashboard():
    """Return stats for a project: total live cases, in-arrears count, percentage."""
    if not user_can_see(current_user, "arrears"):
        return jsonify({"error": "Forbidden"}), 403
    project_id = request.args.get("project_id")
    if not project_id:
        return jsonify({"error": "project_id required"}), 400
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Latest upload
            cur.execute(
                "SELECT id FROM arrears_uploads WHERE project_id = %s ORDER BY created_at DESC LIMIT 1",
                (project_id,),
            )
            row = cur.fetchone()
            if not row:
                conn.close()
                return jsonify({"total": 0, "in_arrears": 0, "percentage": 0.0, "upload_id": None})
            upload_id = row["id"]

            cur.execute(
                "SELECT COUNT(*) AS total FROM arrears_cases WHERE upload_id = %s",
                (upload_id,),
            )
            total = cur.fetchone()["total"]

            cfg = _get_arrears_config(cur, int(project_id))
            frag, params = _build_arrears_sql_filter(cfg)
            sql = f"SELECT COUNT(*) AS cnt FROM arrears_cases WHERE upload_id = %s AND {frag}"
            cur.execute(sql, [upload_id] + params)
            in_arrears = cur.fetchone()["cnt"]

        conn.close()
        pct = round((in_arrears / total * 100), 1) if total else 0.0
        return jsonify({"total": total, "in_arrears": in_arrears, "percentage": pct, "upload_id": upload_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/arrears/cases")
@login_required
def arrears_cases_list():
    """Return in-arrears cases from the latest upload for a project."""
    if not user_can_see(current_user, "arrears"):
        return jsonify({"error": "Forbidden"}), 403
    project_id = request.args.get("project_id")
    if not project_id:
        return jsonify({"error": "project_id required"}), 400
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id FROM arrears_uploads WHERE project_id = %s ORDER BY created_at DESC LIMIT 1",
                (project_id,),
            )
            row = cur.fetchone()
            if not row:
                conn.close()
                return jsonify([])
            upload_id = row["id"]

            cfg = _get_arrears_config(cur, int(project_id))
            frag, params = _build_arrears_sql_filter(cfg)
            sql = f"""
                SELECT id, client_name, phone_number, arrears_amount,
                       last_payment_date, last_note,
                       (CURRENT_DATE - last_payment_date) AS days_overdue
                FROM arrears_cases
                WHERE upload_id = %s AND {frag}
                ORDER BY arrears_amount DESC
            """
            cur.execute(sql, [upload_id] + params)
            rows = cur.fetchall()
        conn.close()
        result = []
        for r in rows:
            d = dict(r)
            if d["last_payment_date"]:
                d["last_payment_date"] = d["last_payment_date"].isoformat()
            d["days_overdue"] = int(d["days_overdue"]) if d["days_overdue"] is not None else None
            d["arrears_amount"] = float(d["arrears_amount"]) if d["arrears_amount"] is not None else 0.0
            result.append(d)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/arrears/config/<int:project_id>", methods=["GET"])
@login_required
def get_arrears_config(project_id):
    """Return the arrears logic config for a project."""
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cfg = _get_arrears_config(cur, project_id)
        conn.close()
        if not cfg:
            return jsonify({"project_id": project_id, "min_days_since_payment": None,
                            "min_arrears_amount": None, "require_both": False,
                            "logic_description": ""})
        d = dict(cfg)
        if d.get("updated_at"):
            d["updated_at"] = d["updated_at"].isoformat()
        if d.get("min_arrears_amount") is not None:
            d["min_arrears_amount"] = float(d["min_arrears_amount"])
        return jsonify(d)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/arrears/config/<int:project_id>", methods=["POST"])
@login_required
def save_arrears_config(project_id):
    """Save/update the arrears logic config. Admin only."""
    if current_user.role != "admin":
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json() or {}
    min_days = data.get("min_days_since_payment")
    min_amt = data.get("min_arrears_amount")
    require_both = bool(data.get("require_both", False))
    logic_description = (data.get("logic_description") or "").strip() or None
    try:
        min_days = int(min_days) if min_days not in (None, "") else None
    except (ValueError, TypeError):
        min_days = None
    try:
        min_amt = float(min_amt) if min_amt not in (None, "") else None
    except (ValueError, TypeError):
        min_amt = None
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO arrears_project_config
                    (project_id, min_days_since_payment, min_arrears_amount, require_both, logic_description, updated_at, updated_by)
                VALUES (%s, %s, %s, %s, %s, NOW(), %s)
                ON CONFLICT (project_id) DO UPDATE SET
                    min_days_since_payment = EXCLUDED.min_days_since_payment,
                    min_arrears_amount = EXCLUDED.min_arrears_amount,
                    require_both = EXCLUDED.require_both,
                    logic_description = EXCLUDED.logic_description,
                    updated_at = NOW(),
                    updated_by = EXCLUDED.updated_by
            """, (project_id, min_days, min_amt, require_both, logic_description, int(current_user.id)))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Parker Philips Arrears — Page route
# ---------------------------------------------------------------------------
@app.route("/pp-arrears")
@login_required
def pp_arrears():
    return render_template("pp_arrears.html")


# ---------------------------------------------------------------------------
# Parker Philips Arrears — API helpers
# ---------------------------------------------------------------------------
def _pp_get_latest_snapshot_id(cur):
    """Return the UUID of the latest non-superseded PP snapshot, or None."""
    cur.execute(
        "SELECT id FROM pp_snapshots WHERE superseded = FALSE ORDER BY snapshot_date DESC, uploaded_at DESC LIMIT 1"
    )
    row = cur.fetchone()
    return row["id"] if row else None


def _pp_case_to_dict(row) -> dict:
    """Convert a pp_case_snapshots RealDictRow to a JSON-serialisable dict."""
    d = dict(row)
    for key in ("payment_amount", "arrears_amount", "months_in_arrears",
                "catchup_amount", "iva_fees_arrears", "wf_arrears_amount",
                "cases_in_arrears_amount", "td_arrears_amount"):
        if d.get(key) is not None:
            d[key] = float(d[key])
    for key in ("last_payment_due_date",):
        if d.get(key) is not None:
            d[key] = d[key].isoformat()
    for key in ("last_contact_date",):
        if d.get(key) is not None:
            d[key] = d[key].isoformat()
    if d.get("id"):
        d["id"] = str(d["id"])
    if d.get("snapshot_id"):
        d["snapshot_id"] = str(d["snapshot_id"])
    return d


# ---------------------------------------------------------------------------
# POST /api/pp/upload  (admin / uploader only)
# ---------------------------------------------------------------------------
@app.route("/api/pp/upload", methods=["POST"])
@login_required
def pp_upload():
    if current_user.role not in ("admin", "uploader"):
        return jsonify({"error": "Forbidden"}), 403

    import tempfile
    from parker_philips_arrears import run_pipeline

    required_files = ["iva_fees", "td_fees", "cases_in_arrears", "wf_arrears", "total_live_cases"]
    missing = [k for k in required_files if k not in request.files]
    if missing:
        return jsonify({"error": f"Missing files: {', '.join(missing)}"}), 400

    snapshot_date_raw = (request.form.get("snapshot_date") or "").strip()
    confirm = request.form.get("confirm", "false").lower() in ("true", "1", "yes")

    try:
        from datetime import date as _date
        snap_date = _date.fromisoformat(snapshot_date_raw) if snapshot_date_raw else _date.today()
    except ValueError:
        return jsonify({"error": "Invalid snapshot_date format (use YYYY-MM-DD)"}), 400

    # Save uploaded files to temp dir
    tmp_dir = tempfile.mkdtemp()
    file_paths = {}
    try:
        for key in required_files:
            f = request.files[key]
            ext = (f.filename or "").rsplit(".", 1)[-1].lower()
            if ext not in ("xlsx", "xls"):
                return jsonify({"error": f"File '{key}' must be an Excel file (.xlsx or .xls)."}), 400
            dest = os.path.join(tmp_dir, f"{key}.xlsx")
            f.save(dest)
            file_paths[key] = dest

        # Run pipeline
        try:
            result = run_pipeline(
                iva_fees=file_paths["iva_fees"],
                td_fees=file_paths["td_fees"],
                cases_in_arrears=file_paths["cases_in_arrears"],
                wf_arrears=file_paths["wf_arrears"],
                total_live_cases=file_paths["total_live_cases"],
                snapshot_date=snap_date,
            )
        except ValueError as ve:
            return jsonify({"error": str(ve)}), 400

    finally:
        # Clean up temp files
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)

    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Check if snapshot exists for today
            cur.execute(
                "SELECT id FROM pp_snapshots WHERE snapshot_date = %s AND superseded = FALSE",
                (snap_date,),
            )
            existing = cur.fetchone()
            if existing and not confirm:
                conn.close()
                return jsonify({
                    "needs_confirm": True,
                    "message": f"A snapshot already exists for {snap_date}. Replace it?",
                })

            # If replacing, mark existing as superseded
            if existing:
                cur.execute(
                    "UPDATE pp_snapshots SET superseded = TRUE WHERE snapshot_date = %s AND superseded = FALSE",
                    (snap_date,),
                )

            # Get all historical references for cycle_status determination
            cur.execute("SELECT DISTINCT reference FROM pp_case_snapshots")
            historical_refs = {row["reference"] for row in cur.fetchall()}

            # Insert snapshot
            result_dict = result.to_dict()
            # Remove cases from the stored pipeline_result to avoid bloat (cases go into pp_case_snapshots)
            result_dict_meta = {k: v for k, v in result_dict.items() if k != "cases"}
            cur.execute(
                """INSERT INTO pp_snapshots (snapshot_date, uploaded_by, source, pipeline_result)
                   VALUES (%s, %s, 'file_upload', %s) RETURNING id""",
                (snap_date, int(current_user.id), json.dumps(result_dict_meta)),
            )
            snapshot_id = str(cur.fetchone()["id"])

            # Insert case snapshots
            for c in result.cases:
                cycle_status = "History of arrears" if c.reference in historical_refs else "New"
                cur.execute(
                    """INSERT INTO pp_case_snapshots (
                        snapshot_id, reference, client_name, mobile, case_type,
                        payment_amount, arrears_amount, cycle, cycle_status,
                        months_in_arrears, last_payment_due_date, days_since_last_payment_due,
                        payment_break, catchup_agreed, catchup_amount, vulnerable,
                        case_senior, last_contact_date, last_contact_notes, case_status,
                        needs_manual_review, review_reason, sources_present,
                        iva_fees_arrears, wf_arrears_amount, cases_in_arrears_amount, td_arrears_amount
                    ) VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s, %s
                    )""",
                    (
                        snapshot_id, c.reference, c.client_name, c.mobile, c.case_type,
                        c.payment_amount, c.arrears_amount, c.cycle, cycle_status,
                        c.months_in_arrears, c.last_payment_due_date, c.days_since_last_payment_due,
                        c.payment_break, c.catchup_agreed, c.catchup_amount, c.vulnerable,
                        c.case_senior, c.last_contact_date, c.last_contact_notes, c.case_status,
                        c.needs_manual_review, c.review_reason, c.sources_present,
                        c.iva_fees_arrears, c.wf_arrears_amount, c.cases_in_arrears_amount, c.td_arrears_amount,
                    ),
                )

        conn.commit()
        conn.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    total_live = result.total_live_iva + result.total_live_cpi + result.total_live_td
    return jsonify({
        "snapshot_id": snapshot_id,
        "snapshot_date": snap_date.isoformat(),
        "total_live": total_live,
        "in_arrears_count": result.total_in_arrears,
        "in_arrears_value": result.total_arrears_value,
        "by_cycle": result.by_cycle,
        "by_case_type": result.by_case_type,
        "warnings": result.warnings,
    })


# ---------------------------------------------------------------------------
# GET /api/pp/latest-snapshot
# ---------------------------------------------------------------------------
@app.route("/api/pp/latest-snapshot")
@login_required
def pp_latest_snapshot():
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT id, snapshot_date, uploaded_at, uploaded_by, pipeline_result
                   FROM pp_snapshots WHERE superseded = FALSE
                   ORDER BY snapshot_date DESC, uploaded_at DESC LIMIT 1"""
            )
            row = cur.fetchone()
        conn.close()
        if not row:
            return jsonify(None)
        d = dict(row)
        d["id"] = str(d["id"])
        d["snapshot_date"] = d["snapshot_date"].isoformat()
        d["uploaded_at"] = d["uploaded_at"].isoformat()
        return jsonify(d)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# GET /api/pp/cases
# ---------------------------------------------------------------------------
@app.route("/api/pp/cases")
@login_required
def pp_cases_list():
    snapshot_id = request.args.get("snapshot_id")
    case_type = request.args.get("case_type")
    cycles = request.args.getlist("cycle")
    vulnerable = request.args.get("vulnerable")
    needs_review = request.args.get("needs_manual_review")
    search = (request.args.get("search") or "").strip()
    show = request.args.get("show", "active")   # active | all | worked | resolved
    min_arrears = request.args.get("min_arrears")
    max_arrears = request.args.get("max_arrears")
    sort_col = request.args.get("sort", "arrears_amount")
    sort_dir = request.args.get("dir", "desc").upper()
    try:
        offset = int(request.args.get("offset", 0))
        limit  = int(request.args.get("limit", 100))
    except ValueError:
        offset, limit = 0, 100

    allowed_sorts = {
        "arrears_amount", "reference", "client_name", "cycle",
        "case_type", "days_since_last_payment_due", "months_in_arrears",
    }
    if sort_col not in allowed_sorts:
        sort_col = "arrears_amount"
    if sort_dir not in ("ASC", "DESC"):
        sort_dir = "DESC"

    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Resolve snapshot
            if not snapshot_id:
                cur.execute(
                    "SELECT id FROM pp_snapshots WHERE superseded = FALSE ORDER BY snapshot_date DESC, uploaded_at DESC LIMIT 1"
                )
                row = cur.fetchone()
                if not row:
                    conn.close()
                    return jsonify({"cases": [], "total": 0, "snapshot_date": None})
                snapshot_id = str(row["id"])
            else:
                # Validate it exists
                cur.execute("SELECT id, snapshot_date FROM pp_snapshots WHERE id = %s::uuid", (snapshot_id,))
                row = cur.fetchone()
                if not row:
                    conn.close()
                    return jsonify({"error": "Snapshot not found"}), 404

            # Get snapshot date for response
            cur.execute("SELECT snapshot_date FROM pp_snapshots WHERE id = %s::uuid", (snapshot_id,))
            snap_row = cur.fetchone()
            snap_date_str = snap_row["snapshot_date"].isoformat() if snap_row else None

            # Base filters
            filters = ["cs.snapshot_id = %s::uuid"]
            params: list = [snapshot_id]

            if case_type:
                filters.append("cs.case_type = %s")
                params.append(case_type)
            if cycles:
                filters.append("cs.cycle = ANY(%s)")
                params.append(cycles)
            if vulnerable and vulnerable.lower() in ("true", "1"):
                filters.append("cs.vulnerable = TRUE")
            if needs_review and needs_review.lower() in ("true", "1"):
                filters.append("cs.needs_manual_review = TRUE")
            if search:
                filters.append("(cs.reference ILIKE %s OR cs.client_name ILIKE %s OR cs.mobile ILIKE %s)")
                like = f"%{search}%"
                params += [like, like, like]
            if min_arrears:
                try:
                    filters.append("cs.arrears_amount >= %s")
                    params.append(float(min_arrears))
                except ValueError:
                    pass
            if max_arrears:
                try:
                    filters.append("cs.arrears_amount <= %s")
                    params.append(float(max_arrears))
                except ValueError:
                    pass

            # Show filter
            if show == "active":
                # No active note (removes_from_queue=TRUE AND superseded_at IS NULL)
                filters.append(
                    "NOT EXISTS (SELECT 1 FROM pp_case_notes n WHERE n.reference = cs.reference "
                    "AND n.removes_from_queue = TRUE AND n.superseded_at IS NULL)"
                )
            elif show == "worked":
                filters.append(
                    "EXISTS (SELECT 1 FROM pp_case_notes n WHERE n.reference = cs.reference "
                    "AND n.removes_from_queue = TRUE AND n.superseded_at IS NULL)"
                )
            # "all" and "resolved" handled below; "resolved" requires a different query

            where_clause = " AND ".join(filters)

            # Count
            cur.execute(
                f"SELECT COUNT(*) AS cnt FROM pp_case_snapshots cs WHERE {where_clause}",
                params,
            )
            total = cur.fetchone()["cnt"]

            # Fetch
            cur.execute(
                f"""SELECT cs.*, n.created_at AS note_created_at
                    FROM pp_case_snapshots cs
                    LEFT JOIN LATERAL (
                        SELECT created_at FROM pp_case_notes
                        WHERE reference = cs.reference AND removes_from_queue = TRUE AND superseded_at IS NULL
                        ORDER BY created_at DESC LIMIT 1
                    ) n ON TRUE
                    WHERE {where_clause}
                    ORDER BY cs.{sort_col} {sort_dir}
                    LIMIT %s OFFSET %s""",
                params + [limit, offset],
            )
            rows = cur.fetchall()

        conn.close()
        cases_out = [_pp_case_to_dict(r) for r in rows]
        return jsonify({"cases": cases_out, "total": int(total), "snapshot_date": snap_date_str})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# GET /api/pp/cases/<reference>
# ---------------------------------------------------------------------------
@app.route("/api/pp/cases/<path:reference>")
@login_required
def pp_case_detail(reference):
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Latest snapshot data for this reference
            cur.execute(
                """SELECT cs.* FROM pp_case_snapshots cs
                   JOIN pp_snapshots s ON s.id = cs.snapshot_id
                   WHERE cs.reference = %s AND s.superseded = FALSE
                   ORDER BY s.snapshot_date DESC LIMIT 1""",
                (reference,),
            )
            case_row = cur.fetchone()

            # All notes history
            cur.execute(
                """SELECT id, note_text, note_category, created_at, created_by,
                          removes_from_queue, arrears_at_time, cycle_at_time,
                          superseded_at, superseded_reason
                   FROM pp_case_notes
                   WHERE reference = %s
                   ORDER BY created_at ASC""",
                (reference,),
            )
            notes_rows = cur.fetchall()

            # Last 30 snapshots arrears trend
            cur.execute(
                """SELECT s.snapshot_date, cs.arrears_amount, cs.cycle
                   FROM pp_case_snapshots cs
                   JOIN pp_snapshots s ON s.id = cs.snapshot_id
                   WHERE cs.reference = %s AND s.superseded = FALSE
                   ORDER BY s.snapshot_date DESC LIMIT 30""",
                (reference,),
            )
            trend_rows = cur.fetchall()

        conn.close()
        if not case_row:
            return jsonify({"error": "Case not found"}), 404

        case_dict = _pp_case_to_dict(case_row)

        notes_out = []
        for n in notes_rows:
            nd = dict(n)
            nd["id"] = str(nd["id"])
            nd["created_at"] = nd["created_at"].isoformat() if nd.get("created_at") else None
            nd["superseded_at"] = nd["superseded_at"].isoformat() if nd.get("superseded_at") else None
            if nd.get("arrears_at_time") is not None:
                nd["arrears_at_time"] = float(nd["arrears_at_time"])
            notes_out.append(nd)

        trend_out = []
        for t in trend_rows:
            td = dict(t)
            td["snapshot_date"] = td["snapshot_date"].isoformat()
            td["arrears_amount"] = float(td["arrears_amount"]) if td.get("arrears_amount") else 0.0
            trend_out.append(td)

        return jsonify({"case": case_dict, "notes": notes_out, "trend": trend_out})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# POST /api/pp/cases/<reference>/notes
# ---------------------------------------------------------------------------
@app.route("/api/pp/cases/<path:reference>/notes", methods=["POST"])
@login_required
def pp_add_note(reference):
    data = request.get_json() or {}
    note_text = (data.get("note_text") or "").strip()
    note_category = (data.get("note_category") or "").strip() or None
    removes_from_queue = bool(data.get("removes_from_queue", True))

    if not note_text:
        return jsonify({"error": "note_text is required"}), 400

    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Get current snapshot info for the case
            snap_id = _pp_get_latest_snapshot_id(cur)
            cur.execute(
                "SELECT arrears_amount, cycle FROM pp_case_snapshots WHERE reference = %s AND snapshot_id = %s::uuid LIMIT 1",
                (reference, snap_id),
            ) if snap_id else None
            case_row = cur.fetchone() if snap_id else None
            arrears_at_time = float(case_row["arrears_amount"]) if case_row and case_row.get("arrears_amount") else None
            cycle_at_time = case_row["cycle"] if case_row else None

            cur.execute(
                """INSERT INTO pp_case_notes
                   (reference, note_text, note_category, created_by, removes_from_queue,
                    arrears_at_time, cycle_at_time, snapshot_id_at_time)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s::uuid)
                   RETURNING id, reference, note_text, note_category, created_at,
                             removes_from_queue, arrears_at_time, cycle_at_time""",
                (reference, note_text, note_category, int(current_user.id), removes_from_queue,
                 arrears_at_time, cycle_at_time, snap_id),
            )
            note_row = cur.fetchone()
        conn.commit()
        conn.close()
        nd = dict(note_row)
        nd["id"] = str(nd["id"])
        nd["created_at"] = nd["created_at"].isoformat() if nd.get("created_at") else None
        if nd.get("arrears_at_time") is not None:
            nd["arrears_at_time"] = float(nd["arrears_at_time"])
        return jsonify(nd), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# GET /api/pp/movement
# ---------------------------------------------------------------------------
@app.route("/api/pp/movement")
@login_required
def pp_movement():
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Latest two non-superseded snapshots
            cur.execute(
                """SELECT id, snapshot_date FROM pp_snapshots WHERE superseded = FALSE
                   ORDER BY snapshot_date DESC, uploaded_at DESC LIMIT 2"""
            )
            snaps = cur.fetchall()
        if len(snaps) < 2:
            conn.close()
            return jsonify({"new": [], "cleared": [], "cycle_changes": [], "message": "Need at least 2 snapshots"})

        latest_id = str(snaps[0]["id"])
        prev_id   = str(snaps[1]["id"])
        latest_date = snaps[0]["snapshot_date"].isoformat()
        prev_date   = snaps[1]["snapshot_date"].isoformat()

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # New: in latest but not prev
            cur.execute(
                """SELECT cs.reference, cs.client_name, cs.case_type, cs.arrears_amount, cs.cycle
                   FROM pp_case_snapshots cs
                   WHERE cs.snapshot_id = %s::uuid
                     AND cs.reference NOT IN (
                         SELECT reference FROM pp_case_snapshots WHERE snapshot_id = %s::uuid
                     )""",
                (latest_id, prev_id),
            )
            new_cases = [_pp_case_to_dict(r) for r in cur.fetchall()]

            # Cleared: in prev but not latest
            cur.execute(
                """SELECT cs.reference, cs.client_name, cs.case_type, cs.arrears_amount, cs.cycle
                   FROM pp_case_snapshots cs
                   WHERE cs.snapshot_id = %s::uuid
                     AND cs.reference NOT IN (
                         SELECT reference FROM pp_case_snapshots WHERE snapshot_id = %s::uuid
                     )""",
                (prev_id, latest_id),
            )
            cleared_cases = [_pp_case_to_dict(r) for r in cur.fetchall()]

            # Cycle changes: in both but different cycle
            cur.execute(
                """SELECT
                       l.reference, l.client_name, l.case_type,
                       p.cycle AS prev_cycle, l.cycle AS new_cycle,
                       p.arrears_amount AS prev_arrears, l.arrears_amount AS new_arrears
                   FROM pp_case_snapshots l
                   JOIN pp_case_snapshots p ON p.reference = l.reference AND p.snapshot_id = %s::uuid
                   WHERE l.snapshot_id = %s::uuid AND l.cycle != p.cycle""",
                (prev_id, latest_id),
            )
            cycle_changes = []
            for r in cur.fetchall():
                d = dict(r)
                if d.get("prev_arrears") is not None:
                    d["prev_arrears"] = float(d["prev_arrears"])
                if d.get("new_arrears") is not None:
                    d["new_arrears"] = float(d["new_arrears"])
                cycle_changes.append(d)

        conn.close()
        return jsonify({
            "new": new_cases,
            "cleared": cleared_cases,
            "cycle_changes": cycle_changes,
            "latest_date": latest_date,
            "prev_date": prev_date,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# GET /api/pp/snapshots  (admin only)
# ---------------------------------------------------------------------------
@app.route("/api/pp/snapshots")
@login_required
def pp_snapshots_list():
    if current_user.role != "admin":
        return jsonify({"error": "Forbidden"}), 403
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT s.id, s.snapshot_date, s.uploaded_at, s.uploaded_by,
                          s.superseded, s.source,
                          COUNT(cs.id) AS case_count,
                          SUM(cs.arrears_amount) AS total_arrears_value,
                          u.username AS uploaded_by_name
                   FROM pp_snapshots s
                   LEFT JOIN pp_case_snapshots cs ON cs.snapshot_id = s.id
                   LEFT JOIN users u ON u.id = s.uploaded_by
                   GROUP BY s.id, s.snapshot_date, s.uploaded_at, s.uploaded_by,
                            s.superseded, s.source, u.username
                   ORDER BY s.snapshot_date DESC, s.uploaded_at DESC"""
            )
            rows = cur.fetchall()
        conn.close()
        result = []
        for r in rows:
            d = dict(r)
            d["id"] = str(d["id"])
            d["snapshot_date"] = d["snapshot_date"].isoformat()
            d["uploaded_at"] = d["uploaded_at"].isoformat()
            if d.get("total_arrears_value") is not None:
                d["total_arrears_value"] = float(d["total_arrears_value"])
            result.append(d)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ===========================================================================
# DSS Workload Management
# ===========================================================================

# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------
@app.route("/dss")
@login_required
def dss_index():
    if current_user.role not in ("admin", "team_leader"):
        return redirect(url_for("home"))
    if not user_can_see(current_user, "dss"):
        abort(404)
    return redirect(url_for("dss_dashboard"))


@app.route("/dss/dashboard")
@login_required
def dss_dashboard():
    if current_user.role not in ("admin", "team_leader"):
        return redirect(url_for("home"))
    if not user_can_see(current_user, "dss"):
        abort(404)
    return render_template("dss_dashboard.html")


@app.route("/dss/entry")
@login_required
def dss_entry():
    if current_user.role not in ("admin", "team_leader"):
        return redirect(url_for("home"))
    if not user_can_see(current_user, "dss"):
        abort(404)
    return render_template("dss_entry.html")


@app.route("/dss/history")
@login_required
def dss_history():
    if current_user.role not in ("admin", "team_leader"):
        return redirect(url_for("home"))
    if not user_can_see(current_user, "dss"):
        abort(404)
    return render_template("dss_history.html")


@app.route("/dss/settings")
@login_required
def dss_settings():
    if current_user.role != "admin":
        return redirect(url_for("home"))
    if not user_can_see(current_user, "dss"):
        abort(404)
    return render_template("dss_settings.html")


@app.route("/dss/agent-performance")
@login_required
def dss_agent_performance():
    if current_user.role not in ("admin", "team_leader"):
        abort(403)
    if not user_can_see(current_user, "dss"):
        abort(404)
    return render_template("dss_agent_performance.html")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _dss_get_team(cur):
    """Return the first dss_team row as a dict."""
    cur.execute("SELECT * FROM dss_teams ORDER BY id LIMIT 1")
    return cur.fetchone()


def _dss_get_base_rate(cur, team_id):
    """Return the rate_per_hour of the base task type for the team."""
    cur.execute(
        "SELECT rate_per_hour FROM dss_task_types WHERE team_id = %s AND is_base = TRUE AND is_active = TRUE LIMIT 1",
        (team_id,),
    )
    row = cur.fetchone()
    return float(row["rate_per_hour"]) if row else 15.0


def _dss_bulk_load(cur, team_id, up_to_date):
    """
    Load all shifts+completions and landings up to (and including) up_to_date in 2 queries.
    Returns (shift_rows, landing_rows) as raw dicts for downstream processing.
    """
    cur.execute(
        """SELECT ds.id AS shift_id, ds.team_member_id, ds.work_date,
                  ds.hours_worked, ds.notes, tm.name AS member_name,
                  dc.count AS comp_count, dc.conversion_factor
           FROM dss_daily_shifts ds
           JOIN dss_team_members tm ON tm.id = ds.team_member_id
           LEFT JOIN dss_daily_completions dc ON dc.daily_shift_id = ds.id
           WHERE ds.team_id = %s AND ds.work_date <= %s
           ORDER BY ds.work_date, ds.id""",
        (team_id, up_to_date),
    )
    shift_rows = cur.fetchall()

    cur.execute(
        """SELECT dl.work_date,
                  dl.count * (br.base_rate / tt.rate_per_hour) AS units
           FROM dss_daily_landings dl
           JOIN dss_task_types tt ON tt.id = dl.task_type_id
           CROSS JOIN (
               SELECT rate_per_hour AS base_rate
               FROM dss_task_types
               WHERE team_id = %s AND is_base = TRUE AND is_active = TRUE
               LIMIT 1
           ) br
           WHERE dl.team_id = %s AND dl.work_date <= %s""",
        (team_id, team_id, up_to_date),
    )
    landing_rows = cur.fetchall()
    return shift_rows, landing_rows


def _dss_build_series(shift_rows, landing_rows, base_rate, starting_backlog):
    """
    From raw bulk rows build:
      shifts_by_date, completions_by_shift, landed_by_date,
      completed_by_date, backlog_by_date, all_dates (sorted)
    All heavy lifting done in Python — zero extra DB queries.
    """
    from collections import defaultdict

    seen_shifts = {}
    shifts_by_date = defaultdict(list)
    completions_by_shift = defaultdict(list)

    for row in shift_rows:
        sid = row["shift_id"]
        if sid not in seen_shifts:
            s = {
                "id": sid,
                "team_member_id": row["team_member_id"],
                "work_date": row["work_date"],
                "hours_worked": float(row["hours_worked"] or 0),
                "notes": row.get("notes"),
                "member_name": row["member_name"],
            }
            seen_shifts[sid] = s
            shifts_by_date[row["work_date"]].append(s)
        if row["comp_count"] is not None:
            completions_by_shift[sid].append({
                "count": row["comp_count"],
                "conversion_factor": float(row["conversion_factor"]),
            })

    landed_by_date = defaultdict(float)
    for row in landing_rows:
        landed_by_date[row["work_date"]] += float(row["units"])

    all_dates = sorted(set(list(shifts_by_date.keys()) + list(landed_by_date.keys())))

    completed_by_date = {}
    for d in all_dates:
        total = 0.0
        for shift in shifts_by_date.get(d, []):
            comps = completions_by_shift.get(shift["id"], [])
            metrics = dss_calc.shift_metrics(shift["hours_worked"], comps, base_rate)
            total += metrics["actual_units"]
        completed_by_date[d] = total

    backlog_by_date = {}
    running = starting_backlog
    for d in all_dates:
        landed = landed_by_date.get(d, 0.0)
        completed = completed_by_date.get(d, 0.0)
        rollup = dss_calc.daily_team_rollup(landed, completed, 0, running)
        running = rollup["running_backlog"]
        backlog_by_date[d] = running

    return {
        "seen_shifts": seen_shifts,
        "shifts_by_date": shifts_by_date,
        "completions_by_shift": completions_by_shift,
        "landed_by_date": landed_by_date,
        "completed_by_date": completed_by_date,
        "backlog_by_date": backlog_by_date,
        "all_dates": all_dates,
    }


# ---------------------------------------------------------------------------
# GET /api/dss/dashboard
# ---------------------------------------------------------------------------
@app.route("/api/dss/dashboard")
@login_required
def dss_api_dashboard():
    if current_user.role not in ("admin", "team_leader"):
        return jsonify({"error": "Forbidden"}), 403
    if not user_can_see(current_user, "dss"):
        return jsonify({"error": "Forbidden"}), 403

    date_str = request.args.get("date")
    try:
        from datetime import date as date_type
        work_date = date_type.fromisoformat(date_str) if date_str else date_type.today()
    except ValueError:
        return jsonify({"error": "Invalid date"}), 400

    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            team = _dss_get_team(cur)
            if not team:
                conn.close()
                return jsonify({"error": "No team configured"}), 404
            team_id = team["id"]
            base_rate = _dss_get_base_rate(cur, team_id)

            cur.execute("SELECT sla_breach_threshold_days, starting_backlog_units FROM dss_team_settings WHERE team_id = %s", (team_id,))
            settings_row = cur.fetchone()
            threshold = settings_row["sla_breach_threshold_days"] if settings_row else 3
            starting_backlog = float(settings_row["starting_backlog_units"]) if settings_row else 0.0

            # Two queries replace hundreds: bulk load all data up to work_date
            shift_rows, landing_rows = _dss_bulk_load(cur, team_id, work_date)

            # Avg daily capacity: last 7 dates with hours > 0
            cur.execute(
                """SELECT work_date, SUM(hours_worked) AS total_hours
                   FROM dss_daily_shifts
                   WHERE team_id = %s AND work_date <= %s AND hours_worked > 0
                   GROUP BY work_date HAVING SUM(hours_worked) > 0
                   ORDER BY work_date DESC LIMIT 7""",
                (team_id, work_date),
            )
            cap_rows = cur.fetchall()

        conn.close()

        avg_daily_cap = (sum(float(r["total_hours"]) * base_rate for r in cap_rows) / len(cap_rows)) if cap_rows else 0

        series = _dss_build_series(shift_rows, landing_rows, base_rate, starting_backlog)
        shifts_by_date = series["shifts_by_date"]
        completions_by_shift = series["completions_by_shift"]
        landed_by_date = series["landed_by_date"]
        completed_by_date = series["completed_by_date"]
        backlog_by_date = series["backlog_by_date"]
        all_dates = series["all_dates"]

        # Build per-agent list for the requested date
        # Group historical shifts by member for rolling avg (all data loaded already)
        from collections import defaultdict
        member_shift_history = defaultdict(list)  # member_id -> [{work_date, pct_target_hit}] newest first
        for d in reversed(all_dates):
            for shift in shifts_by_date.get(d, []):
                if shift["hours_worked"] > 0:
                    comps = completions_by_shift.get(shift["id"], [])
                    hm = dss_calc.shift_metrics(shift["hours_worked"], comps, base_rate)
                    member_shift_history[shift["team_member_id"]].append({
                        "work_date": str(d),
                        "pct_target_hit": hm["pct_target_hit"],
                    })

        today_shifts = sorted(shifts_by_date.get(work_date, []), key=lambda s: s["member_name"])
        agents = []
        for shift in today_shifts:
            comps = completions_by_shift.get(shift["id"], [])
            metrics = dss_calc.shift_metrics(shift["hours_worked"], comps, base_rate)
            history = member_shift_history[shift["team_member_id"]][:30]
            avg_pct = dss_calc.rolling_avg_pct(history, 7)
            agents.append({
                "member_id": shift["team_member_id"],
                "member_name": shift["member_name"],
                "hours_worked": shift["hours_worked"],
                "target_units": metrics["target_units"],
                "actual_units": metrics["actual_units"],
                "pct_target_hit": metrics["pct_target_hit"],
                "status": metrics["status"],
                "rolling_avg_pct": avg_pct,
            })

        completed_units = completed_by_date.get(work_date, 0.0)
        landed_units = landed_by_date.get(work_date, 0.0)
        team_capacity = sum(s["hours_worked"] * base_rate for s in today_shifts)
        prior_backlog = backlog_by_date.get(
            all_dates[all_dates.index(work_date) - 1], starting_backlog
        ) if work_date in all_dates and all_dates.index(work_date) > 0 else starting_backlog
        rollup = dss_calc.daily_team_rollup(landed_units, completed_units, team_capacity, prior_backlog)

        dow = dss_calc.days_of_work(rollup["running_backlog"], avg_daily_cap)
        sla = dss_calc.sla_status(dow, threshold)

        # SLA history for hiring trigger: last `threshold` dates before today
        prev_dates = [d for d in all_dates if d < work_date][-threshold:]
        sla_history = []
        for pd in prev_dates:
            idx = all_dates.index(pd)
            pb = backlog_by_date.get(all_dates[idx - 1], starting_backlog) if idx > 0 else starting_backlog
            r2 = dss_calc.daily_team_rollup(
                landed_by_date.get(pd, 0.0),
                completed_by_date.get(pd, 0.0),
                0, pb,
            )
            d_w = dss_calc.days_of_work(r2["running_backlog"], avg_daily_cap)
            sla_history.append(dss_calc.sla_status(d_w, threshold))

        trigger = dss_calc.hiring_trigger(sla_history, threshold)

        return jsonify({
            "date": str(work_date),
            "team": {"id": team_id, "name": team["name"]},
            "base_rate": base_rate,
            "agents": agents,
            "rollup": rollup,
            "avg_daily_capacity": round(avg_daily_cap, 2),
            "days_of_work": dow,
            "sla_status": sla,
            "sla_breach_threshold_days": threshold,
            "hiring_trigger": trigger,
            "hiring_trigger_days": len([s for s in sla_history if s == "❌ SLA Breached"]),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# GET /api/dss/entry
# ---------------------------------------------------------------------------
@app.route("/api/dss/entry")
@login_required
def dss_api_entry_get():
    if current_user.role not in ("admin", "team_leader"):
        return jsonify({"error": "Forbidden"}), 403
    if not user_can_see(current_user, "dss"):
        return jsonify({"error": "Forbidden"}), 403

    date_str = request.args.get("date")
    try:
        from datetime import date as date_type
        work_date = date_type.fromisoformat(date_str) if date_str else date_type.today()
    except ValueError:
        return jsonify({"error": "Invalid date"}), 400

    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            team = _dss_get_team(cur)
            if not team:
                conn.close()
                return jsonify({"error": "No team configured"}), 404
            team_id = team["id"]
            base_rate = _dss_get_base_rate(cur, team_id)

            # Task types + sub-types
            cur.execute(
                """SELECT id, name, rate_per_hour, is_base, display_order
                   FROM dss_task_types WHERE team_id = %s AND is_active = TRUE
                   ORDER BY display_order""",
                (team_id,),
            )
            task_types = []
            for tt in cur.fetchall():
                cur.execute(
                    "SELECT id, name, display_order FROM dss_task_sub_types WHERE task_type_id = %s AND is_active = TRUE ORDER BY display_order",
                    (tt["id"],),
                )
                sub_types = [dict(s) for s in cur.fetchall()]
                tt_dict = dict(tt)
                tt_dict["rate_per_hour"] = float(tt_dict["rate_per_hour"])
                tt_dict["sub_types"] = sub_types
                task_types.append(tt_dict)

            # Active members
            cur.execute(
                "SELECT id, name FROM dss_team_members WHERE team_id = %s AND is_active = TRUE ORDER BY name",
                (team_id,),
            )
            members = [dict(m) for m in cur.fetchall()]

            # Existing shifts + completions
            cur.execute(
                "SELECT id, team_member_id, hours_worked, notes FROM dss_daily_shifts WHERE team_id = %s AND work_date = %s",
                (team_id, work_date),
            )
            shifts_raw = cur.fetchall()
            shifts_by_member = {}
            for s in shifts_raw:
                cur.execute(
                    """SELECT task_type_id, task_sub_type_id, count, conversion_factor
                       FROM dss_daily_completions WHERE daily_shift_id = %s""",
                    (s["id"],),
                )
                completions = {}
                for c in cur.fetchall():
                    key = f"{c['task_type_id']}_{c['task_sub_type_id'] or 'null'}"
                    completions[key] = {"count": c["count"], "conversion_factor": float(c["conversion_factor"])}
                shifts_by_member[s["team_member_id"]] = {
                    "shift_id": s["id"],
                    "hours_worked": float(s["hours_worked"]),
                    "notes": s["notes"] or "",
                    "completions": completions,
                }

            # Landings for this date
            cur.execute(
                "SELECT task_type_id, count FROM dss_daily_landings WHERE team_id = %s AND work_date = %s",
                (team_id, work_date),
            )
            landings = {r["task_type_id"]: r["count"] for r in cur.fetchall()}

        conn.close()
        return jsonify({
            "date": str(work_date),
            "team_id": team_id,
            "base_rate": base_rate,
            "task_types": task_types,
            "members": members,
            "shifts": shifts_by_member,
            "landings": landings,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# POST /api/dss/entry
# ---------------------------------------------------------------------------
@app.route("/api/dss/entry", methods=["POST"])
@login_required
def dss_api_entry_post():
    if current_user.role not in ("admin", "team_leader"):
        return jsonify({"error": "Forbidden"}), 403
    if not user_can_see(current_user, "dss"):
        return jsonify({"error": "Forbidden"}), 403

    data = request.get_json() or {}
    date_str = data.get("date")
    agents = data.get("agents", [])
    landings_input = data.get("landings", [])

    try:
        from datetime import date as date_type
        work_date = date_type.fromisoformat(date_str) if date_str else date_type.today()
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid date"}), 400

    conn = None
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            team = _dss_get_team(cur)
            if not team:
                conn.close()
                return jsonify({"error": "No team configured"}), 404
            team_id = team["id"]
            base_rate = _dss_get_base_rate(cur, team_id)

            # Upsert shifts + completions in a single transaction
            for agent in agents:
                member_id = agent.get("member_id")
                hours_worked = float(agent.get("hours_worked", 0))
                notes = agent.get("notes", "") or ""
                completions = agent.get("completions", [])

                # UPSERT shift
                cur.execute(
                    """INSERT INTO dss_daily_shifts
                       (team_id, team_member_id, work_date, hours_worked, notes, updated_at)
                       VALUES (%s, %s, %s, %s, %s, NOW())
                       ON CONFLICT (team_member_id, work_date)
                       DO UPDATE SET hours_worked = EXCLUDED.hours_worked,
                                     notes = EXCLUDED.notes,
                                     updated_at = NOW()
                       RETURNING id""",
                    (team_id, member_id, work_date, hours_worked, notes),
                )
                shift_id = cur.fetchone()["id"]

                # Delete old completions
                cur.execute("DELETE FROM dss_daily_completions WHERE daily_shift_id = %s", (shift_id,))

                # Insert new completions with snapshotted conversion_factor
                for comp in completions:
                    task_type_id = comp.get("task_type_id")
                    sub_type_id = comp.get("sub_type_id") or None
                    count = int(comp.get("count", 0))
                    if count <= 0:
                        continue

                    # Look up current task rate to snapshot conversion_factor
                    cur.execute(
                        "SELECT rate_per_hour FROM dss_task_types WHERE id = %s",
                        (task_type_id,),
                    )
                    tt_row = cur.fetchone()
                    if not tt_row:
                        continue
                    task_rate = float(tt_row["rate_per_hour"])
                    cf = dss_calc.conversion_factor(task_rate, base_rate)

                    cur.execute(
                        """INSERT INTO dss_daily_completions
                           (daily_shift_id, task_type_id, task_sub_type_id, count, conversion_factor,
                            equivalent_units)
                           VALUES (%s, %s, %s, %s, %s, %s)""",
                        (shift_id, task_type_id, sub_type_id, count, cf, count * cf),
                    )

            # UPSERT landings
            for landing in landings_input:
                task_type_id = landing.get("task_type_id")
                count = int(landing.get("count", 0))
                cur.execute(
                    """INSERT INTO dss_daily_landings (team_id, work_date, task_type_id, count, updated_at)
                       VALUES (%s, %s, %s, %s, NOW())
                       ON CONFLICT (team_id, work_date, task_type_id)
                       DO UPDATE SET count = EXCLUDED.count, updated_at = NOW()""",
                    (team_id, work_date, task_type_id, count),
                )

        conn.commit()
        conn.close()

        # ── Maintain daily_team_rollup for this work_date ────────────────────
        try:
            conn2 = get_db_conn()
            with conn2.cursor(cursor_factory=RealDictCursor) as cur2:
                team2 = _dss_get_team(cur2)
                if team2:
                    team_id2 = team2["id"]
                    base_rate2 = _dss_get_base_rate(cur2, team_id2)
                    cur2.execute(
                        "SELECT starting_backlog_units FROM dss_team_settings WHERE team_id = %s",
                        (team_id2,),
                    )
                    settings2 = cur2.fetchone()
                    starting_backlog2 = float(settings2["starting_backlog_units"]) if settings2 else 0.0

                    shift_rows2, landing_rows2 = _dss_bulk_load(cur2, team_id2, work_date)
                    series2 = _dss_build_series(shift_rows2, landing_rows2, base_rate2, starting_backlog2)

                    all_dates2 = series2["all_dates"]
                    backlog_by_date2 = series2["backlog_by_date"]
                    completed_by_date2 = series2["completed_by_date"]
                    landed_by_date2 = series2["landed_by_date"]
                    shifts_by_date2 = series2["shifts_by_date"]
                    completions_by_shift2 = series2["completions_by_shift"]

                    if work_date in all_dates2:
                        idx2 = all_dates2.index(work_date)
                        pb2 = backlog_by_date2.get(all_dates2[idx2 - 1], starting_backlog2) if idx2 > 0 else starting_backlog2
                        landed2 = landed_by_date2.get(work_date, 0.0)
                        completed2 = completed_by_date2.get(work_date, 0.0)
                        today_shifts2 = shifts_by_date2.get(work_date, [])
                        hours_total2 = sum(s["hours_worked"] for s in today_shifts2)
                        team_cap2 = hours_total2 * base_rate2
                        rollup2 = dss_calc.daily_team_rollup(landed2, completed2, team_cap2, pb2)

                        # Count agents below target
                        below_count2 = 0
                        for s2 in today_shifts2:
                            if s2["hours_worked"] > 0:
                                comps2 = completions_by_shift2.get(s2["id"], [])
                                m2 = dss_calc.shift_metrics(s2["hours_worked"], comps2, base_rate2)
                                if m2.get("status") == "Below Target":
                                    below_count2 += 1

                        cur2.execute(
                            """INSERT INTO dss_daily_team_rollups
                               (team_id, work_date, hours_worked_total, actual_units_total,
                                landed_units_total, running_backlog_units, sla_status,
                                agents_below_target_count, updated_at)
                               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                               ON CONFLICT (team_id, work_date) DO UPDATE SET
                                   hours_worked_total = EXCLUDED.hours_worked_total,
                                   actual_units_total = EXCLUDED.actual_units_total,
                                   landed_units_total = EXCLUDED.landed_units_total,
                                   running_backlog_units = EXCLUDED.running_backlog_units,
                                   sla_status = EXCLUDED.sla_status,
                                   agents_below_target_count = EXCLUDED.agents_below_target_count,
                                   updated_at = NOW()""",
                            (
                                team_id2, work_date,
                                round(hours_total2, 2),
                                round(rollup2.get("completed_units", completed2), 4),
                                round(landed2, 4),
                                round(rollup2["running_backlog"], 4),
                                rollup2.get("sla_status", ""),
                                below_count2,
                            ),
                        )
                    conn2.commit()
                    # Bust cache for this work_date
                    _bust_perf_cache(team_id2, work_date)
            conn2.close()
        except Exception as rollup_err:
            logger.warning("Rollup maintenance failed: %s", rollup_err)

        # Return updated grid data
        return dss_api_entry_get()
    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# GET /api/dss/history
# ---------------------------------------------------------------------------
@app.route("/api/dss/history")
@login_required
def dss_api_history():
    if current_user.role not in ("admin", "team_leader"):
        return jsonify({"error": "Forbidden"}), 403
    if not user_can_see(current_user, "dss"):
        return jsonify({"error": "Forbidden"}), 403

    try:
        page = int(request.args.get("page", 1))
        per_page = int(request.args.get("per_page", 30))
    except ValueError:
        page, per_page = 1, 30

    offset = (page - 1) * per_page

    try:
        from datetime import date as date_type
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            team = _dss_get_team(cur)
            if not team:
                conn.close()
                return jsonify({"error": "No team configured"}), 404
            team_id = team["id"]
            base_rate = _dss_get_base_rate(cur, team_id)

            cur.execute("SELECT sla_breach_threshold_days, starting_backlog_units FROM dss_team_settings WHERE team_id = %s", (team_id,))
            settings_row = cur.fetchone()
            threshold = settings_row["sla_breach_threshold_days"] if settings_row else 3
            starting_backlog = float(settings_row["starting_backlog_units"]) if settings_row else 0.0

            cur.execute(
                """SELECT work_date, SUM(hours_worked) AS total_hours
                   FROM dss_daily_shifts WHERE team_id = %s AND hours_worked > 0
                   GROUP BY work_date HAVING SUM(hours_worked) > 0
                   ORDER BY work_date DESC LIMIT 7""",
                (team_id,),
            )
            cap_rows = cur.fetchall()

            # Two queries load all history
            today = date_type.today()
            shift_rows, landing_rows = _dss_bulk_load(cur, team_id, today)

        conn.close()

        avg_daily_cap = (sum(float(r["total_hours"]) * base_rate for r in cap_rows) / len(cap_rows)) if cap_rows else 0

        series = _dss_build_series(shift_rows, landing_rows, base_rate, starting_backlog)
        shifts_by_date = series["shifts_by_date"]
        landed_by_date = series["landed_by_date"]
        completed_by_date = series["completed_by_date"]
        backlog_by_date = series["backlog_by_date"]
        all_dates_asc = series["all_dates"]

        # Pagination (newest first)
        all_dates_desc = list(reversed(all_dates_asc))
        total = len(all_dates_desc)
        page_dates = all_dates_desc[offset:offset + per_page]

        rows = []
        for d in page_dates:
            day_shifts = shifts_by_date.get(d, [])
            agent_count = len(day_shifts)
            total_hours = sum(s["hours_worked"] for s in day_shifts)

            idx = all_dates_asc.index(d)
            prior_backlog = backlog_by_date.get(all_dates_asc[idx - 1], starting_backlog) if idx > 0 else starting_backlog
            rollup = dss_calc.daily_team_rollup(
                landed_by_date.get(d, 0.0),
                completed_by_date.get(d, 0.0),
                total_hours * base_rate,
                prior_backlog,
            )
            dow = dss_calc.days_of_work(rollup["running_backlog"], avg_daily_cap)
            sla = dss_calc.sla_status(dow, threshold)

            rows.append({
                "work_date": str(d),
                "agent_count": agent_count,
                "total_hours": total_hours,
                "completed_units": rollup["completed_units"],
                "landed_units": rollup["landed_units"],
                "backlog_change": rollup["backlog_change"],
                "running_backlog": rollup["running_backlog"],
                "sla_status": sla,
                "days_of_work": dow,
            })

        return jsonify({
            "dates": rows,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page if per_page else 1,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# GET /api/dss/settings/members
# ---------------------------------------------------------------------------
@app.route("/api/dss/settings/members")
@login_required
def dss_settings_members_get():
    if current_user.role not in ("admin", "team_leader"):
        return jsonify({"error": "Forbidden"}), 403
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            team = _dss_get_team(cur)
            if not team:
                conn.close()
                return jsonify([])
            cur.execute(
                "SELECT id, name, is_active, created_at FROM dss_team_members WHERE team_id = %s ORDER BY name",
                (team["id"],),
            )
            rows = cur.fetchall()
        conn.close()
        result = []
        for r in rows:
            d = dict(r)
            d["created_at"] = d["created_at"].isoformat() if d.get("created_at") else None
            result.append(d)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# POST /api/dss/settings/members
# ---------------------------------------------------------------------------
@app.route("/api/dss/settings/members", methods=["POST"])
@login_required
def dss_settings_members_post():
    if current_user.role != "admin":
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            team = _dss_get_team(cur)
            if not team:
                conn.close()
                return jsonify({"error": "No team configured"}), 404
            cur.execute(
                "INSERT INTO dss_team_members (team_id, name) VALUES (%s, %s) RETURNING id, name, is_active",
                (team["id"], name),
            )
            row = cur.fetchone()
        conn.commit()
        conn.close()
        return jsonify(dict(row)), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# PUT /api/dss/settings/members/<id>
# ---------------------------------------------------------------------------
@app.route("/api/dss/settings/members/<int:member_id>", methods=["PUT"])
@login_required
def dss_settings_members_put(member_id):
    if current_user.role != "admin":
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json() or {}
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if "name" in data:
                cur.execute("UPDATE dss_team_members SET name = %s, updated_at = NOW() WHERE id = %s", (data["name"], member_id))
            if "is_active" in data:
                cur.execute("UPDATE dss_team_members SET is_active = %s, updated_at = NOW() WHERE id = %s", (data["is_active"], member_id))
            cur.execute("SELECT id, name, is_active FROM dss_team_members WHERE id = %s", (member_id,))
            row = cur.fetchone()
        conn.commit()
        conn.close()
        return jsonify(dict(row)) if row else (jsonify({"error": "Not found"}), 404)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# GET /api/dss/settings/task-types
# ---------------------------------------------------------------------------
@app.route("/api/dss/settings/task-types")
@login_required
def dss_settings_task_types_get():
    if current_user.role not in ("admin", "team_leader"):
        return jsonify({"error": "Forbidden"}), 403
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            team = _dss_get_team(cur)
            if not team:
                conn.close()
                return jsonify([])
            cur.execute(
                "SELECT id, name, rate_per_hour, is_base, display_order, is_active FROM dss_task_types WHERE team_id = %s ORDER BY display_order",
                (team["id"],),
            )
            task_types = []
            for tt in cur.fetchall():
                cur.execute(
                    "SELECT id, name, display_order, is_active FROM dss_task_sub_types WHERE task_type_id = %s ORDER BY display_order",
                    (tt["id"],),
                )
                tt_dict = dict(tt)
                tt_dict["rate_per_hour"] = float(tt_dict["rate_per_hour"])
                tt_dict["sub_types"] = [dict(s) for s in cur.fetchall()]
                task_types.append(tt_dict)
        conn.close()
        return jsonify(task_types)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# POST /api/dss/settings/task-types
# ---------------------------------------------------------------------------
@app.route("/api/dss/settings/task-types", methods=["POST"])
@login_required
def dss_settings_task_types_post():
    if current_user.role != "admin":
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    try:
        rate = float(data.get("rate_per_hour", 0))
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid rate_per_hour"}), 400
    display_order = int(data.get("display_order", 0))
    if not name or rate <= 0:
        return jsonify({"error": "name and rate_per_hour required"}), 400
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            team = _dss_get_team(cur)
            if not team:
                conn.close()
                return jsonify({"error": "No team configured"}), 404
            cur.execute(
                """INSERT INTO dss_task_types (team_id, name, rate_per_hour, display_order)
                   VALUES (%s, %s, %s, %s) RETURNING id, name, rate_per_hour, is_base, display_order, is_active""",
                (team["id"], name, rate, display_order),
            )
            row = dict(cur.fetchone())
            row["rate_per_hour"] = float(row["rate_per_hour"])
            row["sub_types"] = []
        conn.commit()
        conn.close()
        return jsonify(row), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# PUT /api/dss/settings/task-types/<id>
# ---------------------------------------------------------------------------
@app.route("/api/dss/settings/task-types/<int:task_type_id>", methods=["PUT"])
@login_required
def dss_settings_task_types_put(task_type_id):
    if current_user.role != "admin":
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json() or {}
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if "name" in data:
                cur.execute("UPDATE dss_task_types SET name = %s, updated_at = NOW() WHERE id = %s", (data["name"], task_type_id))
            if "rate_per_hour" in data:
                cur.execute("UPDATE dss_task_types SET rate_per_hour = %s, updated_at = NOW() WHERE id = %s", (float(data["rate_per_hour"]), task_type_id))
            if "display_order" in data:
                cur.execute("UPDATE dss_task_types SET display_order = %s, updated_at = NOW() WHERE id = %s", (int(data["display_order"]), task_type_id))
            if "is_active" in data:
                cur.execute("UPDATE dss_task_types SET is_active = %s, updated_at = NOW() WHERE id = %s", (bool(data["is_active"]), task_type_id))
            if data.get("is_base"):
                # Unset is_base on all others first
                cur.execute("SELECT team_id FROM dss_task_types WHERE id = %s", (task_type_id,))
                team_row = cur.fetchone()
                if team_row:
                    cur.execute("UPDATE dss_task_types SET is_base = FALSE WHERE team_id = %s AND id != %s", (team_row["team_id"], task_type_id))
                cur.execute("UPDATE dss_task_types SET is_base = TRUE, updated_at = NOW() WHERE id = %s", (task_type_id,))
            cur.execute(
                "SELECT id, name, rate_per_hour, is_base, display_order, is_active FROM dss_task_types WHERE id = %s",
                (task_type_id,),
            )
            row = cur.fetchone()
        conn.commit()
        conn.close()
        if not row:
            return jsonify({"error": "Not found"}), 404
        d = dict(row)
        d["rate_per_hour"] = float(d["rate_per_hour"])
        return jsonify(d)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# POST /api/dss/settings/task-types/<id>/sub-types
# ---------------------------------------------------------------------------
@app.route("/api/dss/settings/task-types/<int:task_type_id>/sub-types", methods=["POST"])
@login_required
def dss_settings_sub_types_post(task_type_id):
    if current_user.role != "admin":
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    display_order = int(data.get("display_order", 0))
    if not name:
        return jsonify({"error": "name is required"}), 400
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "INSERT INTO dss_task_sub_types (task_type_id, name, display_order) VALUES (%s, %s, %s) RETURNING id, name, display_order, is_active",
                (task_type_id, name, display_order),
            )
            row = cur.fetchone()
        conn.commit()
        conn.close()
        return jsonify(dict(row)), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# PUT /api/dss/settings/sub-types/<id>
# ---------------------------------------------------------------------------
@app.route("/api/dss/settings/sub-types/<int:sub_type_id>", methods=["PUT"])
@login_required
def dss_settings_sub_types_put(sub_type_id):
    if current_user.role != "admin":
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json() or {}
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if "name" in data:
                cur.execute("UPDATE dss_task_sub_types SET name = %s, updated_at = NOW() WHERE id = %s", (data["name"], sub_type_id))
            if "display_order" in data:
                cur.execute("UPDATE dss_task_sub_types SET display_order = %s, updated_at = NOW() WHERE id = %s", (int(data["display_order"]), sub_type_id))
            if "is_active" in data:
                cur.execute("UPDATE dss_task_sub_types SET is_active = %s, updated_at = NOW() WHERE id = %s", (bool(data["is_active"]), sub_type_id))
            cur.execute("SELECT id, name, display_order, is_active FROM dss_task_sub_types WHERE id = %s", (sub_type_id,))
            row = cur.fetchone()
        conn.commit()
        conn.close()
        return jsonify(dict(row)) if row else (jsonify({"error": "Not found"}), 404)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# GET /api/dss/settings/team
# ---------------------------------------------------------------------------
@app.route("/api/dss/settings/team")
@login_required
def dss_settings_team_get():
    if current_user.role not in ("admin", "team_leader"):
        return jsonify({"error": "Forbidden"}), 403
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            team = _dss_get_team(cur)
            if not team:
                conn.close()
                return jsonify({"error": "No team configured"}), 404
            cur.execute("SELECT * FROM dss_team_settings WHERE team_id = %s", (team["id"],))
            row = cur.fetchone()
        conn.close()
        if not row:
            return jsonify({"team_id": team["id"], "starting_backlog_units": 0, "sla_breach_threshold_days": 3})
        d = dict(row)
        d["starting_backlog_units"] = float(d["starting_backlog_units"])
        return jsonify(d)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# PUT /api/dss/settings/team
# ---------------------------------------------------------------------------
@app.route("/api/dss/settings/team", methods=["PUT"])
@login_required
def dss_settings_team_put():
    if current_user.role != "admin":
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json() or {}
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            team = _dss_get_team(cur)
            if not team:
                conn.close()
                return jsonify({"error": "No team configured"}), 404
            team_id = team["id"]
            cur.execute(
                """INSERT INTO dss_team_settings (team_id, starting_backlog_units, sla_breach_threshold_days)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (team_id) DO UPDATE
                   SET starting_backlog_units = EXCLUDED.starting_backlog_units,
                       sla_breach_threshold_days = EXCLUDED.sla_breach_threshold_days,
                       updated_at = NOW()
                   RETURNING *""",
                (team_id, float(data.get("starting_backlog_units", 0)), int(data.get("sla_breach_threshold_days", 3))),
            )
            row = cur.fetchone()
        conn.commit()
        conn.close()
        d = dict(row)
        d["starting_backlog_units"] = float(d["starting_backlog_units"])
        return jsonify(d)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# In-memory cache for agent performance (historic ranges only)
# ---------------------------------------------------------------------------
_perf_cache = {}  # key: (team_id, start_date_str, end_date_str) -> {"data": ..., "expires": datetime}


def _bust_perf_cache(team_id, work_date):
    """Remove any cached entry whose date range includes work_date."""
    from datetime import date as _date
    wd = work_date if isinstance(work_date, _date) else _date.fromisoformat(str(work_date))
    keys_to_remove = [
        k for k in list(_perf_cache.keys())
        if k[0] == team_id and _date.fromisoformat(k[1]) <= wd <= _date.fromisoformat(k[2])
    ]
    for k in keys_to_remove:
        del _perf_cache[k]


# ---------------------------------------------------------------------------
# GET /api/dss/agent-performance
# ---------------------------------------------------------------------------
@app.route("/api/dss/agent-performance")
@login_required
def dss_agent_performance_api():
    if current_user.role not in ("admin", "team_leader"):
        return jsonify({"error": "Forbidden"}), 403
    try:
        from datetime import date as _date, datetime as _datetime
        today = _date.today()
        first_of_month = today.replace(day=1).isoformat()
        today_iso = today.isoformat()

        start_date = request.args.get("start_date", first_of_month)
        end_date = request.args.get("end_date", today_iso)

        # Resolve team_id first (needed for cache key)
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            team = _dss_get_team(cur)
            if not team:
                conn.close()
                return jsonify({"error": "No team configured"}), 404
            team_id = team["id"]

            # Cache check — only for fully historic ranges (end_date < today)
            end_date_obj = _date.fromisoformat(end_date)
            cache_key = (team_id, start_date, end_date)
            if end_date_obj < today:
                entry = _perf_cache.get(cache_key)
                if entry and entry["expires"] > _datetime.utcnow():
                    conn.close()
                    return jsonify(entry["data"])

            cur.execute("""
                SELECT
                    tm.id AS member_id,
                    tm.name AS member_name,
                    tm.is_active,
                    COUNT(DISTINCT CASE WHEN ds.hours_worked > 0 THEN ds.work_date END) AS days_worked,
                    COALESCE(SUM(ds.hours_worked), 0) AS total_hours,
                    tt.name AS task_type_name,
                    tst.name AS sub_type_name,
                    COALESCE(SUM(dc.count), 0) AS total_count
                FROM dss_team_members tm
                LEFT JOIN dss_daily_shifts ds
                    ON ds.team_member_id = tm.id
                    AND ds.work_date BETWEEN %s AND %s
                LEFT JOIN dss_daily_completions dc ON dc.daily_shift_id = ds.id
                LEFT JOIN dss_task_types tt ON tt.id = dc.task_type_id
                LEFT JOIN dss_task_sub_types tst ON tst.id = dc.task_sub_type_id
                WHERE tm.team_id = %s
                GROUP BY tm.id, tm.name, tm.is_active, tt.name, tst.name
                ORDER BY tm.name, tt.name, tst.name
            """, (start_date, end_date, team_id))
            rows = cur.fetchall()
        conn.close()

        # Pivot rows into one dict per agent
        agents_map = {}
        for row in rows:
            mid = row["member_id"]
            if mid not in agents_map:
                agents_map[mid] = {
                    "member_id": mid,
                    "member_name": row["member_name"],
                    "is_active": row["is_active"],
                    "days": int(row["days_worked"] or 0),
                    "hours": float(row["total_hours"] or 0),
                    "emails": 0,
                    "dnp": 0,
                    "offers": 0,
                    "transfer": 0,
                    "bals": 0,
                    "rev_bals": 0,
                    "out": 0,
                    "in_calls": 0,
                    "s_sheets": 0,
                    "tac": 0,
                    "un_alloc": 0,
                    "returns": 0,
                    "packs_poi": 0,
                    "ie_review_appts": 0,
                }
            else:
                # Update days/hours from later rows if needed (should be consistent per member)
                agents_map[mid]["days"] = max(agents_map[mid]["days"], int(row["days_worked"] or 0))
                agents_map[mid]["hours"] = max(agents_map[mid]["hours"], float(row["total_hours"] or 0))

            tt = row["task_type_name"]
            tst = row["sub_type_name"]
            cnt = int(row["total_count"] or 0)

            if tt == "Creditor Emails":
                agents_map[mid]["emails"] += cnt
            elif tt == "DocuWare":
                if tst == "Offers":
                    agents_map[mid]["offers"] += cnt
                elif tst == "Transfer":
                    agents_map[mid]["transfer"] += cnt
                elif tst == "Balances":
                    agents_map[mid]["bals"] += cnt
            elif tt == "Reviews":
                agents_map[mid]["rev_bals"] += cnt
            elif tt == "Spreadsheet":
                agents_map[mid]["s_sheets"] += cnt
            elif tt == "Packs/POI":
                agents_map[mid]["packs_poi"] += cnt
            elif tt == "I&E Review Appts":
                agents_map[mid]["ie_review_appts"] += cnt

        numeric_keys = ["days", "hours", "emails", "dnp", "offers", "transfer", "bals",
                        "rev_bals", "out", "in_calls", "s_sheets", "tac", "un_alloc",
                        "returns", "packs_poi", "ie_review_appts"]

        # Filter: active members always included; inactive only if they have shifts in range
        agents_list = []
        for agent in agents_map.values():
            if not agent["is_active"] and agent["days"] == 0:
                continue
            agents_list.append(agent)

        # Sort by name
        agents_list.sort(key=lambda a: a["member_name"])

        # Totals
        totals = {k: 0 for k in numeric_keys}
        for agent in agents_list:
            for k in numeric_keys:
                totals[k] += agent[k]
        totals["hours"] = round(totals["hours"], 1)

        # Round hours per agent
        for agent in agents_list:
            agent["hours"] = round(agent["hours"], 1)

        result = {
            "start_date": start_date,
            "end_date": end_date,
            "agents": agents_list,
            "totals": totals,
        }

        # Store in cache only for fully historic ranges (TTL: 1 hour)
        if end_date_obj < today:
            from datetime import timedelta as _timedelta
            _perf_cache[cache_key] = {
                "data": result,
                "expires": _datetime.utcnow() + _timedelta(hours=1),
            }

        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG", "false").lower() == "true")
