#!/usr/bin/env python3
"""Send a prepared .eml over SMTP (Gmail by default), with explicit confirmation.

Credentials are read from environment variables, never from the command line or files:
  SMTP_USER=flumos@gmail.com
  SMTP_PASSWORD=<app password, not the account password>
Optional: SMTP_HOST (default smtp.gmail.com), SMTP_PORT (587).

Usage:
  python tools/send_email_smtp.py --eml outreach/legat/email.eml --confirm
Without --confirm it only validates the .eml and prints a dry-run summary.
"""
from __future__ import annotations

import argparse
import os
import smtplib
import ssl
from email import policy
from email.parser import BytesParser
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send a prepared .eml via SMTP (dry-run unless --confirm)")
    parser.add_argument("--eml", required=True)
    parser.add_argument("--confirm", action="store_true", help="actually send")
    parser.add_argument("--host", default=os.environ.get("SMTP_HOST", "smtp.gmail.com"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SMTP_PORT", "587")))
    args = parser.parse_args(argv)

    raw = Path(args.eml).read_bytes()
    message = BytesParser(policy=policy.default).parsebytes(raw)
    recipients = [addr.strip() for addr in str(message["To"]).split(",") if addr.strip()]
    print({"from": message["From"], "to": recipients, "subject": message["Subject"],
           "bytes": len(raw), "send": bool(args.confirm)})
    if not args.confirm:
        print("dry-run: pass --confirm to send")
        return 0

    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "").strip()
    if not user or not password:
        print("BLOCKED: SMTP_USER / SMTP_PASSWORD are not set; no send attempted")
        return 2
    context = ssl.create_default_context()
    with smtplib.SMTP(args.host, args.port, timeout=60) as smtp:
        smtp.ehlo()
        smtp.starttls(context=context)
        smtp.login(user, password)
        smtp.send_message(message, from_addr=user, to_addrs=recipients)
    print("SENT")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
