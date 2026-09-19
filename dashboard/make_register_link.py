#!/usr/bin/env python3
"""(Re)generate the dashboard's ONE-TIME registration link and print its URL.

Bootstraps the dashboard account WITHOUT anyone picking a password on the CLI: it
mints a single-use token (only its SHA-256 is stored, under the git-ignored
instance/) and prints a URL. The operator binds the new account's identity to the
link here — the recipient's email becomes the account username and their name a
display label. Open the URL (through the SSH tunnel), choose a password, and the
token is consumed. Refuses if a password is already set — registration is a
one-time bootstrap, not a reset path.

    python make_register_link.py --email you@example.com --name "Your Name"
    python make_register_link.py                      # identity-less (still works)

To reset a lost password: delete instance/auth.json, then re-run this.
"""
import argparse
import os
import sys

import auth
import config


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--email", default=None,
                    help="the new account's username (recommended)")
    ap.add_argument("--name", default=None, help="display name for the account")
    args = ap.parse_args()

    if auth.is_configured():
        print("A dashboard password is already set — registration is closed.",
              file=sys.stderr)
        print(f"To reset it, remove {config.SECRET_FILE} and re-run this.",
              file=sys.stderr)
        return 1
    tok = auth.create_register_token(email=args.email, name=args.name)
    port = os.environ.get("INFRA_DASH_PORT", str(config.PORT))
    # Match the bind this instance is actually configured for. The token is
    # SINGLE-USE, so printing a link whose scheme or host cannot be opened costs the
    # operator the token: on a public+TLS bind, localhost/http is simply wrong.
    public = os.environ.get("INFRA_DASH_PUBLIC", "") == "1"
    tls = public or os.environ.get("INFRA_DASH_TLS", "") == "1"
    scheme = "https" if tls else "http"
    if public:
        host = os.environ.get("INFRA_DASH_ADVERTISE", "") or "<this-host>"
        print(f"{scheme}://{host}:{port}/register?token={tok}")
        print("(replace <this-host> with this machine's hostname or IP if it is not "
              "already filled in)", file=sys.stderr)
    else:
        # Localhost bind: the link is opened through an SSH tunnel.
        print(f"{scheme}://localhost:{port}/register?token={tok}")
    if args.email:
        print(f"(account username: {args.email})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
