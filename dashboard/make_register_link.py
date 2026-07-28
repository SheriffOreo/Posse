#!/usr/bin/env python3
"""(Re)generate the dashboard's ONE-TIME registration link and print its URL.

Bootstraps the first login WITHOUT anyone picking a password on the CLI: it mints a
single-use token (only its SHA-256 is stored, under the git-ignored instance/) and
prints a URL. Open the URL (through the SSH tunnel), choose a password, and the token
is consumed. Refuses if a password is already set — registration is a one-time
bootstrap, not a reset path.

    python make_register_link.py         # print a fresh registration URL

To reset a lost password: delete instance/auth.json, then re-run this.
"""
import os
import sys

import auth
import config


def main():
    if auth.is_configured():
        print("A dashboard password is already set — registration is closed.",
              file=sys.stderr)
        print(f"To reset it, remove {config.SECRET_FILE} and re-run this.",
              file=sys.stderr)
        return 1
    tok = auth.create_register_token()
    port = os.environ.get("INFRA_DASH_PORT", str(config.PORT))
    # The link is opened through the SSH tunnel, so localhost is the right host.
    print(f"http://localhost:{port}/register?token={tok}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
