#!/usr/bin/env python3
"""ci-triage orchestrator — the dogfood pipeline. (v0.2, 2026-10-08)

For each configured repo: opportunistically record PASSES of recent runs, find NEW failed
runs (not in hist), fetch failed-job logs, render each failed job's verdict via a cheap
LLM (OpenAI-compatible endpoint) under verdict.md v0.1, append to the flake history, and
post a <=3-line comment when the contract says so.

Env:
  GITHUB_TOKEN     user PAT (dogfood) or GitHub App installation token
  TRIAGE_ENDPOINT  OpenAI-compatible chat endpoint
                   (default: guardian http://192.168.1.35:11434/v1/chat/completions)
  TRIAGE_API_KEY   Bearer key for the endpoint
  TRIAGE_MODEL     model name (default: google/gemini-3.5-flash-lite)

Usage:
  python3 triage.py                          # all REPOS_DEFAULT, live (may comment)
  python3 triage.py --dry                    # nothing posted
  python3 triage.py --repos A/B --run-id N   # force one specific run (test mode)
"""
import argparse, io, json, os, re, sys, time, urllib.parse, urllib.request, zipfile

HIST = "hist.jsonl"
OUT = "out"
REPOS_DEFAULT = "m0nk111-qwen-agent/ci-triage m0nk111-qwen-agent/nl-dmarc-census m0nk111-qwen-agent/pennyforge-site"
LOG_CAP = 5 * 1024 * 1024  # 5MB raw-log cap (skeptic 6c: cost bomb guard)

PROMPT = """You are ci-triage, a CI failure triage tool. Render a verdict for ONE failed GitHub Actions job, per this FROZEN contract (v0.1):

VERDICT CONTRACT (obey exactly; output ONLY a strict JSON object, no prose):
{{"classification": "flaky"|"regression"|"infra"|"config"|"unknown", "action": "rerun"|"fix"|"check"|"none", "confidence": <0-1>, "evidence": "<one line QUOTING the decisive log line, <=200 chars>", "test": "<failing test name or null>"}}

Rules:
- flaky = NON-DETERMINISTIC: the RECENT HISTORY below shows a PASS for this job AFTER the current failure streak began (fail -> pass -> fail). Only then: flaky/rerun.
- A stable error that repeats with NO pass in between is NOT flaky: if the decisive error line is IDENTICAL to a prior failure's evidence_line, it is the same unfixed bug -> regression/fix (call it a stable repeat). If the decisive error is DIFFERENT from prior -> regression/fix (new distinct bug).
- infra = network/DNS timeout, OOM-killed, disk full, npm/apt 5xx, runner/sandbox/permission -> rerun (even first occurrence).
- regression = deterministic assertion/compile/type/lint error with a file pointer -> fix.
- config = tool/config drift (new dependency version, changed config file) -> fix.
- FIRST occurrence (history shows no prior failure for this job/step): never "fix" — use "unknown"/"rerun". A novel error is not yet a proven regression (v0.2).
- Your "evidence" must be an EXACT verbatim line from the LOG EXCERPT (it is machine-checked against the full log). "test" must appear verbatim in the log, or be null (v0.2).
- confidence < 0.6 -> classification "unknown", action "rerun".

REPO: {repo}   JOB: {job}   FAILED STEP: {step}

RECENT HISTORY for this (repo, job) — newest last. result=fail rows carry evidence_line (the prior decisive quote); result=pass rows are job-level passes:
{history}

LOG EXCERPT (head + error-matched lines + tail; truncation marked):
{log_excerpt}

Return ONLY the JSON object."""


class _NoAuthRedirect(urllib.request.HTTPRedirectHandler):
    """The job-logs 302 target is a SIGNED BLOB URL: it must NOT carry the GitHub auth header,
    and it 415s on API-ish Accept values (2026-10-08, verified live) -> rebuild with Accept */*."""
    def redirect_request(self, rq, fp, code, msg, headers, newurl):
        if "://" not in newurl:
            newurl = urllib.parse.urljoin(rq.full_url, newurl)
        nr = urllib.request.Request(newurl, headers={"User-Agent": "ci-triage", "Accept": "*/*"})
        nr.method = rq.get_method()
        return nr

