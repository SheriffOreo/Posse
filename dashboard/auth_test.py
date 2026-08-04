#!/usr/bin/env python3
"""Tests for the dashboard auth + operator-identity layer (email-as-username).

Runs against a throwaway instance dir (INFRA_DASH_INSTANCE) so it never touches a
live instance/auth.json. Covers: password round-trip, cookie sign/verify, the
one-time registration-link flow, identity capture (email/name), and backward
compatibility (no-identity instances still work).

    python auth_test.py
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="posse_auth_test_")
os.environ["INFRA_DASH_INSTANCE"] = _TMP  # must be set BEFORE importing config/auth

import auth  # noqa: E402
import config  # noqa: E402

_n = 0


def ok(cond, msg):
    global _n
    _n += 1
    assert cond, "FAIL: " + msg
    print(f"  ok {_n}: {msg}")


def _reset():
    for f in (config.SECRET_FILE, config.REGISTER_FILE):
        try:
            f.unlink()
        except OSError:
            pass


def test_password_and_cookie():
    _reset()
    ok(not auth.is_configured(), "fresh instance is unconfigured")
    ok(not auth.verify_password("whatever"), "verify fails with no password set")
    auth.set_password("hunter2secret")
    ok(auth.is_configured(), "configured after set_password")
    ok(auth.verify_password("hunter2secret"), "correct password verifies")
    ok(not auth.verify_password("wrong"), "wrong password rejected")
    c = auth.make_cookie()
    ok(auth.check_cookie(c), "freshly minted cookie verifies")
    ok(not auth.check_cookie(c + "x"), "tampered cookie rejected")


def test_no_identity_backward_compat():
    _reset()
    auth.set_password("plainpassword")  # no name/email — the pre-identity path
    a = auth.account()
    ok(a == {"email": None, "name": None}, "account() is empty on a no-identity set")
    ok(auth.verify_password("plainpassword"), "no-identity account still logs in")


def test_registration_identity_flow():
    _reset()
    tok = auth.create_register_token(email="ada@example.com", name="Ada Lovelace")
    ok(auth.register_open(), "registration open after minting a token")
    info = auth.register_info()
    ok(info["email"] == "ada@example.com", "register_info surfaces the bound email")
    ok(info["name"] == "Ada Lovelace", "register_info surfaces the bound name")
    ok(auth.check_register_token(tok), "the raw token validates")
    ok(not auth.check_register_token("bogus"), "a bogus token is rejected")
    ok(not auth.consume_register_token(tok, "short"), "too-short password refused")
    ok(auth.consume_register_token(tok, "longenoughpw"), "consume sets the password")
    ok(auth.is_configured(), "configured after consuming the link")
    a = auth.account()
    ok(a["email"] == "ada@example.com", "email persisted as the account username")
    ok(a["name"] == "Ada Lovelace", "name persisted on the account")
    ok(auth.verify_password("longenoughpw"), "the chosen password logs in")
    ok(not auth.register_open(), "registration closed once a password exists")
    ok(not auth.consume_register_token(tok, "anotherpw"), "token is single-use")


def test_form_name_overrides_link_name():
    _reset()
    tok = auth.create_register_token(email="ops@example.com", name="From Link")
    ok(auth.consume_register_token(tok, "longenoughpw", name="From Form"),
       "consume with a form-supplied name succeeds")
    ok(auth.account()["name"] == "From Form", "form name overrides the link name")
    ok(auth.account()["email"] == "ops@example.com", "email still comes from the link")


def test_secret_file_never_holds_plaintext():
    _reset()
    auth.set_password("s3cr3t-plaintext-marker", email="x@y.z", name="X")
    raw = config.SECRET_FILE.read_text()
    ok("s3cr3t-plaintext-marker" not in raw, "plaintext password never stored on disk")
    ok("x@y.z" in raw, "email IS stored (it is the non-secret username)")


if __name__ == "__main__":
    test_password_and_cookie()
    test_no_identity_backward_compat()
    test_registration_identity_flow()
    test_form_name_overrides_link_name()
    test_secret_file_never_holds_plaintext()
    print(f"\nall {_n} auth assertions passed")
