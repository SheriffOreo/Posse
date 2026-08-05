#!/usr/bin/env bash
# Task 170 F1: THE one way to append mail to a worker's live mailbox.
#   Usage: scratch_mailbox_append.sh <agent> <origin> <bodyfile>
#     <origin>  "uid=NNN from <sender>"   router-delivered USER mail
#               "relay from agent <name>" agent-injected relay (status pokes,
#                                         cross-agent nudges, etc.)
#     <bodyfile> file whose contents are the message body
# Appends, under the SAME flock the relaunch drain and the router use:
#   === mail <origin> delivered <date> ===
#   <body>
#
# Why the header matters: the relaunch scripts' mail-branch greps '^=== '
# header lines for 'uid=' to decide whether the mechanical "resumed on your
# email" ack is due. uid present -> user mail -> ack (truthful, quotes the
# uid); no uid -> agent relay -> NO mechanical ack (the resumed worker itself
# sends the substantive reply). Legacy headers already on disk keep working:
# the old router wrote '(uid=NNN)' which the same grep catches, and killfix-
# style '(relayed by <agent>)' has no uid= so it is treated as a relay.
#
# AGENTS: never append to scratch_full_logs/inbox/mailbox_*.md by hand —
# always go through this helper (or scratch_interrupt_worker.sh, which calls
# it and also does the kill+relaunch dance safely).
cd "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"   # portable: infra/ dir = code + state root
AGENT="${1:?agent}"; ORIGIN="${2:?origin (uid=N from <sender> | relay from agent <name>)}"; BODYFILE="${3:?bodyfile}"
IDIR=scratch_full_logs/inbox
MB="$IDIR/mailbox_${AGENT}.md"
LOCK="$IDIR/mailbox_${AGENT}.lock"
[ -f "$BODYFILE" ] || { echo "ERR: bodyfile not found: $BODYFILE" >&2; exit 1; }
mkdir -p "$IDIR"
{
  flock 9
  { echo "=== mail $ORIGIN delivered $(date) ==="
    cat "$BODYFILE"; echo; } >> "$MB"
} 9>"$LOCK"
[ -s "$MB" ] || { echo "ERR: mailbox append failed for $AGENT" >&2; exit 1; }
echo "appended to $MB (origin: $ORIGIN)"
