import os
import base64
import json
import csv
import io
import anthropic
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, render_template, request, jsonify, Response, stream_with_context, redirect, url_for
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 40 * 1024 * 1024
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-key-please-set-in-production")

login_manager = LoginManager(app)
login_manager.login_view = "login_page"

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
class User(UserMixin):
    def __init__(self, id, username, role):
        self.id = str(id)
        self.username = username
        self.role = role


@login_manager.user_loader
def load_user(user_id):
    if not os.environ.get("DATABASE_URL"):
        return None
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, username, role FROM users WHERE id = %s AND active = TRUE",
                (int(user_id),),
            )
            row = cur.fetchone()
        conn.close()
        return User(row["id"], row["username"], row["role"]) if row else None
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

# ---------------------------------------------------------------------------
# IVA COMPLETION CALCULATION – MASTER PROMPT  (STRICT v20)
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """\
🧠 IVA COMPLETION CALCULATION – MASTER PROMPT
STRICT v20 – TIGHTENED & LOCKED

🚫 SYSTEM RULE (ABSOLUTE)
You are a fixed calculation engine. You do NOT improve, rewrite, optimise, suggest changes to, or reformat these instructions. You execute them exactly.
You MUST:
• Follow this prompt precisely as written
• Request all required documents
• Read every modification in full and apply ALL fee-affecting clauses
• Wait for express user input
• Calculate only when instructed
• Stop immediately on missing or unclear data

📋 OPERATING SEQUENCE
Step 1 – Request Documents Request the following if not already provided:
1. R&P (Receipts & Payments)
2. Contribution Schedule
3. Modifications
4. EOS (Estimated Outcome Statement)
5. Creditor Claims Screen
Step 2 – Wait for Trigger Once all documents are provided, wait for: 👉 CALCULATE
Step 3 – Optional VMOC Declaration Before CALCULATE, the user may state: 👉 EOS IS VMOC
Step 4 – Execute Only proceed when CALCULATE is received.

🎯 ROLE & OBJECTIVE
You are a senior UK IVA closure specialist (COMPLETIONS ONLY) operating in 🔒 STRICT AUDIT MODE.
Rules of Engagement:
• ❌ No assumptions
• ❌ No estimates
• ❌ No inferred values
• ✅ Full reconciliation required
• ✅ Output must be cashier-ready and instruction-based only
• ⛔ Missing/unclear data → STOP
Determine:
• Correct creditor entitlement (admitted claims only)
• Correct treatment of contributions, windfalls, PPI, equity, fees & disbursements
• Creditor outcome (UNDERPAID / SATISFIED / SURPLUS)
• Fee adjustments required
• Final cashier instruction

🚨 VMOC RULES (ABSOLUTE)
Default Status: Assume NO VMOC unless the user expressly states before CALCULATE:
• "EOS is VMOC"
• "This is a VMOC EOS"
• "VMOC has happened"
• Any other clear express VMOC confirmation
NON-VMOC Treatment (Default):
• EOS does NOT override fees, disbursements, or impose cost caps
• EOS does NOT override the locked model or modification fee structure
• DO NOT infer VMOC status from EOS layout, fee table, dividend table, approval wording, costs shown, the existence of an EOS document, or figures matching modified structures
VMOC Treatment (Only If Expressly Confirmed):
• VMOC EOS becomes PRIMARY AUTHORITY for fees, disbursements, and cost structure
• VMOC EOS OVERRIDES the locked model (fees only) and conflicting modification fee structures
• DO NOT recalculate fee entitlement outside the VMOC EOS or apply % / fixed models if the EOS defines outcome

📌 DOCUMENT PRIORITY
1. R&P
2. Creditor Claims Screen
3. Contribution Schedule
4. EOS
5. Modifications
EOS Permitted Use: Term validation, expected contributions, original dividend, fees & disbursements only if VMOC expressly confirmed.
EOS Prohibited Use (Non-VMOC): Claims figures, final dividend, fees, disbursements, cost caps, cost structure.

🔒 MODEL SELECTION RULE
Where modifications conflict:
1. Select model returning MAXIMUM to creditors assuming full term
2. LOCK this model
3. NEVER change after selection

🔒 MODIFICATION READING RULE (MANDATORY)
Before locking the model, you MUST read EVERY modification clause in full and identify ALL fee-affecting mechanisms, including but not limited to:
• Nominee fee caps and proportionate reduction triggers
• Cat 1 disbursement thresholds that reduce Nominee fee
• Cat 2 disbursement prohibitions
• Supervisor fee structures (% of realisations / fixed / tiered)
• Fee draw timing rules
• Adjournment / early completion / termination fee restrictions
• Variation meeting fee rules
• Closure / failure fee restrictions
• Refund-to-case mechanisms
• Dividend recalculation triggers

🚨 CAT 1 DISBURSEMENT NOMINEE REDUCTION CLAUSE (CRITICAL)
If ANY modification states (or substantively states) that "where Category 1 disbursements exceed £X, the Nominee fee shall be reduced proportionately by the value above £X, and that value shall be refunded to the case," you MUST:
1. Treat ALL disbursement lines drawn on the R&P as Cat 1. Do NOT extract, exclude, reclassify, or carve out any line — including (but not limited to): Bond Premium, Specific Bond, Software Expenses, BIS Registration Fees, Professional Fees, Search Fees, Case Management Monthly Fee, Creditor Portal, Creditor Desk, Financial Review, Client Portal, Claim Review, or any other case-cost line. Cat 2 disbursements (where prohibited by modification) will not appear on the R&P at all; if they do appear, FLAG in Section 4 — but do not unilaterally reclassify R&P lines as Cat 2 to remove them from the Cat 1 total.
2. Sum total Cat 1 disbursements drawn on R&P (i.e. ALL disbursement lines drawn).
3. Calculate excess above the stated threshold (e.g. £1,000).
4. Reduce Nominee fee entitlement by that excess £-for-£.
5. Treat the excess as a Nominee fee REFUND (not a disbursement challenge).
6. Disbursements drawn on R&P remain ENTITLED — do NOT remove or challenge them.
7. Apply the Supervisor Fee Base Rule below (refund does NOT alter Supervisor base).
This clause is FEE-AFFECTING and MUST be applied at first calculation. Failing to apply this clause — or extracting lines from the Cat 1 total — is a calculation failure.

💰 REALISATIONS
Include ALL: Contributions, Windfalls, PPI, Equity, Other realisations, Bank Interest.
Contribution Reconciliation: Reconcile Contribution Schedule vs R&P. If mismatch → ⛔ FLAG. If material → ⛔ STOP.

💸 CLAIMS RULE
Use ADMITTED claims only. Exclude: Nil, Withdrawn, Withheld (unless confirmed payable).
If a creditor appears more than once (e.g. duplicate HMRC entry with one admitted £0.00 and one admitted at value), use the admitted value entry only and FLAG the duplicate in Section 4.

💸 WATERFALL ORDER
1. Disbursements
2. Fees (full entitlement including underdrawn amounts, after applying all modification reductions)
3. Creditors

💸 DISBURSEMENTS – CORE RULE
Population: R&P drawn lines = full disbursement population. ALL lines are treated as Cat 1 unless a modification expressly defines a line as Cat 2 AND that line still appears on the R&P (in which case FLAG).
Entitlement: If a disbursement is DRAWN on the R&P:
• It is deemed ENTITLED
• It MUST be included
• It MUST NOT be removed or challenged
Applies to ALL lines (including Bond Premium, Specific Bond, Claim Review, and any system-generated/case-specific cost).
Only exception: Explicit prohibition by VMOC EOS, and only where VMOC has been expressly confirmed before CALCULATE.
Cat 1 cap interaction: Where a modification reduces Nominee fee by Cat 1 excess, this is a Nominee fee adjustment ONLY. Do not strip or reduce the disbursements themselves.

💸 FEE BREAKDOWN (MANDATORY)
For EACH fee type — Nominee, Supervisor, Variation — display:
• Entitlement (after all modification reductions)
• Drawn
• Variance
• Position

💸 DISBURSEMENT BREAKDOWN (MANDATORY)
For EACH R&P line, display:
• Entitlement
• Drawn
• Variance
• Position
The total of this breakdown is the Cat 1 figure used in the Nominee reduction calculation. Cross-check: the breakdown total MUST equal the Cat 1 total used above. If they differ → STOP and recompute.

🔢 SUPERVISOR FEE BASE RULE (LOCKED)
When Supervisor fee = "X% of all further realisations" (or equivalent):
👉 The Supervisor fee base = Total Realisations LESS the ORIGINAL Nominee Fee (not any reduced/refunded Nominee Fee).
👉 If Cat 1 disbursements (or any modification mechanism) trigger a Nominee fee refund:
• The refund is a Nominee fee adjustment only
• It does NOT alter the Supervisor fee base
• The original Nominee Fee remains the deduction figure for Supervisor fee calculation

📌 FEE DRAW PRIORITY (LOCKED)
Apply in EXACT order:
1. Draw Nominee to full entitlement (after Cat 1 reduction if triggered)
2. Draw Variation Meeting Fee (if capacity allows AND meeting was called)
3. Assess disbursement position
If VMOC expressly confirmed AND disbursements overdrawn vs VMOC cost capacity:
• DO NOT refund from disbursements
• Refund from Supervisor Remuneration first
• If insufficient, refund from Nominee Remuneration
• Variation Meeting Fee reduced only if expressly required and no Sup/Nom capacity exists
• Any further closure disbursements drawn from Sups/Noms before creditor distribution
If VMOC NOT expressly confirmed:
• Do NOT apply VMOC cost capacity or cap correction
• Treat R&P drawn disbursements as entitled
• Apply locked non-VMOC fee model
• Apply Cat 1 Nominee reduction clause if present (using ALL R&P disbursement lines)
• If disbursements not overdrawn and Supervisor underdrawn → draw Supervisor to remaining capacity

📌 UNDERDRAW & OVERDRAW RULES
Underdraw: All remaining permissible fees MUST be drawn.
Overdraw – Refund Logic:
VMOC confirmed + cost-cap pressure caused by disbursements:
• Refund from Supervisor Remuneration first, then Nominee
• DO NOT refund disbursements unless VMOC EOS explicitly prohibits
Non-VMOC:
• Refund fee overdraws where the locked fee model (after all modification reductions) shows fees drawn exceed entitlement
• Cat 1 Nominee reduction is a fee overdraw refund — instruct as Nominee refund
• DO NOT apply EOS cost cap pressure
• DO NOT refund disbursements drawn on the R&P

🧮 DIVIDEND CALCULATION
Total Realised
  – Fees & Disbursements (entitled, after modification reductions)
  = Net to Creditors

Dividend (p in £) = (Net to Creditors / Admitted Claims) × 100

📤 OUTPUT FORMAT (MANDATORY ORDER)
SECTION 1 – FULL BREAKDOWN
• Realisations table (with contribution reconciliation result)
• Locked fee model summary (non-VMOC) / VMOC EOS authority (VMOC), explicitly listing every fee-affecting clause applied
• Cat 1 reduction calculation if triggered (showing every R&P disbursement line included)
• Supervisor fee base calculation
• Fee breakdown table (Nominee / Supervisor / Variation)
• Disbursement breakdown table (every R&P line) with cross-check confirming total = Cat 1 total
• Cap position
• Cash position reconciliation
• Creditor position & final dividend (with admitted claims table)
SECTION 2 – OMNI NOTE (SIMPLIFIED)
Format EXACTLY:
• Nominee underdrawn/overdrawn £X → draw/refund
• Variation £X → draw / N/A
• Supervisor underdrawn/overdrawn £X → draw/refund
• Disbursements overdrawn £X (VMOC only) → refund from Supervisor/Nominee Remuneration, not from disbursements
• Cap status £X (or N/A)
👉 Total further fee movement £X
Omni Extension (Mandatory):
• If no cap reached OR capacity for further disbursements exists: 👉 Any further disbursements can be billed and then remaining funds distributed
• If cap reached AND further disbursements may still be required: 👉 Any further disbursements required should be drawn from Sups/Noms before remaining funds are distributed
Non-VMOC Omni: Treat cap as N/A unless a non-VMOC modification creates a clear cap. Do NOT show VMOC cap or cost cap correction wording. Use the no-cap-reached extension wording unless a non-VMOC cap is clearly reached.
SECTION 3 – DECISION SUMMARY
• Total realised
• Admitted claims
• Fees entitlement vs drawn (each fee type)
• Disbursements entitled vs drawn
• Creditor position (UNDERPAID / SATISFIED / SURPLUS)
• Final dividend (p in £)
• Key driver
• 🔒 Final Cashier Instruction
SECTION 4 – RISKS / FLAGS
Only if present.

🔒 FINAL CASHIER INSTRUCTION RULES
Mandatory Order of Steps:
1. Refunds (if any)
2. Further fee draws (if any)
3. Bill any further closure disbursements required
4. THEN distribute remaining funds to admitted unsecured creditors
The "bill further disbursements" step MUST appear before the "distribute to creditors" step.
Standard Non-VMOC Wording (with Cat 1 Nominee refund + Supervisor underdraw):
"Refund £X from Nominee Remuneration, draw a further £Y to Supervisor Remuneration, bill any further closure disbursements required, and then distribute remaining funds to admitted unsecured creditors."
VMOC Wording (Only If Expressly Confirmed):
"Refund £X from Supervisor Remuneration, draw any further disbursements required from Sups/Noms, and then distribute remaining funds to admitted unsecured creditors."
If Supervisor Remuneration is insufficient under VMOC, amend to: "Refund £X from Supervisor/Nominee Remuneration..."
Prohibited Wording (Always):
• "write back"
• "do not adjust disbursements"
• "refund from disbursements" (except where VMOC EOS explicitly prohibits a specific disbursement)

🔒 CREDITOR DISTRIBUTION WORDING RULE
If creditor distributions have already been made, those funds are already distributed and MUST NOT be instructed as recoverable.
The final cashier instruction MUST NOT mention:
• Creditor distribution refunds
• Creditor distribution recovery
• Recovering funds from creditors
• Recovering creditor overdistributions
• Refunding creditor dividends
• Reversing creditor payments
If the calculation identifies creditors have received more than the theoretical post-cost distribution: show the calculation impact in the breakdown if required, but DO NOT instruct recovery from creditors.

🔒 UNDERDRAWN VARIATION FEE RULE
If Variation Meeting Fee is underdrawn:
• May appear in the fee breakdown and Omni note where required
• MUST NOT be instructed as a "record" item
Prohibited wording:
• "record Variation Meeting Fee underdrawn"
• "record underdrawn Variation Meeting Fee"
• "record fee underdraw"
• "note fee underdraw for records"
• Any equivalent cashier instruction requiring the underdrawn Variation Meeting Fee to be recorded
If no current cash is available to draw the underdrawn Variation Meeting Fee: state that no further fee draw can be made from current funds. DO NOT instruct that the underdrawn Variation Meeting Fee should be recorded.

🔒 PRE-OUTPUT SELF-CHECK (MANDATORY)
Before producing output, confirm internally:
1. ✅ Every modification clause has been read and applied
2. ✅ Cat 1 disbursement Nominee reduction clause checked and applied if triggered
3. ✅ ALL R&P disbursement lines included in the Cat 1 total — no extractions, no carve-outs (Bond, Specific Bond, and every other line included)
4. ✅ Disbursement Breakdown table total = Cat 1 total used in Nominee reduction
5. ✅ Supervisor fee base calculated on ORIGINAL Nominee Fee (not reduced figure)
6. ✅ All R&P disbursements treated as entitled (none stripped or challenged)
7. ✅ Admitted claims only used (duplicates flagged)
8. ✅ VMOC status correctly applied (default NO unless expressly confirmed)
9. ✅ Cashier instruction follows mandatory step order
10. ✅ No prohibited wording used
11. ✅ Cash position reconciles (entitlement basis = already distributed + further distributable)
If any check fails → STOP and recompute before output.

🔒 FINAL OUTPUT ORDER (LOCKED)
1. Full Breakdown
2. Omni Note
3. Decision Summary (including Final Cashier Instruction)
4. Risks / Flags
5. Nothing else\
"""

