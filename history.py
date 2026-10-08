#!/usr/bin/env python3
"""history.py — per-repo JSONL flake history. The compounding asset.

Each line: {"ts","repo","run_id","job","step","test","classification","consecutive_failures"}
- `test` = best-effort failing-test identifier (pytest/node test name from log tail),
  or null if not identifiable.
- `consecutive_failures` = how many consecutive runs this (repo, job, step-or-test)
  has failed — the number that gates auto-rerun (>=2 = re-run bias; 1 = fresh failure).

usage:
  history.py append <histfile> <run.json> <classification> [test]
  history.py show   <histfile> [repo]        # tail 20 relevant entries
  history.py streak <histfile> <repo> <job> [step]  # current consecutive-failure streak
"""
import json, sys
from datetime import datetime, timezone

def load(fn):
    try:
        return [json.loads(l) for l in open(fn) if l.strip()]
    except FileNotFoundError:
        return []

def append(fn, runmeta, classification, test=None):
    h = load(fn)
    repo = runmeta["repo"]
    for j in runmeta["jobs"]:
        if j.get("conclusion") != "failure":
            continue
        # find first failing step as the stable key
        steps = [s for s in j.get("steps", []) if s.get("conclusion") == "failure"]
        step = steps[0]["name"] if steps else None
        key = (repo, j["name"], step)
        streak = 0
        for e in reversed(h):
            if (e.get("repo"), e.get("job"), e.get("step")) == key:
                if e.get("consecutive_failures"):
                    streak = max(streak, e["consecutive_failures"] + 1)
                break
            # any intervening success would reset; we only store failures,
            # so a gap of >26h between failures is treated as a reset
            if e.get("ts") and (datetime.now(timezone.utc) - datetime.fromisoformat(e["ts"])).total_seconds() > 26*3600:
                break
        entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "repo": repo,
                 "run_id": runmeta.get("run_id"), "job": j["name"], "step": step,
                 "test": test, "classification": classification,
                 "consecutive_failures": max(streak, 1)}
        h.append(entry)
        open(fn, "a").write(json.dumps(entry) + "\n")
        print(f"history: {repo} / {j['name']} / {step} streak={entry['consecutive_failures']} class={classification}")

def show(fn, repo=None):
    for e in load(fn)[-20:]:
        if repo and e.get("repo") != repo:
            continue
        print(json.dumps(e))

def streak(fn, repo, job, step=None):
    s = 0
    for e in reversed(load(fn)):
        if (e.get("repo"), e.get("job"), e.get("step")) == (repo, job, step):
            s = e.get("consecutive_failures", 1); break
    print(s)

if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "append":
        append(sys.argv[2], json.load(open(sys.argv[3])), sys.argv[4], sys.argv[5] if len(sys.argv) > 5 else None)
    elif cmd == "show":
        show(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    elif cmd == "streak":
        streak(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5] if len(sys.argv) > 5 else None)
