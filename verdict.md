# Verdict contract (v0.1 — 2026-10-08, revised from the 05:3xZ freeze the same day)

Input to the cheap model: the failed job's log EXCERPT (head + error-matched lines + tail,
capped ~15KB; full log stays on disk), the run metadata (repo, branch, sha), and the last
~6 history.py entries for this (repo, job) — INCLUDING pass entries and each failure's
`evidence_line`, so the model can compare this error against prior ones.

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
1. **FLAKY = non-deterministic, defined by an interleaved PASS.** If the recent history for
   this (repo, job) shows a PASS after the current failure streak began (fail → pass → fail),
   the job passes without a code change → `flaky` / `rerun`.
   A stable error that repeats with NO pass in between is NOT flaky — it is the same
   unfixed bug. (v0.1 fix: the old "streak>=2 → flaky unless NEW error" rule misclassified
   byte-identical stable regressions as flaky from their 2nd repeat; live case
   chrome-devtools-mcp run 37646465760 is exactly this shape.)
2. **Stable repeat (no interleaved pass):** decisive error line identical to the prior
   failure's `evidence_line` → `regression` / `fix`, note it is a stable repeat ("same
   error N runs in a row — still unfixed; a rerun will not change it").
   Decisive error line DIFFERENT from prior → `regression` / `fix` (a new distinct bug).
3. `infra` = network/registry timeout, DNS, OOM-killed, runner disk full, npm/apt 5xx,
   sandbox/permissions → `rerun` (even on first occurrence).
4. `regression` = deterministic assertion/compile/type/lint error with a plausible file
   pointer. Action `fix`. Never auto-rerun the same deterministic error twice.
5. `config` = tool/config drift (new dependency version, changed config) → `fix`.
6. If <0.6 confidence → `unknown` + `rerun` once (the cheap honest answer).
7. Comment on the run ONLY when action != rerun OR consecutive_failures >= 3. Comment <=3
   lines (classification, evidence line, one suggestion), no emoji, signed
   "ci-triage (a Pennyforge tool)".

v0.1 change log (2026-10-08):
- flaky redefined to interleaved-pass (was: streak>=2 minus "new distinct error").
  Reason: skeptical review (CLOUD-DEEPSEEK delegate) + the live case — a deterministic
  error is byte-identical every run, so the old escape hatch fired only on genuinely new
  errors and the 2nd+ repeat of a stable regression became silent flaky/rerun.
- 26h gap reset removed from history.py — only an observed PASS resets a streak (a 3-day
  silence is "no evidence", not "healed").
- log input changed from "tail 2000 lines" to a capped excerpt + prior evidence lines
  (distinctness check is the contract's most consequential decision; the model needs both
  sides of the comparison).