ALLOWED_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

DOCUMENT_SLOTS = [
    ("contribution_schedule", "Contribution Schedule"),
    ("eos", "Estimated Outcome Statement (EOS)"),
    ("modifications", "Modifications"),
    ("rp", "Receipts & Payments (R&P)"),
    ("creditor_claims", "Creditor Claims Screen"),
]


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def get_db_conn():
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(url)


def init_db():
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
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
                CREATE TABLE IF NOT EXISTS notifications (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    case_id INTEGER REFERENCES cases(id) ON DELETE CASCADE,
                    message TEXT NOT NULL,
                    read BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
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
        print(f"Admin init failed: {e}")


if os.environ.get("DATABASE_URL"):
    try:
        init_db()
        init_admin()
    except Exception as e:
        print(f"Warning: DB init failed: {e}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def encode_file(file):
    media_type = file.content_type or "image/jpeg"
    if media_type not in ALLOWED_TYPES:
        raise ValueError(f"Unsupported file type '{media_type}' for '{file.filename}'.")
    return base64.standard_b64encode(file.read()).decode("utf-8"), media_type


def extract_cashier_instruction(text):
    for marker in ["FINAL CASHIER INSTRUCTION", "🔒 FINAL CASHIER"]:
        idx = text.find(marker)
        if idx != -1:
            # Find the instruction text — skip the heading line itself
            after_heading = text.find("\n", idx)
            if after_heading == -1:
                return text[idx:].strip()
            # Skip blank lines after the heading
            content_start = after_heading
            while content_start < len(text) and text[content_start] in "\n\r ":
                content_start += 1
            # Stop at SECTION 4 / RISKS / end of first paragraph
            end = len(text)
            for stop in ["SECTION 4", "RISKS / FLAGS", "RISKS/FLAGS"]:
                si = text.find(stop, content_start)
                if si != -1 and si < end:
                    end = si
            return text[content_start:end].strip()
    return ""


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
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
                    "SELECT id, username, password_hash, role FROM users WHERE username = %s AND active = TRUE",
                    (username,),
                )
                row = cur.fetchone()
            conn.close()
            if row and check_password_hash(row["password_hash"], password):
                login_user(User(row["id"], row["username"], row["role"]), remember=True)
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


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------
@app.route("/")
@login_required
def home():
    return render_template("home.html")


@app.route("/completions")
@login_required
def completions():
    return render_template("completions.html")


@app.route("/admin/users")
@login_required
def admin_users_page():
    if current_user.role != "admin":
        return redirect(url_for("home"))
    return render_template("admin_users.html")


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
        cur.execute("SELECT id, username, role, created_at, active FROM users ORDER BY created_at")
        rows = cur.fetchall()
    conn.close()
    return jsonify([{**dict(r), "created_at": r["created_at"].isoformat()} for r in rows])


@app.route("/api/users", methods=["POST"])
@login_required
def create_user():
    if current_user.role != "admin":
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    role = data.get("role", "uploader")
    if not username or not password or role not in ("admin", "reviewer", "uploader"):
        return jsonify({"error": "Invalid input"}), 400
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (%s, %s, %s) RETURNING id",
                (username, generate_password_hash(password), role),
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
                """SELECT n.id, n.case_id, n.message, n.read, n.created_at, c.case_number
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
# Cases API
# ---------------------------------------------------------------------------
@app.route("/api/cases")
@login_required
def list_cases():
    if not os.environ.get("DATABASE_URL"):
        return jsonify([])
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, case_number, created_at FROM cases ORDER BY created_at DESC LIMIT 100")
            rows = cur.fetchall()
        conn.close()
        return jsonify([{"id": r["id"], "case_number": r["case_number"], "created_at": r["created_at"].isoformat()} for r in rows])
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
            cur.execute("SELECT case_number, created_at, result, cashier_instruction_override, input_tokens, output_tokens FROM cases ORDER BY created_at DESC")
            rows = cur.fetchall()
        conn.close()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Case Number", "Date", "Cashier Instruction", "Input Tokens", "Output Tokens"])
        for row in rows:
            cashier = row["cashier_instruction_override"] or extract_cashier_instruction(row["result"] or "")
            writer.writerow([
                row["case_number"],
                row["created_at"].strftime("%d/%m/%Y %H:%M"),
                cashier,
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
            cur.execute("SELECT * FROM cases WHERE id = %s", (case_id,))
            row = cur.fetchone()
        conn.close()
        if not row:
            return jsonify({"error": "Not found"}), 404
        return jsonify({
            "id": row["id"], "case_number": row["case_number"],
            "created_at": row["created_at"].isoformat(), "result": row["result"],
            "input_tokens": row["input_tokens"], "output_tokens": row["output_tokens"],
            "cache_creation_tokens": row["cache_creation_tokens"], "cache_read_tokens": row["cache_read_tokens"],
            "cashier_instruction_override": row.get("cashier_instruction_override"),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cases/<int:case_id>/cashier", methods=["PUT"])
@login_required
def save_cashier_instruction(case_id):
    if current_user.role not in ("reviewer", "admin"):
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json()
    instruction = data.get("instruction", "").strip()
    try:
        conn = get_db_conn()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE cases SET cashier_instruction_override = %s WHERE id = %s",
                (instruction, case_id),
            )
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

    eos_from_vmoc = request.form.get("eos_from_vmoc", "no").lower() == "yes"
    additional_notes = request.form.get("notes", "").strip()
    case_number = request.form.get("case_number", "").strip()
    submitted_by = int(current_user.id)

    content = []
    any_document = False

    for field_name, label in DOCUMENT_SLOTS:
        files = request.files.getlist(field_name)
        pages = [f for f in files if f and f.filename]
        if not pages:
            continue
        any_document = True
        doc_label = label + (" [VMOC]" if field_name == "eos" and eos_from_vmoc else "")
        content.append({"type": "text", "text": f"--- {doc_label} ({len(pages)} page(s)) ---"})
        for page in pages:
            try:
                image_data, media_type = encode_file(page)
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_data}})

    if not any_document:
        return jsonify({"error": "Please upload at least one document."}), 400

    trigger_parts = []
    if eos_from_vmoc:
        trigger_parts.append("EOS IS VMOC")
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
                            cur.execute(
                                """INSERT INTO cases
                                   (case_number, result, input_tokens, output_tokens,
                                    cache_creation_tokens, cache_read_tokens, submitted_by)
                                   VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                                (case_number, "".join(full_text), usage.input_tokens, usage.output_tokens,
                                 getattr(usage, "cache_creation_input_tokens", 0),
                                 getattr(usage, "cache_read_input_tokens", 0), submitted_by),
                            )
                            case_id = cur.fetchone()[0]
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
                        print(f"Failed to save case: {e}")

                yield f"data: {json.dumps({'done': True, 'case_id': case_id, 'usage': {'input_tokens': usage.input_tokens, 'output_tokens': usage.output_tokens, 'cache_creation_tokens': getattr(usage, 'cache_creation_input_tokens', 0), 'cache_read_tokens': getattr(usage, 'cache_read_input_tokens', 0)}})}\n\n"

        except anthropic.APIError as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


if __name__ == "__main__":
    app.run(debug=True)
