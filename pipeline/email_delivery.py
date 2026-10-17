"""Simple SMTP email delivery with dry-run previews."""

from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path


@dataclass(frozen=True)
class EmailResult:
    status: str
    detail: str


def build_email_bodies(
    *,
    subject: str,
    plain_body: str,
    html_body: str,
) -> tuple[str, str]:
    return plain_body, html_body


def send_digest_email(
    *,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    email_from: str,
    email_to: str,
    subject: str,
    plain_body: str,
    html_body: str,
    attachment_path: Path | None,
    dry_run: bool,
    preview_dir: Path | None,
) -> EmailResult:
    if dry_run:
        if preview_dir:
            preview_dir.mkdir(parents=True, exist_ok=True)
            (preview_dir / "email-preview.txt").write_text(
                f"Subject: {subject}\n\n{plain_body}", encoding="utf-8"
            )
            (preview_dir / "email-preview.html").write_text(html_body, encoding="utf-8")
        return EmailResult(status="preview", detail="Email not sent (dry-run).")

    message = MIMEMultipart("mixed")
    message["Subject"] = subject
    message["From"] = email_from
    message["To"] = email_to

    alternative = MIMEMultipart("alternative")
    alternative.attach(MIMEText(plain_body, "plain", "utf-8"))
    alternative.attach(MIMEText(html_body, "html", "utf-8"))
    message.attach(alternative)

    if attachment_path and attachment_path.exists():
        part = MIMEBase("application", "octet-stream")
        part.set_payload(attachment_path.read_bytes())
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            f'attachment; filename="{attachment_path.name}"',
        )
        message.attach(part)

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(smtp_host, smtp_port, timeout=60) as server:
            server.starttls(context=context)
            server.login(smtp_user, smtp_password)
            refused = server.sendmail(email_from, [email_to], message.as_string())
    except smtplib.SMTPException as exc:
        return EmailResult(status="failed", detail=f"SMTP error: {exc.__class__.__name__}")
    except OSError as exc:
        return EmailResult(status="failed", detail=f"SMTP connection error: {exc.__class__.__name__}")

    if refused:
        return EmailResult(status="failed", detail=f"SMTP refused recipients: {list(refused)}")
    return EmailResult(status="sent", detail="SMTP accepted message for delivery.")
