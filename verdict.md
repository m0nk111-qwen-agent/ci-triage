# Verdict contract (v0.2 — 2026-10-08)

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
7. Comment on the run ONLY when action != rerun OR consecutive_failures >= 3. ONE comment per
   run (<=3 lines: classification, evidence line quoted as code, one suggestion), no emoji,
   signed "ci-triage (a Pennyforge tool)". No bare confidence number in the comment (it is a
   model-internal estimate a maintainer cannot audit — it stays in hist.jsonl).
8. GROUNDING (v0.2): `evidence` must be a VERBATIM line of the fetched log; `test`, if not
   null, must appear verbatim in the log. Machine-checked before acceptance; any failure →
   downgrade to unknown/rerun and suppress the comment. (Also guards against prompt injection
   through attacker-controllable log content.)
9. FIRST OCCURRENCE (v0.2): if history has NO prior failure for this (repo, job, step), the
   verdict is never `fix` — a novel error is `unknown`/`rerun`. "fix" requires a comparable
   prior failure (normalized-evidence identity per rule 2) or a deterministic error with a
   file pointer that has REPEATED.
10. STEP SELECTION (v0.2): triage the FIRST failing step in execution order, excluding
    post/cleanup steps ("Post …", "Setup Job") — those fail after the real failure and
    mislead classification (v0.1 took the last failing step = the cleanup).
11. FRESHNESS (v0.2): suppress the comment if a NEWER run on the same branch already
    succeeded (the repo has moved past this failure; commenting would necro-triage).
12. STREAK ACCOUNTING (v0.2): history dedupes on (repo, run_id, job, step) — a re-processed
    run never inflates consecutive_failures; re-run attempts of one run are one failure.

v0.2 change log (2026-10-08, from two independent delegate reviews of v0.1 — GLM-5.3 skeptical
code review [CLOUD] + maintainer-POV comment critique [CLOUD-FREE]):
- Grounding check (rule 8): the highest-value ~30-line fix — machine-verifies evidence before
  any verdict is trusted; also kills prompt injection via log content.
- First-occurrence default inverted (rule 9): v0.1's rule-2 "DIFFERENT → fix" branch was
  vacuously true with no history, making "fix" the de-facto default verdict for any novel
  error the model scored ≥0.6. Now: nothing novel is "fix".
- Step selection (rule 10): v0.1 triaged the LAST failing step — on real runs that is often a
  Post/cleanup step that failed because of the earlier bug.
- Freshness suppression (rule 11): oldest-first backlog processing could comment on runs the
  branch already fixed.
- Streak dedupe (rule 12): double-processed runs inflated streaks and the ≥3 comment gate.
- Comment format (rule 7): no bare confidence; evidence quoted as code (a broken evidence line
  made v0.1 comments look like the bot's own parsing failure — maintainer-POV critique).
- Pass capture now records a pass even when the run_id already has a fail row (re-run passes =
  the interleaved-pass flaky signal v0.1 could not see).
- Defered to v0.3 (recorded, not fixed): attempt-number tracking + run-chronological context
  ordering; infra-persistent escalation (rule 3 has no defined precedence over rule 2 —
  a typo'd hostname retried forever is a real bug the bot would keep "rerunning");
  normalized (repo, workflow, job) key fallback for matrix/renamed steps; kill-criteria
  follow-up wiring (rerun suggestion vs. next run conclusion = free ground truth).

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
