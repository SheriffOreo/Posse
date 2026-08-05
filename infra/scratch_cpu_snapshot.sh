#!/usr/bin/env bash
# Task 153B: read-only snapshot of a worker's SURVIVING CPU jobs, for injection
# into a relaunched worker's resume prompt (companion to scratch_gpu_snapshot.sh).
#   Usage: bash scratch_cpu_snapshot.sh [worker_name]
# Sources, in order:
#   1. the survivor manifest scratch_full_logs/inbox/survivors_<worker>.jsonl the
#      surgical kill (scratch_kill_agent_only.py) wrote at interrupt time — each
#      pid re-verified via its /proc starttime so pid reuse can't lie;
#   2. a /proc sweep for processes whose stdout still points at this worker's
#      claude task-output dir (/tmp/claude-*/…/<session>/tasks/*.output) — catches
#      jobs from older interrupts even if the manifest is gone;
#   3. scratch_full_logs/<worker>*.pid pid-file liveness (same hook the GPU
#      snapshot reports).
# NEVER mutates anything: no killing, no file writes — safe to run at any time.
cd "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"   # portable: infra/ dir = code + state root
WORKER="${1:-}"
python3 - "$WORKER" <<'PY'
import glob, json, os, sys, time

worker = sys.argv[1] if len(sys.argv) > 1 else ""
now = time.time()

def fmt_t(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except Exception:
        return "?"

def clip(s, n=170):
    s = str(s)
    return s if len(s) <= n else s[:n] + "..."

def starttime_of(pid):
    try:
        stat = open(f"/proc/{pid}/stat").read()
        return int(stat.rsplit(")", 1)[1].split()[19])
    except Exception:
        return None

def out_info(path):
    """size + write-freshness of a surviving job's output file"""
    try:
        st = os.stat(path)
        return f"{st.st_size} B, last write {int(now - st.st_mtime)}s ago"
    except OSError:
        return "unreadable"

print(f"=== CPU SURVIVOR SNAPSHOT @ {fmt_t(now)}" + (f" (worker: {worker})" if worker else "") + " ===")

if not worker:
    mans = sorted(glob.glob("scratch_full_logs/inbox/survivors_*.jsonl"))
    print(f"No worker given. Survivor manifests on disk ({len(mans)}):")
    for m in mans:
        print(f"  {m} ({fmt_t(os.path.getmtime(m))})")
    if not mans:
        print("  (none)")
    sys.exit(0)

listed = set()

# 1) manifest written by the surgical kill at interrupt time
mpath = f"scratch_full_logs/inbox/survivors_{worker}.jsonl"
recs = []
try:
    with open(mpath) as f:
        for line in f:
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                pass
except OSError:
    pass

alive_n = 0
print(f"JOBS RECORDED AT LAST INTERRUPT ({len(recs)} in {mpath if recs else '(no manifest)'}):")
for r in recs:
    pid, st = r.get("pid"), r.get("starttime")
    is_alive = pid is not None and starttime_of(pid) == st
    listed.add(pid)
    tag = "ALIVE  " if is_alive else "gone   "
    if is_alive:
        alive_n += 1
    print(f"  [{tag}] pid {pid} (recorded {fmt_t(r.get('recorded_at'))})")
    print(f"      cmd: {clip(r.get('cmd', '?'))}")
    if r.get("out"):
        print(f"      out: {r['out']} ({out_info(r['out']) if is_alive else 'job ended - check artifacts'})")
if not recs:
    print("  (none)")

# 2) sweep: processes still writing into this worker's claude task-output dir
sid = ""
try:
    for j in json.load(open("scratch_full_logs/watchdog_jobs.json")):
        if j.get("name") == worker:
            sid = j.get("session") or ""
except Exception:
    pass
if not sid:
    try:
        reg = json.load(open("scratch_agents_registry.json"))
        sid = (reg.get("workers", {}).get(worker) or {}).get("session") or ""
    except Exception:
        pass

def ancestors(pid):
    out, hops = set(), 0
    while pid > 1 and hops < 30:
        out.add(pid)
        try:
            pid = int(open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[1])
        except Exception:
            break
        hops += 1
    return out

self_chain = ancestors(os.getpid())   # don't list the snapshot's own call chain

extra = []
if sid:
    for stat_p in glob.glob("/proc/[0-9]*/fd/1"):
        try:
            pid = int(stat_p.split("/")[2])
            if pid in listed or pid in self_chain:
                continue
            tgt = os.readlink(stat_p)
            if f"/{sid}/tasks/" in tgt:
                cmd = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="replace").strip()
                if os.path.basename(cmd.split()[0] if cmd.split() else "") == "claude":
                    continue          # the (re)launched agent itself, not a job
                extra.append((pid, cmd, tgt))
        except (OSError, ValueError):
            continue
print(f"OTHER LIVE PROCESSES STILL WRITING TO THIS WORKER'S TASK OUTPUTS ({len(extra)}):"
      + ("" if sid else " (no session id found)"))
for pid, cmd, tgt in extra:
    print(f"  pid {pid}: {clip(cmd, 150)}")
    print(f"      out: {tgt} ({out_info(tgt)})")
if not extra:
    print("  (none)")

# 3) pid-file convention (shared with the GPU snapshot)
pidfiles = sorted(glob.glob(f"scratch_full_logs/{worker}*.pid"))
print(f"TRACKED PID FILES (scratch_full_logs/{worker}*.pid, {len(pidfiles)}):")
for p in pidfiles:
    try:
        pid = int(open(p).read().split()[0])
        print(f"  {os.path.basename(p)}: pid {pid} {'ALIVE' if os.path.exists(f'/proc/{pid}') else 'dead'}")
    except Exception:
        print(f"  {os.path.basename(p)}: unreadable")
if not pidfiles:
    print("  (none)")

if not recs and not extra and not pidfiles:
    print("No surviving CPU jobs recorded for this worker (nothing was running at the interrupt, "
          "or it predates Task 153B).")
else:
    print(f"SUMMARY: {alive_n} manifest job(s) still ALIVE, {len(extra)} additional live task-output writer(s).")
PY
