# ci-triage

CI failure triage for small repos. When a required check goes red, it tells you — in
three lines — whether to **re-run** (infra/flaky), **fix** (regression), or **check**
(something to look at yourself), based on the actual job log plus this repo's own
flake history. It never auto-retries. It never rewrites your config. It comments only
when it says something.

Built as a dated, dogfooded proof artifact for a one-person studio (Pennyforge).

## What it does

```
failed run → fetch run + all failed-job logs (plain text)
           → per-(repo, job, step) flake history (JSONL, committed per repo)
           → cheap-LLM verdict against a frozen contract (verdict.md)
           → ≤3-line comment on the run, only when actionable
```

The compounding asset is the flake history: the same (repo, job, step) failing again
is a flake; a NEW distinct error is a regression. That distinction, kept per repo over
time, is what kills the infinite re-run loop that small teams live in.

## Pieces

| file | role | status (2026-10-08) |
|---|---|---|
| `fetch_run.py` | Actions API: run + failed-job logs, decoded to plain text | **verified live** (see below) |
| `history.py` | per-repo JSONL flake history: append / show / streak | **verified** (live run appended) |
| `verdict.md` | frozen LLM verdict contract (bias to re-run, comment only when it matters) | frozen |
| `webhook.py` | GitHub App webhook → orchestration | **next** (App create + install) |
| `comment.py` | post/withhold the run comment per contract | **next** |

## Verified live — 2026-10-08

`fetch_run.py` against a real failing run (ChromeDevTools/chrome-devtools-mcp run
37646465760, created 2026-10-07 15:44Z, 2 failed jobs): both job logs fetched and
decoded (~1.1s each, 275 + 627 lines), metadata + per-step conclusions in
`live-test/run.json`, logs in `live-test/`.

That run also became **dogfood case 1 of n=30**: two red jobs, one root cause —
`src/McpContext.ts:13` must be `import type {HeapSnapshotManager}`. Verdict per
`verdict.md` contract: **regression / fix / 0.85** for both jobs (NOT flaky, do not
re-run; the lint error is deterministic and `--fix`-able, and the docs job's own diff
prints the pending fix). History streak for both (repo, job, step) keys set to 1.

Two real bugs found and fixed during verification (dated): the log endpoint's 302
target (signed blob URL) must not carry the GitHub auth header, and the blob serves
plain text (not the zip newer runs use) — decoder now handles both.

## Scope discipline (from the design review, 2026-09-30)

- read-only GitHub App (contents:read, checks:read, actions:read) + comment write
- cheap LLM only; verdict contract frozen before any tuning
- dogfood on the studio's own repos first; n=30 dated verdicts before "product"
- kill criteria: ≥1-in-5 false "fix" verdicts, or ≥10% missed real bugs, in the
  n=30 sample
- no auto-retry, no dashboard, no multi-user, no SaaS — until n=30 says otherwise

MIT.
