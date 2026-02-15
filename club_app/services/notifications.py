from __future__ import annotations

import smtplib
from datetime import datetime
from email.message import EmailMessage

import requests

from ..models import NotificationOutbox
from .settings_store import get_setting, get_setting_int


def queue_notification(session, title: str, body: str) -> NotificationOutbox:
    channel = (get_setting(session, "notify_channel", "LOG") or "LOG").upper()
    target = get_setting(session, "notify_target", "") or ""
    item = NotificationOutbox(channel=channel, target=target, title=title, body=body)
    session.add(item)
    return item


def dispatch_pending_notifications(session) -> None:
    pending = (
        session.query(NotificationOutbox)
        .filter(NotificationOutbox.status == "PENDING")
        .order_by(NotificationOutbox.created_at.asc())
        .all()
    )
    for item in pending:
        try:
            _send_item(session, item)
            item.status = "SENT"
            item.sent_at = datetime.utcnow()
            item.error = None
        except Exception as exc:  # pragma: no cover - operational fallback
            item.status = "FAILED"
            item.error = str(exc)


def _send_item(session, item: NotificationOutbox) -> None:
    channel = (item.channel or "LOG").upper()
    if channel == "LOG":
        return
    if channel == "DISCORD":
        webhook = get_setting(session, "discord_webhook", "") or item.target
        if not webhook:
            raise RuntimeError("Discord webhook is not configured")
        payload = {"content": f"**{item.title}**\n{item.body}"}
        response = requests.post(webhook, json=payload, timeout=10)
        response.raise_for_status()
        return
    if channel == "EMAIL":
        smtp_host = get_setting(session, "smtp_host", "")
        smtp_port = get_setting_int(session, "smtp_port", 587)
        smtp_user = get_setting(session, "smtp_user", "")
        smtp_password = get_setting(session, "smtp_password", "")
        smtp_from = get_setting(session, "smtp_from", "") or smtp_user
        target = item.target or get_setting(session, "notify_target", "")
        if not smtp_host or not smtp_from or not target:
            raise RuntimeError("SMTP settings are incomplete")

        msg = EmailMessage()
        msg["Subject"] = item.title
        msg["From"] = smtp_from
        msg["To"] = target
        msg.set_content(item.body)

        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as smtp:
            smtp.starttls()
            if smtp_user:
                smtp.login(smtp_user, smtp_password)
            smtp.send_message(msg)
        return

    raise RuntimeError(f"Unsupported channel: {channel}")
