#!/usr/bin/env python3
"""history.py — per-repo JSONL flake history. The compounding asset. (v0.1, 2026-10-08)

Each line: {"ts","repo","run_id","job","step","result":"fail"|"pass",
            "test","classification","evidence_line","consecutive_failures"}
- fail entries: step = first failing step; evidence_line = the model's decisive quote.
- pass entries: step = null (job-level pass — resets every failing step of that job).
- consecutive_failures: fail increments within a run sequence; pass resets to 0.
  v0.1: the 26h gap reset is GONE — only an observed PASS resets the streak
  (a 3-day silence with the same failure is still the same unfixed regression).

usage:
  history.py append <histfile> <run.json | inline-JSON> <classification> [test] [evidence_line]
  history.py pass   <histfile> <repo> <run_id> <job>
  history.py show   <histfile> [repo]
  history.py streak <histfile> <repo> <job> [step]
  history.py recent <histfile> <repo> <job> [step] [limit]   # last N entries, pass-aware
"""
import json, sys
from datetime import datetime, timezone

def load(fn):
    try:
        return [json.loads(l) for l in open(fn) if l.strip()]
    except FileNotFoundError:
        return []

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def _write(fn, entry):
    with open(fn, "a") as fh:
        fh.write(json.dumps(entry) + "\n")

def _streak_after(h, key):
    """consecutive failures at the end of history for an exact (repo,job,step) key."""
    s = 0
    for e in reversed(h):
        if (e.get("repo"), e.get("job"), e.get("step")) == key:
            if e.get("result", "fail") == "fail":
                s = max(s, e.get("consecutive_failures", 1))
                break
            else:
                break
    return s

def append(fn, runmeta, classification, test=None, evidence=None):
    h = load(fn)
    repo = runmeta["repo"]
    for j in runmeta["jobs"]:
        if j.get("conclusion") != "failure":
            continue
        steps = [s for s in j.get("steps", []) if s.get("conclusion") == "failure"]
        step = steps[0]["name"] if steps else None
        key = (repo, j["name"], step)
        # dedupe: an entry for the same (repo, run_id, job, step) means this run was
        # already recorded (2026-10-08: double-append wart from a retried invocation).
        if any(e.get("repo") == repo and e.get("run_id") == runmeta.get("run_id")
               and e.get("job") == j["name"] and e.get("step") == step for e in h):
            print(f"history: {repo} / {j['name']} / {step} already recorded (dedupe)")
            continue
        streak = _streak_after(h, key) + 1
        entry = {"ts": now(), "repo": repo, "run_id": runmeta.get("run_id"), "job": j["name"],
                 "step": step, "result": "fail", "test": test, "classification": classification,
                 "evidence_line": evidence, "consecutive_failures": streak}
        h.append(entry); _write(fn, entry)
        print(f"history: {repo} / {j['name']} / {step} streak={streak} class={classification}")

def record_pass(fn, repo, run_id, job):
    h = load(fn)
    if any(e.get("run_id") == run_id and e.get("job") == job for e in h):
        return
    entry = {"ts": now(), "repo": repo, "run_id": run_id, "job": job, "step": None,
             "result": "pass", "test": None, "classification": None,
             "evidence_line": None, "consecutive_failures": 0}
    _write(fn, entry)
    print(f"history: {repo} / {job} PASS (resets streaks)")

def show(fn, repo=None):
    for e in load(fn)[-20:]:
        if repo and e.get("repo") != repo:
            continue
        print(json.dumps(e))

def streak(fn, repo, job, step=None):
    print(_streak_after(load(fn), (repo, job, step)))

def recent(fn, repo, job, step=None, limit=6):
    """last entries for (repo, job) with step match OR step=None (pass rows), newest first."""
    out = []
    for e in reversed(load(fn)):
        if e.get("repo") == repo and e.get("job") == job and (step is None or e.get("step") in (None, step)):
            out.append(e)
            if len(out) >= limit:
                break
    out.reverse()
    for e in out:
        print(json.dumps(e))

if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "append":
        meta_arg = sys.argv[3]
        try:
            with open(meta_arg) as fh:
                runmeta = json.load(fh)
        except (FileNotFoundError, IsADirectoryError):
            runmeta = json.loads(meta_arg)  # accept inline JSON (self-CI 37735374032: the round-trip test passes it inline)
        append(sys.argv[2], runmeta, sys.argv[4],
               sys.argv[5] if len(sys.argv) > 5 else None,
               sys.argv[6] if len(sys.argv) > 6 else None)
    elif cmd == "pass":
        record_pass(sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[5])
    elif cmd == "show":
        show(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    elif cmd == "streak":
        streak(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5] if len(sys.argv) > 5 else None)
    elif cmd == "recent":
        recent(sys.argv[2], sys.argv[3], sys.argv[4],
               sys.argv[5] if len(sys.argv) > 5 else None,
               int(sys.argv[6]) if len(sys.argv) > 6 else 6)
