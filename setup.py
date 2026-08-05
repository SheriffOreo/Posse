#!/usr/bin/env python3
"""
Posse onboarding — one interactive program that stands up your instance.

    python3 setup.py                 # walk through setup, then launch the system
    python3 setup.py --no-launch     # configure only; don't start the daemons/dashboard

It replaces the manual ONBOARDING.md walkthrough: it asks you a handful of
questions (your name + email, the posse's mailbox, Claude auth, the web
host/port), writes every config file for you, seeds the front desk, registers
your dashboard login, starts the daemons + dashboard, and prints the URL to
open. Re-runnable: it shows current values as defaults and only rewrites what
you change.

Nothing here is Posse-specific to any one operator — the released repo carries
no identity. You supply yours once, into files that are git-ignored (your
name/email in infra/operator.json, mail creds in ~/.smtp_env, Claude auth in
~/.anthropic_key or ~/.claude/.credentials.json).
"""
import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
INFRA = REPO / "infra"
DASH = REPO / "dashboard"
OPERATOR_FILE = INFRA / "operator.json"
ENV_LOCAL = REPO / "infra_env.local.sh"
SMTP_ENV = Path(os.path.expanduser("~/.smtp_env"))
ANTHROPIC_KEY = Path(os.path.expanduser("~/.anthropic_key"))
CLAUDE_CREDS = Path(os.path.expanduser("~/.claude/.credentials.json"))

# ---------------------------------------------------------------------------
# tiny terminal helpers (color degrades gracefully when not a tty)
# ---------------------------------------------------------------------------
_TTY = sys.stdout.isatty()


def _c(code, s):
    return f"\033[{code}m{s}\033[0m" if _TTY else s


def step(n, total, title):
    print("\n" + _c("1;36", f"[{n}/{total}] {title}"))
    print(_c("36", "─" * (len(title) + 8)))


def ok(s):
    print("  " + _c("1;32", "✓ ") + s)


def warn(s):
    print("  " + _c("1;33", "! ") + s)


def info(s):
    print("  " + s)


def ask(prompt, default=""):
    d = f" [{default}]" if default else ""
    try:
        v = input(_c("1;37", f"  {prompt}{d}: ")).strip()
    except EOFError:
        v = ""
    return v or default


def ask_secret(prompt):
    try:
        return getpass.getpass(_c("1;37", f"  {prompt}: ")).strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def ask_yesno(prompt, default=True):
    d = "Y/n" if default else "y/N"
    v = ask(f"{prompt} ({d})", "").lower()
    if not v:
        return default
    return v.startswith("y")


# ---------------------------------------------------------------------------
# pure config writers (unit-testable: no prompting, no launching)
# ---------------------------------------------------------------------------
def write_operator(name, email, allowed):
    """Write the operator identity FILE (infra/operator.json) — the single source
    of truth for who this posse reports to. `allowed` is the fail-closed list of
    senders who may command the fleet (defaults to just the operator)."""
    allowed = [a.strip() for a in allowed if a and a.strip()] or ([email] if email else [])
    OPERATOR_FILE.parent.mkdir(parents=True, exist_ok=True)
    OPERATOR_FILE.write_text(json.dumps(
        {"name": name, "email": email, "allowed": allowed}, indent=2) + "\n")
    return OPERATOR_FILE


def write_smtp_env(user, app_pass, to, smtp_host="", imap_host=""):
    """Write ~/.smtp_env (chmod 600) with the posse mailbox credentials."""
    lines = [f"SMTP_USER={user}", f"SMTP_PASS={app_pass}", f"SMTP_TO={to}"]
    if smtp_host:
        lines += [f"SMTP_HOST={smtp_host}", "SMTP_PORT=465"]
    if imap_host:
        lines += [f"IMAP_HOST={imap_host}", "IMAP_PORT=993"]
    SMTP_ENV.write_text("\n".join(lines) + "\n")
    os.chmod(SMTP_ENV, 0o600)
    return SMTP_ENV


