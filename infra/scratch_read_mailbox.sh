#!/usr/bin/env bash
# Worker helper: print + clear this worker's LIVE mailbox (mid-task feedback the
# inbox router delivered while the worker was running). Prints nothing if empty.
# Usage: bash scratch_read_mailbox.sh <worker_name>
# Task 123: read+archive+clear runs under the same flock the router uses to
# append, so a delivery landing mid-read can no longer be wiped unseen.
# Task 185 ORPHAN GUARD: the drain is allowed only when a LIVE claude process is
# an ancestor of this call. Rationale: workers poll their mailbox from Bash tool
# calls (MB=$(bash scratch_read_mailbox.sh <name>)); a surgical interrupt kills
# only the claude, so an in-flight poll loop survives as an orphan (by design,
# Task 153B — compute must survive). On 2026-07-14 ~03:24 such an orphan of the
# killed `slides` claude woke, drained the parked uid=183 mail, and echoed it
# into its dead parent's task-output file — no agent ever saw it. An orphan has
# been reparented away from claude, so it now gets an EMPTY read and the mail
# stays put for the live worker / the next relaunch drain.
# Manual/admin drain override: MAILBOX_FORCE=1 bash scratch_read_mailbox.sh <name>
# (to inspect without draining, just: cat scratch_full_logs/inbox/mailbox_<name>.md)
cd "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"   # portable: infra/ dir = code + state root
NAME="${1:?worker name}"
MB="scratch_full_logs/inbox/mailbox_${NAME}.md"
LOCK="scratch_full_logs/inbox/mailbox_${NAME}.lock"

claude_ancestor() {  # 0 iff some live ancestor has argv[0] or argv[1] == claude
  local pid=$PPID hops=0 stat rest
  while [ -n "$pid" ] && [ "$pid" -gt 1 ] 2>/dev/null && [ "$hops" -lt 40 ]; do
    if tr '\0' '\n' < "/proc/$pid/cmdline" 2>/dev/null | head -2 \
         | sed 's|.*/||' | grep -qx 'claude'; then
      return 0
    fi
    stat=$(cat "/proc/$pid/stat" 2>/dev/null) || return 1
    rest=${stat##*) }              # strip "pid (comm) " — comm may hold spaces
    set -- $rest                   # $1=state $2=ppid
    pid=$2
    hops=$((hops+1))
  done
  return 1
}

[ -s "$MB" ] || exit 0
if [ -z "${MAILBOX_FORCE:-}" ] && ! claude_ancestor; then
  echo "[mailbox] $(date) REFUSED drain of mailbox_${NAME}.md: caller pid=$$ has no live claude ancestor (orphaned poller of a killed worker?) — mail left in place for the live worker/relaunch" \
    >> scratch_full_logs/inbox_agent.log
  exit 0
fi
(
  flock 9
  cat "$MB"
  cat "$MB" >> "scratch_full_logs/inbox/mailbox_${NAME}.processed.md"   # keep an archive
  : > "$MB"                                                             # clear (consumed)
) 9>"$LOCK"
