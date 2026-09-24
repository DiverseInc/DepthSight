import os
import smtplib
from email.message import EmailMessage
from email.utils import formataddr

import resend

# --- SMTP fallback (legacy) ----------------------------------------------
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp-pulse.com")
_smtp_port_raw = os.getenv("SMTP_PORT", "465")
try:
    if isinstance(_smtp_port_raw, str) and "(" in _smtp_port_raw:
        SMTP_PORT = 465
    else:
        SMTP_PORT = int(_smtp_port_raw)
except (ValueError, TypeError):
    SMTP_PORT = 465

SMTP_USER = os.getenv("SMTP_USER", "allester21212@gmail.com")
SMTP_SENDER_EMAIL = os.getenv("SMTP_SENDER_EMAIL", "noreply@depthsight.pro")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")

# --- Resend (preferred) ---------------------------------------------------
RESEND_API_KEY = os.getenv("RESEND_API_KEY")
RESEND_FROM_EMAIL = os.getenv(
    "RESEND_FROM_EMAIL",
    SMTP_SENDER_EMAIL,  # fall back to the SMTP from-address so we don't break if user only sets one
)


def _send_via_resend(to_email: str, subject: str, html_content: str) -> None:
    """Send via the Resend API. Uses the official `resend` Python client."""
    resend.api_key = RESEND_API_KEY
    resend.Emails.send(
        {
            "from": formataddr(("DepthSight", RESEND_FROM_EMAIL)),
            "to": [to_email],
            "subject": subject,
            "html": html_content,
        }
    )


def _send_via_smtp(to_email: str, subject: str, html_content: str) -> None:
    """Legacy SMTP send — used only when RESEND_API_KEY is unset."""
    if not SMTP_PASSWORD or SMTP_PASSWORD == "YOUR_SMTP_PASSWORD":
        print(
            f"ERROR: SMTP password is not configured. Cannot send email to {to_email}"
        )
        raise ValueError("Email not sent: SMTP password is not configured.")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("Depth Sight", SMTP_SENDER_EMAIL))
    msg["To"] = to_email
    msg.set_content(html_content, subtype="html")

    try:
        print(f"Attempting to send email to {to_email} via {SMTP_SERVER}:{SMTP_PORT}")
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
            print(f"Connected to SMTP server, logging in as {SMTP_USER}")
            server.login(SMTP_USER, SMTP_PASSWORD)
            print(f"Login successful, sending message from {SMTP_SENDER_EMAIL}")
            server.send_message(msg)
            print(f"✓ Email sent successfully to {to_email}")
    except smtplib.SMTPAuthenticationError as e:
        print(f"✗ SMTP Authentication failed for {SMTP_USER}: {e}")
        raise
    except smtplib.SMTPException as e:
        print(f"✗ SMTP error while sending email to {to_email}: {e}")
        raise
    except Exception as e:
        print(
            f"✗ Unexpected error sending email to {to_email}: {type(e).__name__}: {e}"
        )
        raise


def send_email(to_email: str, subject: str, html_content: str):
    """
    Send a transactional email. Prefers Resend (when RESEND_API_KEY is set),
    falls back to legacy SMTP for backwards compatibility.

    Callers don't need to know which transport was used — this is the only
    public function in the email layer.
    """
    if RESEND_API_KEY and RESEND_API_KEY.startswith("re_"):
        _send_via_resend(to_email, subject, html_content)
        return
    _send_via_smtp(to_email, subject, html_content)