def write_anthropic_key(key):
    ANTHROPIC_KEY.write_text(key.strip())
    os.chmod(ANTHROPIC_KEY, 0o600)
    return ANTHROPIC_KEY


def write_env_local(conda_env, claude_auth, dash_host, dash_port, dash_public):
    """Write infra_env.local.sh (git-ignored): the non-identity, non-secret runtime
    env sourced before starting the daemons. Identity lives in operator.json; secrets
    live in ~/.smtp_env and the Claude auth files."""
    lines = [
        "#!/usr/bin/env bash",
        "# infra_env.local.sh — written by setup.py. Git-ignored (per-machine).",
        "# Source this before starting the daemons/dashboard; the tmux daemons inherit it.",
        'export INFRA_CODE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"',
        'export INFRA_STATE_ROOT="${INFRA_STATE_ROOT:-$INFRA_CODE_ROOT/infra}"',
        f'export INFRA_CONDA_ENV="{conda_env}"',
        f'export TSOMP_CLAUDE_AUTH="{claude_auth}"',
        f'export INFRA_DASH_HOST="{dash_host}"',
        f'export INFRA_DASH_PORT="{dash_port}"',
    ]
    if dash_public:
        lines.append('export INFRA_DASH_PUBLIC="1"   # bind 0.0.0.0 + TLS')
    ENV_LOCAL.write_text("\n".join(lines) + "\n")
    os.chmod(ENV_LOCAL, 0o644)
    return ENV_LOCAL


def _rel(p):
    """Path relative to the repo for display; the absolute path if it sits elsewhere."""
    try:
        return Path(p).relative_to(REPO)
    except ValueError:
        return Path(p)


def _reachable_host():
    """Best-guess address a browser on ANOTHER machine would use to reach this host —
    for the final banner when the dashboard is bound publicly (0.0.0.0). Mirrors
    dashboard/start_dashboard.sh: prefer the FQDN (`hostname -f`, then `hostname`),
    else the primary outbound IP, else localhost. Never raises."""
    for cmd in (["hostname", "-f"], ["hostname"]):
        try:
            out = subprocess.run(cmd, text=True, capture_output=True).stdout.strip()
        except Exception:
            out = ""
        if out and out != "localhost":
            return out.split()[0]
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
        if ip:
            return ip
    except Exception:
        pass
    return "localhost"


def dashboard_url(host, port, public, advertise=""):
    """The URL to open. A localhost bind is literally localhost; a PUBLIC bind (0.0.0.0)
    shows a REACHABLE address — the operator's advertised host if given, else an
    auto-detected FQDN/IP — never the misleading 'localhost' the user complained about."""
    scheme = "https" if public else "http"
    if host in ("127.0.0.1", "localhost"):
        shown = "localhost"
    elif advertise:
        shown = advertise
    elif host in ("0.0.0.0", ""):
        shown = _reachable_host()
    else:
        shown = host
    return f"{scheme}://{shown}:{port}"


# ---------------------------------------------------------------------------
# subprocess wrappers for the steps that call the existing tooling
# ---------------------------------------------------------------------------
def _run(cmd, cwd=None, env=None, input_text=None, check=False):
    return subprocess.run(cmd, cwd=cwd, env=env, input=input_text,
                          text=True, capture_output=True, check=check)


def _base_env():
    """Env for subprocesses: the operator identity + state root already resolved, so
    ensure-receptionist / set_password / the daemons all agree on locations."""
    env = dict(os.environ)
    env.setdefault("INFRA_STATE_ROOT", str(INFRA))
    return env


def seed_receptionist(env=None):
    """Idempotently seed the front-desk precinct so the dashboard Precincts tab is
    never empty on a fresh install."""
    return _run([sys.executable, "scratch_records.py", "directory", "ensure-receptionist"],
                cwd=str(INFRA), env=env or _base_env())


def set_dashboard_account(password, email, name, env=None):
    """Bind the single dashboard account: username = email, plus a password (via stdin
    so it never lands in argv). Delegates to dashboard/set_password.py."""
    return _run([sys.executable, "set_password.py", "--stdin", "--email", email, "--name", name],
                cwd=str(DASH), env=env or _base_env(), input_text=password + "\n")