_OPENER = urllib.request.build_opener(_NoAuthRedirect)

def api(url, token, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Accept", "application/vnd.github+json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    with _OPENER.open(req, timeout=60) as r:
        return json.load(r)

def fetch_log(repo, run_id, job_id, token):
    # NOTE (2026-10-08): the nested /actions/runs/{id}/jobs/{id}/logs 404s for some runs;
    # the repo-scoped /actions/jobs/{job_id}/logs is the proven endpoint (302 -> signed blob).
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/actions/jobs/{job_id}/logs",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    raw = _OPENER.open(req, timeout=120).read()
    if len(raw) > LOG_CAP:
        raw = raw[-LOG_CAP:]
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
        names = [n for n in z.namelist() if n.endswith(".txt")]
        txt = z.read(sorted(names, key=len)[0]).decode("utf-8", "replace") if names else ""
    except zipfile.BadZipFile:
        txt = raw.decode("utf-8", "replace").lstrip("\ufeff")
    return txt.replace("\r\n", "\n").replace("\r", "\n")  # skeptic 6b: normalize

def first_failing_step(job):
    """v0.2 (GLM review #8): FIRST failing step in EXECUTION ORDER is the bug. Post/cleanup
    steps ('Post actions/cache', 'Setup Job') run after the real failure and fail on their own —
    the v0.1 reversed() order triaged the cleanup, not the bug."""
    fsteps = [s["name"] for s in job.get("steps", []) if s.get("conclusion") == "failure"]
    real = [n for n in fsteps if not n.startswith(("Post ", "Setup Job"))]
    return (real or fsteps or [None])[0]

def excerpt(text, cap=15000):
    lines = text.splitlines()
    pat = re.compile(r"(error|fail|exception|traceback|✖|npm ERR|eslint|cannot find|no such|timeout|E\d{3,}|exit code [1-9]|assert|panic|fatal)", re.I)
    picked, seen = [], set()
    def take(ls):
        for ln in ls:
            k = ln.strip()[:160]
            if k and k not in seen:
                seen.add(k); picked.append(ln)
    take(lines[:15])
    take([l for l in lines if pat.search(l)][:80])
    take(lines[-120:])
    out = "\n".join(picked)
    if len(out) > cap:
        out = out[:cap] + "\n[...truncated...]"
    return f"({len(lines)} lines total)\n{out}"

def load_hist():
    try:
        return [json.loads(l) for l in open(HIST) if l.strip()]
    except FileNotFoundError:
        return []

def _hist_write(entries):
    """flock'd append (GLM #13: overlapping invocations double-appended streaks)."""
    import fcntl
    with open(HIST, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        for e in entries:
            fh.write(json.dumps(e) + "\n")
        fcntl.flock(fh, fcntl.LOCK_UN)

def _norm(s):
    """Normalize for rule-2 identity (GLM #3b): durations/ports/timestamps change between runs,
    so byte-identity never matched for common flake shapes. digits -> N, whitespace collapsed."""
    return re.sub(r"\s+", " ", re.sub(r"\d+", "N", str(s or ""))).strip()[:200]

def hist_context(hist, repo, job, step, limit=6):
    """pass-aware: entries for (repo, job) with step match OR step=None (pass rows)."""
    rows = [e for e in hist if e.get("repo") == repo and e.get("job") == job
            and (step is None or e.get("step") in (None, step))][-limit:]
    slim = [{"ts": e.get("ts"), "result": e.get("result", "fail"),
             "classification": e.get("classification"), "evidence_line": e.get("evidence_line"),
             "evidence_norm": e.get("evidence_norm") or _norm(e.get("evidence_line")),
             "consecutive_failures": e.get("consecutive_failures")} for e in rows]
    return slim or "(none — first recorded failure)"

def llm_verdict(endpoint, key, model, repo, job, step, hist_ctx, log_excerpt):
    p = PROMPT.format(repo=repo, job=job, step=step,
                      history=json.dumps(hist_ctx, indent=1), log_excerpt=log_excerpt)
    body = {"model": model, "messages": [{"role": "user", "content": p}], "max_tokens": 300, "temperature": 0}
    req = urllib.request.Request(endpoint, data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.load(r)
    txt = data["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        raise ValueError(f"no JSON in LLM output: {txt[:200]!r}")
    v = json.loads(m.group(0))
    for k in ("classification", "action", "confidence", "evidence"):
        if k not in v:
            raise ValueError(f"verdict missing {k}: {txt[:200]!r}")
    return v

def record_recent_passes(repo, token, hist, seen_runs):
    """Opportunistic pass capture: latest 8 runs; any run not yet in hist with a SUCCESSFUL
    job gets a pass row (resets that job's streaks). Bounded: 1 jobs-call per unseended run."""
    runs = api(f"https://api.github.com/repos/{repo}/actions/runs?per_page=8", token).get("workflow_runs", [])
    for run in runs:
        rid = run["id"]
        if run.get("conclusion") != "success":
            continue
        # v0.2 (GLM #2a): record the pass even when a FAIL row for this run_id already exists —
        # a re-run pass after a recorded failure (same run_id, attempt++) is exactly the
        # fail→pass→fail flaky signal v0.1 was structurally blind to.
        jobs = api(f"https://api.github.com/repos/{repo}/actions/runs/{rid}/jobs", token).get("jobs", [])
        new_passes = []
        for j in jobs:
            if j.get("conclusion") != "success":
                continue
            if any(e.get("run_id") == rid and e.get("result") == "pass" for e in hist):
                continue  # pass rows dedupe per (run_id, job)
            new_passes.append({"ts": time.strftime("%Y-%m-%dT%H:%M:%S+00:00"), "repo": repo, "run_id": rid,
                               "job": j["name"], "step": None, "result": "pass", "test": None,
                               "classification": None, "evidence_line": None, "consecutive_failures": 0})
        if new_passes:
            _hist_write(new_passes)
            hist.extend(new_passes)
            for e in new_passes:
                print(f"  history: {repo} / {e['job']} PASS run {rid}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--repos", nargs="+", default=REPOS_DEFAULT.split())
    ap.add_argument("--run-id", type=int, default=None)
    ap.add_argument("--no-passes", action="store_true", help="skip opportunistic pass capture (test mode)")
    a = ap.parse_args()
    token = os.environ.get("GITHUB_TOKEN", "")
    endpoint = os.environ.get("TRIAGE_ENDPOINT", "http://192.168.1.35:11434/v1/chat/completions")
    key = os.environ.get("TRIAGE_API_KEY", "")
    model = os.environ.get("TRIAGE_MODEL", "google/gemini-3.5-flash-lite")
    hist = load_hist()
    seen_runs = {e["run_id"] for e in hist if e.get("run_id")}
    os.makedirs(OUT, exist_ok=True)
    results = []

    for repo in a.repos:
        if not a.no_passes:
            record_recent_passes(repo, token, hist, seen_runs)
        if a.run_id:
            run = api(f"https://api.github.com/repos/{repo}/actions/runs/{a.run_id}", token)
        else:
            runs = api(f"https://api.github.com/repos/{repo}/actions/runs?status=failure&per_page=10", token).get("workflow_runs", [])
            fresh = [x for x in runs if x["id"] not in seen_runs]
            if not fresh:
                print(f"{repo}: no new failed runs"); continue
            run = api(f"https://api.github.com/repos/{repo}/actions/runs/{fresh[0]['id']}", token)
        rid = run["id"]
        jobs = api(f"https://api.github.com/repos/{repo}/actions/runs/{rid}/jobs", token).get("jobs", [])
        failed = [j for j in jobs if j.get("conclusion") == "failure"]
        if not failed:
            print(f"{repo} run {rid}: no failed jobs"); continue
        print(f"{repo} run {rid}: {len(failed)} failed job(s)")
        comment_lines = []
        new_entries = []
        for j in failed:
            step = first_failing_step(j) or "(whole job)"
            ctx = hist_context(hist, repo, j["name"], step)
            streak = 0
            for e in reversed(hist):
                if e.get("repo") == repo and e.get("job") == j["name"] and e.get("step") == step:
                    if e.get("result", "fail") == "fail":
                        streak = e.get("consecutive_failures", 1); break
                    break
            text = fetch_log(repo, rid, j["id"], token)
            log_path = f"{OUT}/{repo.replace('/', '_')}-{rid}-{j['id']}.log"
            open(log_path, "w", encoding="utf-8").write(text)
            v = llm_verdict(endpoint, key, model, repo, j["name"], step, ctx, excerpt(text))
            # v0.2 grounding (GLM #18): evidence must be a verbatim log line (kills fabricated
            # quotes AND prompt injection via log content); test name must appear in the log.
            ev0 = str(v.get("evidence") or "")
            if not ((ev0 and ev0 in text) and ((not v.get("test")) or (v["test"] in text))):
                v = {**v, "classification": "unknown", "action": "rerun",
                     "confidence": min(float(v.get("confidence") or 0), 0.59),
                     "evidence": ev0 + " [v0.2: not a verbatim log line — downgraded]"}
                print(f"    grounding: downgraded to unknown/rerun")
            # v0.2 (GLM #1): nothing NOVEL is 'fix' — first occurrence has no prior evidence to
            # compare against, so the DIFFERENT-branch was vacuously true and 'fix' was the default.
            prior = [e for e in hist if e.get("repo") == repo and e.get("job") == j["name"]
                     and e.get("step") == step and e.get("result", "fail") == "fail"
                     and e.get("evidence_line")]
            if v["action"] == "fix" and not prior:
                v = {**v, "classification": "unknown", "action": "rerun",
                     "evidence": str(v.get("evidence") or "") + " [v0.2: first occurrence]"}
                print(f"    first-occurrence: downgraded to unknown/rerun")
            entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S+00:00"), "repo": repo, "run_id": rid,
                     "job": j["name"], "step": step, "result": "fail", "test": v.get("test"),
                     "classification": v["classification"], "evidence_line": v.get("evidence"),
                     "evidence_norm": _norm(v.get("evidence")),
                     "contract": "v0.2",
                     "consecutive_failures": streak + 1}
            new_entries.append(entry)
            print(f"  [{j['name']}] {v['classification']}/{v['action']} conf={v['confidence']} :: {str(v.get('evidence'))[:100]}")
            if v["action"] != "rerun" or entry["consecutive_failures"] >= 3:
                # v0.2 comment format (2026-10-08, maintainer-POV critique): no bare confidence
                # number (it reads as a probability claim a maintainer cannot audit); evidence is
                # quoted as code, capped at 120 chars, so a broken evidence line cannot make the
                # comment look like the bot's own parsing failure. Confidence stays in hist.jsonl.
                ev = str(v.get("evidence") or "").strip().replace("`", "'")[:120]
                comment_lines.append(f"- **{j['name']}** ({step}): {v['classification']} → {v['action']} — `{ev}`")
            results.append({"repo": repo, "run": rid, "job": j["name"], "step": step, "verdict": v, "log": log_path})
        if new_entries:
            _hist_write(new_entries)
            hist.extend(new_entries)
            seen_runs.add(rid)
        if comment_lines:
            if a.dry:
                print(f"  [dry] would comment {len(comment_lines)} line(s)")
            else:
                # v0.2 (GLM #14): if a newer run on the same branch already succeeded, the verdict
                # is stale (the repo moved past this failure) — suppress instead of necro-commenting.
                br = run.get("head_branch", "")
                newest = api(f"https://api.github.com/repos/{repo}/actions/runs?per_page=1&branch={urllib.parse.quote(br)}", token).get("workflow_runs", [])
                if newest and newest[0]["id"] != rid and newest[0].get("conclusion") == "success":
                    print(f"  [suppressed] newer run {newest[0]['id']} on {br} already succeeded")
                    comment_lines = []
        if comment_lines:
            body = {"body": "ci-triage verdict:\n" + "\n".join(comment_lines[:3]) + "\n\n_— ci-triage (a Pennyforge tool)_"}
            api(f"https://api.github.com/repos/{repo}/actions/runs/{rid}/comments", token, body)
            print(f"  comment posted ({len(comment_lines)} line(s))")
    with open(f"{OUT}/verdicts.json", "a") as fh:
        fh.write(json.dumps(results, indent=1) + "\n")
    print(f"done: {len(results)} job verdict(s)")

if __name__ == "__main__":
    main()
