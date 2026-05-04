import os
import base64
import anthropic
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 40 * 1024 * 1024  # 40 MB

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

SYSTEM_PROMPT = (
    "You are an expert IVA (Individual Voluntary Arrangement) case processor. "
    "You will be provided with IVA documents — which may include a Contribution Schedule, "
    "End of Supervision (EOS) statement, Modifications, Receipts & Payments (R&P), "
    "and Creditor Claims. Each document image is labelled with its type. "
    "Analyse the documents carefully and respond to the user's instructions accurately. "
    "Pay close attention to figures, dates, creditor names, and any discrepancies between documents."
)

ALLOWED_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

# Document slots: form field name → human-readable label
DOCUMENT_SLOTS = [
    ("contribution_schedule", "Contribution Schedule"),
    ("eos", "End of Supervision (EOS)"),
    ("modifications", "Modifications"),
    ("rp", "Receipts & Payments (R&P)"),
    ("creditor_claims", "Creditor Claims"),
]


def encode_file(file):
    """Return (base64_data, media_type) or raise ValueError."""
    media_type = file.content_type or "image/jpeg"
    if media_type not in ALLOWED_TYPES:
        raise ValueError(f"Unsupported file type '{media_type}' for '{file.filename}'.")
    return base64.standard_b64encode(file.read()).decode("utf-8"), media_type


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/analyze", methods=["POST"])
def analyze():
    prompt = request.form.get("prompt", "").strip()
    if not prompt:
        return jsonify({"error": "Please provide a prompt."}), 400

    eos_from_vmoc = request.form.get("eos_from_vmoc", "no").lower() == "yes"

    content = []
    any_document = False

    for field_name, label in DOCUMENT_SLOTS:
        files = request.files.getlist(field_name)
        pages = [f for f in files if f and f.filename]
        if not pages:
            continue

        any_document = True

        # Label for this document type (with VMOC note if applicable)
        doc_label = label
        if field_name == "eos" and eos_from_vmoc:
            doc_label += " [sourced from a VMOC]"

        # Separator text so Claude knows what follows
        content.append({
            "type": "text",
            "text": f"--- {doc_label} ({len(pages)} page(s)) ---",
        })

        for page in pages:
            try:
                image_data, media_type = encode_file(page)
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": image_data,
                },
            })

    if not any_document:
        return jsonify({"error": "Please upload at least one document."}), 400

    # User instruction goes last
    content.append({"type": "text", "text": prompt})

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.APIError as e:
        return jsonify({"error": str(e)}), 500

    usage = response.usage
    return jsonify(
        {
            "response": response.content[0].text,
            "usage": {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_creation_tokens": getattr(usage, "cache_creation_input_tokens", 0),
                "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0),
            },
        }
    )


if __name__ == "__main__":
    app.run(debug=True)