# ---------------------------------------------------------------------------
# launch
# ---------------------------------------------------------------------------
def _have(cmd):
    return shutil.which(cmd) is not None


PREREQS = [("python3", "the daemons + dashboard"), ("tmux", "the long-lived daemons"),
           ("git", "the repo"), ("claude", "every deputy is a claude process")]

# Per-tool install guidance shown when a prerequisite is missing (step 1 pauses on this).
_INSTALL_HINTS = {
    "python3": ["Debian/Ubuntu:  sudo apt install python3",
                "macOS (brew):   brew install python",
                "or:             https://www.python.org/downloads/  (3.9+)"],
    "tmux":    ["Debian/Ubuntu:  sudo apt install tmux",
                "Fedora/RHEL:    sudo dnf install tmux",
                "macOS (brew):   brew install tmux"],
    "git":     ["Debian/Ubuntu:  sudo apt install git",
                "macOS:          xcode-select --install   (or: brew install git)"],
    "claude":  ["npm (Node 18+): npm install -g @anthropic-ai/claude-code",
                "or native:      curl -fsSL https://claude.ai/install.sh | bash",
                "then run once:  claude   (accept the trust prompt, then /login)",
                "docs:           https://docs.claude.com/en/docs/claude-code"],
}


def _check_prereqs():
    """Print each prerequisite's status; return the list of (tool, why) still missing."""
    missing = []
    for tool, why in PREREQS:
        if _have(tool):
            ok(f"{tool}: found")
        else:
            warn(f"{tool}: NOT found — needed for {why}")
            missing.append((tool, why))
    return missing


def _guide_install(missing):
    """Print per-tool install guidance for the missing prerequisites (step 1)."""
    print()
    warn(f"{len(missing)} prerequisite(s) missing — install these before launching:")
    for tool, _why in missing:
        print("  " + _c("1;37", f"install {tool}:"))
        for line in _INSTALL_HINTS.get(tool, ["see the tool's website"]):
            info("    " + line)


def launch_system(env, want_gpu):
    """Start the daemons + dashboard by delegating to posse_start.sh — the SAME
    idempotent start path the operator uses day-to-day (after a reboot, or any time),
    so a fresh install and a later restart are byte-identical. posse_start.sh reads the
    just-written infra_env.local.sh for the dashboard bind, guards every service behind
    a tmux session check (nothing is started twice), and never touches the whole tmux
    server. Output streams straight to the terminal."""
    if not _have("tmux"):
        warn("tmux not found — cannot start the daemons. Install tmux, then run "
             "'bash posse_start.sh' (or follow ONBOARDING.md step 8).")
        return False
    cmd = ["bash", str(REPO / "posse_start.sh")]
    if want_gpu:
        cmd.append("--gpu")
    return subprocess.run(cmd, cwd=str(REPO), env=env).returncode == 0


