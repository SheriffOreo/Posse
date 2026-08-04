#!/usr/bin/env python3
"""
Set (or reset) the dashboard login password.

Usage:
    python set_password.py                 # prompt (hidden input)
    python set_password.py --stdin         # read one line from stdin
    INFRA_DASH_PASSWORD=... python set_password.py --env

Writes a salted PBKDF2 hash + a random cookie-signing secret to instance/auth.json
(chmod 600, git-ignored). The plaintext is never stored.
"""
import argparse
import getpass
import os
import sys

import auth
import config


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stdin", action="store_true", help="read password from stdin")
    ap.add_argument("--env", action="store_true", help="read from INFRA_DASH_PASSWORD")
    ap.add_argument("--email", default=None, help="account username (email)")
    ap.add_argument("--name", default=None, help="account display name")
    args = ap.parse_args()

    if args.env:
        pw = os.environ.get("INFRA_DASH_PASSWORD", "")
    elif args.stdin:
        pw = sys.stdin.readline().rstrip("\n")
    else:
        pw = getpass.getpass("New dashboard password: ")
        if pw != getpass.getpass("Confirm: "):
            print("Passwords do not match.", file=sys.stderr)
            return 1

    if len(pw) < auth.MIN_PW_LEN:
        print(f"Password too short (min {auth.MIN_PW_LEN} chars).", file=sys.stderr)
        return 1

    auth.set_password(pw, name=args.name, email=args.email)
    who = f" for {args.email}" if args.email else ""
    print(f"Password set{who}. Secret stored at: {config.SECRET_FILE} (chmod 600)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
