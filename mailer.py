import resend
import os

resend.api_key = os.environ.get("RESEND_API_KEY", "")
FROM_ADDRESS = "automation@omnigroupuae.com"


def send_password_reset(to_email: str, reset_url: str) -> bool:
    """Send a password reset email. Returns True on success."""
    try:
        resend.Emails.send({
            "from": FROM_ADDRESS,
            "to": to_email,
            "subject": "Reset your IVA Automation password",
            "html": f"""
<p>You requested a password reset for your IVA Automation account.</p>
<p><a href="{reset_url}">Click here to reset your password</a></p>
<p>This link expires in 1 hour. If you didn't request this, ignore this email.</p>
<p>– Omni Group Automation</p>
""",
        })
        return True
    except Exception:
        return False