# ---------------------------------------------------------------------------
# the interactive walkthrough
# ---------------------------------------------------------------------------
def _load_existing_operator():
    try:
        return json.loads(OPERATOR_FILE.read_text())
    except Exception:
        return {}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Posse onboarding — set up & launch your instance.")
    ap.add_argument("--no-launch", action="store_true",
                    help="configure everything but do NOT start the daemons/dashboard")
    args = ap.parse_args(argv)

    TOTAL = 8
    print(_c("1;35", "\n╭──────────────────────────────────────────────────────────────╮"))
    print(_c("1;35", "│   Posse — set up & run your own agent-management instance    │"))
    print(_c("1;35", "╰──────────────────────────────────────────────────────────────╯"))
    print("This will ask a few questions, write your config, and (unless --no-launch)\n"
          "start the system. Ctrl-C any time; re-run to continue. Current values, when\n"
          "present, are shown as defaults.")

    # -- 1. prerequisites ----------------------------------------------------
    # PAUSE on anything missing (esp. `claude` — no deputy can run without it) and guide
    # the install, rather than silently proceeding to a system that cannot fully launch.
    step(1, TOTAL, "Prerequisites")
    missing = _check_prereqs()
    while missing:
        _guide_install(missing)
        if not _TTY:
            warn("Non-interactive run — continuing; install the above, then re-run setup.py.")
            break
        print()
        choice = ask("Press Enter to re-check · type s to skip and continue anyway · q to quit", "").lower()
        if choice.startswith("q"):
            print("\naborted — nothing was changed. Re-run 'python3 setup.py' after installing.")
            return 130
        if choice.startswith("s"):
            warn("Continuing with missing prerequisite(s) — the system may not fully launch.")
            break
        print()
        missing = _check_prereqs()

    # -- 2. your identity ----------------------------------------------------
    step(2, TOTAL, "Your identity")
    info("Your email is your dashboard username, where the posse emails you, and (via the")
    info("allow-list) a sender that may command the fleet. Your name is how agents address you.")
    cur = _load_existing_operator()
    name = ask("Your name", cur.get("name", ""))
    email = ask("Your email", cur.get("email", ""))
    info("Allow-list = the addresses allowed to command the fleet (fail-closed).")
    allowed_raw = ask("Allowed sender emails (comma-separated)",
                      ",".join(cur.get("allowed", []) or ([email] if email else [])))
    write_operator(name, email, allowed_raw.split(","))
    ok(f"wrote {_rel(OPERATOR_FILE)} (name + email + allow-list)")

    # -- 3. posse mailbox ----------------------------------------------------
    step(3, TOTAL, "The posse's mailbox")
    info("The posse needs its OWN email account (a Gmail is the reference) to send + receive")
    info("on your behalf — separate from your personal address. For Gmail, create a 16-char")
    info("app password (Account → Security → 2-Step Verification → App passwords).")
    if SMTP_ENV.exists() and not ask_yesno(f"{SMTP_ENV} already exists — replace it?", default=False):
        ok(f"kept existing {SMTP_ENV}")
    else:
        smtp_user = ask("Posse account email (SMTP_USER)")
        smtp_pass = ask_secret("Posse app password (hidden)")
        smtp_to = ask("Send notifications to (SMTP_TO)", email)
        non_gmail = ask_yesno("Is the posse account NON-Gmail (set SMTP/IMAP hosts)?", default=False)
        smtp_host = ask("SMTP host (SSL:465)", "") if non_gmail else ""
        imap_host = ask("IMAP host (SSL:993)", "") if non_gmail else ""
        if smtp_user and smtp_pass:
            write_smtp_env(smtp_user, smtp_pass, smtp_to or email, smtp_host, imap_host)
            ok(f"wrote {SMTP_ENV} (chmod 600)")
        else:
            warn("skipped ~/.smtp_env (need at least SMTP_USER + app password) — set it later.")

    # -- 4. Claude auth ------------------------------------------------------
    step(4, TOTAL, "Claude authentication")
    info("Every deputy is a `claude` process. Two billing modes:")
    info("  subscription — Claude Max OAuth (default; run `claude` → /login once)")
    info("  apikey       — an Anthropic API key, pay-per-token")
    mode = ask("Auth mode (subscription/apikey)", "subscription").lower()
    claude_auth = "apikey" if mode.startswith("a") else "subscription"
    if claude_auth == "apikey":
        key = ask_secret("Anthropic API key (hidden)")
        if key:
            write_anthropic_key(key)
            ok(f"wrote {ANTHROPIC_KEY} (chmod 600)")
        else:
            warn("no key entered — put it in ~/.anthropic_key later.")
    else:
        if CLAUDE_CREDS.exists():
            ok("found ~/.claude/.credentials.json (Max OAuth login present)")
        else:
            warn("~/.claude/.credentials.json not found — run `claude` then /login before "
                 "sending tasks, or deputies won't start.")

    # -- 5. permissions (the common question) --------------------------------
    step(5, TOTAL, "Autonomous deputies & permissions")
    info("Do you need to set up a Claude permission bypass to run deputies autonomously?")
    ok("Short answer: it's already built in — you do NOT add anything.")
    info("A deputy is a HEADLESS claude run with no human to approve each tool call, so every")
    info("launcher passes --dangerously-skip-permissions. Deputies act autonomously in their")
    info("lane once you send a task. One-time step: run `claude` once interactively and accept")
    info("the trust prompt for this folder (and finish /login) so headless runs start cleanly.")

    # -- 6. web host & port --------------------------------------------------
    step(6, TOTAL, "Dashboard web address")
    info("Localhost (default, safest) = reach via SSH tunnel. Public = bind 0.0.0.0 + TLS.")
    public = ask_yesno("Expose the dashboard publicly with TLS (else localhost-only)?", default=False)
    advertise = ""
    if public:
        dash_host = "0.0.0.0"
        info("Public bind — browsers on OTHER machines reach the dashboard at this host's")
        info("address, not localhost. Enter the hostname/IP they should use (auto-detected).")
        advertise = ask("Public hostname or IP", _reachable_host())
    else:
        dash_host = ask("Bind host", "127.0.0.1")
    dash_port = ask("Port", "8787")
    conda_env = ask("Conda env for the daemons (blank = system python3)", os.environ.get("INFRA_CONDA_ENV", ""))
    write_env_local(conda_env, claude_auth, dash_host, dash_port, public)
    ok(f"wrote {_rel(ENV_LOCAL)} (state root, auth mode, web bind)")

    # -- 7. bootstrap the front desk + dashboard account ---------------------
    step(7, TOTAL, "Seed the front desk + your dashboard login")
    env = _base_env()
    env["INFRA_OPERATOR_EMAIL"] = email
    env["INFRA_OPERATOR_NAME"] = name
    r = seed_receptionist(env)
    ok("seeded the receptionist precinct (the Precincts tab now shows the front desk)"
       if r.returncode == 0 else "receptionist seed reported: " + r.stderr.strip()[:160])
    if ask_yesno("Set your dashboard password now (username = your email)?", default=True):
        pw = ask_secret("Dashboard password (min 8, hidden)")
        pw2 = ask_secret("Confirm password")
        if pw and pw == pw2:
            r = set_dashboard_account(pw, email, name, env)
            ok(f"dashboard account set — sign in as {email}"
               if r.returncode == 0 else "set_password reported: " + (r.stderr.strip()[:160] or r.stdout.strip()[:160]))
        else:
            warn("passwords empty or mismatched — set later with dashboard/set_password.py.")

    # -- 8. launch -----------------------------------------------------------
    step(8, TOTAL, "Launch the system")
    url = dashboard_url(dash_host, dash_port, public, advertise)
    if args.no_launch:
        warn("--no-launch: skipping daemon/dashboard start. Start later with ONBOARDING.md step 8.")
    else:
        launch_system(env, want_gpu=_have("nvidia-smi") and ask_yesno(
            "Start the GPU manager too (you have a GPU)?", default=False))

    # -- done ----------------------------------------------------------------
    print("\n" + _c("1;32", "All set — your Posse is configured." +
                    ("" if args.no_launch else " The system is running.")))
    print(_c("1;37", f"  Dashboard:  {url}"))
    if public:
        info("(public bind + self-signed TLS — your browser will warn once; accept it)")
    else:
        info(f"(localhost-only — tunnel first:  ssh -L {dash_port}:localhost:{dash_port} <this-host>)")
    print(_c("1;37", f"  Sign in as: {email or '<your email>'}"))
    print(_c("1;37", "  Start / stop your posse (any time — e.g. after a reboot):"))
    info("   bash posse_start.sh   — start or verify all daemons + dashboard (idempotent)")
    info("   bash posse_stop.sh    — stop the daemons + dashboard (deputies + jobs keep running)")
    info("   (add --dry-run to either to preview; --gpu to include the GPU manager)")
    info("Next: create precincts (python3 infra/scratch_records.py directory register --name <x> ...)")
    info(f"      then email the posse account a task tagged [precinct]. See ONBOARDING.md §9–10.")
    print()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\naborted.")
        sys.exit(130)
