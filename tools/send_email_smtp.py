#!/usr/bin/env python3
"""Send a prepared .eml over SMTP (Gmail by default), with explicit confirmation.

Credentials are read from environment variables or an explicit env file, never from the command
line.  For Gmail use an App Password (2FA required), not the account password:
  SMTP_USER=flumos@gmail.com
  SMTP_PASSWORD=<16-char app password>
Optional: SMTP_HOST (default smtp.gmail.com), SMTP_PORT (587).

Usage:
  python tools/send_email_smtp.py --eml outreach/legat/email.eml            # dry-run
  python tools/send_email_smtp.py --eml outreach/legat/email.eml \
      --env-file outreach/legat/.smtp.env --confirm
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


def _load_env_file(path: str) -> dict[str, str]:
    """Read simple KEY=VALUE lines; never print values."""
    values: dict[str, str] = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send a prepared .eml via SMTP (dry-run unless --confirm)")
    parser.add_argument("--eml", required=True)
    parser.add_argument("--env-file", default=None,
                        help="optional KEY=VALUE file with SMTP_USER/SMTP_PASSWORD; not committed")
    parser.add_argument("--confirm", action="store_true", help="actually send")
    parser.add_argument("--host", default=os.environ.get("SMTP_HOST", "smtp.gmail.com"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SMTP_PORT", "587")))
    args = parser.parse_args(argv)

    raw = Path(args.eml).read_bytes()
    message = BytesParser(policy=policy.default).parsebytes(raw)
    recipients = [addr.strip() for addr in str(message["To"]).split(",") if addr.strip()]
    print({"from": message["From"], "to": recipients, "subject": message["Subject"],
           "bytes": len(raw), "send": bool(args.confirm), "env_file": bool(args.env_file)})
    if not args.confirm:
        print("dry-run: pass --confirm to send")
        return 0

    values: dict[str, str] = {}
    if args.env_file:
        values.update(_load_env_file(args.env_file))
    user = os.environ.get("SMTP_USER", "").strip() or values.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "").strip() or values.get("SMTP_PASSWORD", "").strip()
    host = args.host or values.get("SMTP_HOST", "").strip() or "smtp.gmail.com"
    port = args.port or int(values.get("SMTP_PORT", "587") or 587)
    if not user or not password:
        print("BLOCKED: SMTP_USER / SMTP_PASSWORD are not set; no send attempted")
        return 2
    context = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=60) as smtp:
        smtp.ehlo()
        smtp.starttls(context=context)
        smtp.login(user, password)
        smtp.send_message(message, from_addr=user, to_addrs=recipients)
    print("SENT")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
