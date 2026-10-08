# Verdict contract (frozen 2026-10-08)

Input to the cheap model: the failed job's log tail (last ~2000 lines), the run metadata
(branch, sha, workflow name), and the last 5 history.py entries for this (repo, job, step).

Output: strict JSON, no prose:

```json
{
  "classification": "flaky | regression | infra | config | unknown",
  "action": "rerun | fix | check | none",
  "confidence": 0.0,
  "evidence": "<one line, quoting the decisive log line>",
  "test": "<failing test name if identifiable, else null>"
}
```

Bias rules (the whole point of the product — stop the rerun-spam loop):
1. If this (repo, job, step) has `consecutive_failures >= 2` in history, classification
   defaults to `flaky` and action to `rerun` UNLESS the log shows a NEW distinct error
   (different test, different traceback head) — then `fix`, action `check`.
2. `infra` = network/registry timeout, OOM-killed, runner disk full, `apt-get`/npm registry
   5xx, sandbox/permissions. These are almost always `rerun` for the FIRST occurrence.
3. `regression` = a deterministic assertion/compile/type error with a plausible diff
   pointer. Action `fix`. Never auto-rerun twice in a row.
4. If the model is <0.6 confidence on classification, output `unknown` + `rerun` once.
   Commenting on the run is only allowed when action != rerun OR streak >= 3.
5. The comment (when allowed) is ≤3 lines: classification, evidence line, one suggestion.
   No emoji, no "Hope this helps!", signed "ci-triage (a Pennyforge tool)".
