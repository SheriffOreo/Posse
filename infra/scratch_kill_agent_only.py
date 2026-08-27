#!/usr/bin/env python3
"""Task 153B: surgically kill ONLY a worker's claude process(es), leaving every
other process it started (CPU eval/report/driver runs, tool-call subtrees) alive.

  Usage: python3 scratch_kill_agent_only.py <agent_tmux_name>
  Exit 0: claude killed; a manifest of surviving descendants was written to
          scratch_full_logs/inbox/survivors_<agent>.jsonl and a one-line summary
          printed to stdout (for the caller's log).
  Exit 1: could not identify the session/pane/claude -> caller should fall back
          to the old `tmux kill-session` path.

Why this works (probe-verified on claude 2.1.186, Task 153B):
  - Bash-tool children run DETACHED (own session, no ctty) with stdout/stderr
    redirected to /tmp/claude-<uid>/<proj>/<session>/tasks/<id>.output — so
    neither the tmux/pty teardown nor a dead pipe can kill them. The ONLY thing
    that kills them today is claude's own graceful-shutdown handler (SIGTERM to
    tool process groups) when tmux HUPs it. SIGKILL gives claude no chance to
    do that.
  - Kill order matters: SIGSTOP the pane shell FIRST so it can never write its
    "EXITED rc=" log marker (the watchdog must classify this death as the quiet
    'interrupted' flow, not a 'crash'), then SIGKILL claude, then SIGKILL the
    frozen pane shell. The session self-destructs; a belt-and-suspenders
    kill-session afterwards is safe because claude is already dead (survivors
    live in their own sessions, out of tmux's reach).
"""
import json, os, subprocess, sys, time

# Portable: the state root is INFRA_STATE_ROOT if set (the same env the daemons +
# dashboard resolve state with), else this script's own directory (the infra/ dir,
# where scratch_full_logs lives). No hardcoded host path.
ROOT = os.environ.get("INFRA_STATE_ROOT") or os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)


def sh(args):
    return subprocess.run(args, capture_output=True, text=True)


def argv_of(pid):
    try:
        raw = open(f"/proc/{pid}/cmdline", "rb").read()
        return [a.decode(errors="replace") for a in raw.split(b"\0") if a]
    except OSError:
        return []


def starttime_of(pid):
    """Field 22 of /proc/<pid>/stat — with (pid,starttime) a process identity
    that survives pid reuse."""
    try:
        stat = open(f"/proc/{pid}/stat").read()
        return int(stat.rsplit(")", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def fd1_of(pid):
    try:
        return os.readlink(f"/proc/{pid}/fd/1")
    except OSError:
        return None


def descendants(roots):
    kids = {}
    for line in sh(["ps", "-eo", "pid=,ppid="]).stdout.splitlines():
        try:
            p, pp = map(int, line.split())
        except ValueError:
            continue
        kids.setdefault(pp, []).append(p)
    out, stack = [], list(roots)
    while stack:
        for k in kids.get(stack.pop(), []):
            out.append(k)
            stack.append(k)
    return out


# Case 557: the agent CLIs a deputy can be running under. This used to be the bare
# literal "claude", which meant a chatgpt (codex) deputy matched NOTHING: main()
# then exited 1 and scratch_interrupt_worker.sh fell back to `tmux kill-session`,
# the blunt path that ALSO kills the worker's plain CPU children — precisely what
# this surgical kill exists to prevent. Matching the union is right here because
# the question is only "which process IS the agent", and a worker's pane tree never
# contains the other vendor's CLI.
#
# Read from the registry, but never at the cost of the kill failing: an import
# error falls back to the hardcoded pair rather than degrading to kill-session.
try:
    import scratch_models as _sm
    _AGENT_CLIS = frozenset(s["cli"] for s in _sm.SERVICES.values())
except Exception:
    _AGENT_CLIS = frozenset({"claude", "codex"})


def is_agent(pid):
    av = argv_of(pid)
    return any(os.path.basename(a) in _AGENT_CLIS for a in av[:2])


# Old name kept: Task 153B's vocabulary (and any external caller) still says "claude".
is_claude = is_agent


def main():
    agent = sys.argv[1]
    tag = f"={agent}"

    panes = sh(["tmux", "list-panes", "-s", "-t", tag, "-F", "#{pane_pid}"])
    if panes.returncode != 0:
        print(f"no tmux session {agent}", file=sys.stderr)
        return 1
    pane_pids = [int(p) for p in panes.stdout.split()]
    if not pane_pids:
        print(f"no panes in session {agent}", file=sys.stderr)
        return 1

    tree = descendants(pane_pids)
    claudes = [p for p in tree if is_agent(p)]
    if not claudes:
        print(f"no agent process ({'/'.join(sorted(_AGENT_CLIS))}) under panes "
              f"{pane_pids} of {agent}", file=sys.stderr)
        return 1

    # snapshot survivor candidates BEFORE killing (they reparent away afterwards)
    now = time.time()
    cands = []
    for p in tree:
        if p in claudes:
            continue
        st = starttime_of(p)
        if st is None:
            continue
        cands.append({"pid": p, "starttime": st, "pgid": _ok(os.getpgid, p),
                      "cmd": " ".join(argv_of(p))[:300], "out": fd1_of(p),
                      "recorded_at": now, "agent": agent})

    # the surgical kill: freeze pane shells -> SIGKILL claude -> SIGKILL shells
    for p in pane_pids:
        _ok(os.kill, p, 19)                     # SIGSTOP: no "EXITED rc=" marker
    for p in claudes:
        _ok(os.kill, p, 9)                      # SIGKILL: no child-reaping cleanup
    for p in pane_pids:
        _ok(os.kill, p, 9)

    time.sleep(0.3)
    survivors = [c for c in cands if starttime_of(c["pid"]) == c["starttime"]]

    # merge manifest: keep prior entries that are still alive, add new survivors
    mpath = f"scratch_full_logs/inbox/survivors_{agent}.jsonl"
    merged, seen = [], set()
    for c in survivors:
        merged.append(c)
        seen.add((c["pid"], c["starttime"]))
    try:
        with open(mpath) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = (r.get("pid"), r.get("starttime"))
                if key not in seen and starttime_of(r.get("pid", -1)) == r.get("starttime"):
                    merged.append(r)
                    seen.add(key)
    except OSError:
        pass
    tmp = mpath + ".tmp"
    with open(tmp, "w") as f:
        for r in merged:
            f.write(json.dumps(r) + "\n")
    os.replace(tmp, mpath)

    # session should self-destruct; force-clean it once claude is dead (cannot
    # touch survivors any more — they live in their own sessions)
    for _ in range(10):
        if sh(["tmux", "has-session", "-t", tag]).returncode != 0:
            break
        time.sleep(0.3)
    sh(["tmux", "kill-session", "-t", tag])

    print(f"claude_killed={','.join(map(str, claudes))} "
          f"survivors={len(survivors)}/{len(cands)} manifest={mpath}")
    return 0


def _ok(fn, *a):
    try:
        return fn(*a)
    except (ProcessLookupError, PermissionError, OSError):
        return None


if __name__ == "__main__":
    sys.exit(main())
