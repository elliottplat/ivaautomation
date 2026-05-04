import os
import base64
import anthropic
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20 MB

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

SYSTEM_PROMPT = (
    "You are a helpful AI assistant with vision capabilities. "
    "When images are provided, analyze them carefully and respond to the "
    "user's prompt in relation to those images. Be detailed, accurate, and helpful."
)

ALLOWED_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/analyze", methods=["POST"])
def analyze():
    prompt = request.form.get("prompt", "").strip()
    if not prompt:
        return jsonify({"error": "Please provide a prompt."}), 400

    files = request.files.getlist("images")

    content = []

    for file in files:
        if not file or not file.filename:
            continue
        media_type = file.content_type or "image/jpeg"
        if media_type not in ALLOWED_TYPES:
            return jsonify({"error": f"Unsupported image type: {media_type}"}), 400
        image_data = base64.standard_b64encode(file.read()).decode("utf-8")
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": image_data,
                },
            }
        )

    content.append({"type": "text", "text": prompt})

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            # Cache the stable system prompt — saves tokens on repeated requests
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
                "cache_creation_tokens": getattr(
                    usage, "cache_creation_input_tokens", 0
                ),
                "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0),
            },
        }
    )


if __name__ == "__main__":
    app.run(debug=True)
