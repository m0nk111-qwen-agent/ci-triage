#!/usr/bin/env python3
"""fetch_run.py — fetch a GitHub Actions run + all failed-job logs, plain text.

usage: python3 fetch_run.py <owner/repo> [run_id] [--out DIR]
  run_id omitted -> most recent run with conclusion=failure
outputs:
  <out>/run.json        run + job metadata (name, conclusion, duration, sha, ref, timestamps)
  <out>/job-<jobid>.log decoded plain-text log for each FAILED job
exits: 0 ok | 2 no failed run found | 3 api error | 4 log decode error
"""
import base64, io, json, os, sys, time, zipfile
import urllib.request, urllib.parse

API = "https://api.github.com"
TOKEN = os.environ.get("GITHUB_TOKEN", "")

class _NoAuthRedirect(urllib.request.HTTPRedirectHandler):
    """the log endpoint 302s to a signed blob URL that must NOT carry the GitHub auth header"""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if "://" not in newurl:
            newurl = urllib.parse.urljoin(req.full_url, newurl)
        return urllib.request.Request(newurl, headers={"User-Agent": "ci-triage", "Accept": "*/*"})

OPENER = urllib.request.build_opener(_NoAuthRedirect)

def get(url, binary=False):
    req = urllib.request.Request(url, headers={
        "Authorization": f"token {TOKEN}", "Accept": "application/vnd.github+json",
        "User-Agent": "ci-triage", "X-GitHub-Api-Version": "2022-11-28"})
    with OPENER.open(req, timeout=60) as r:
        data = r.read()
        return data if binary else json.loads(data)

def main():
    argv = [a for a in sys.argv[1:]]
    out = "out"
    if "--out" in argv:
        i = argv.index("--out"); out = argv[i+1]; del argv[i:i+2]
    slug = argv[0]
    run_id = int(argv[1]) if len(argv) > 1 else None
    os.makedirs(out, exist_ok=True)

    if run_id is None:
        runs = get(f"{API}/repos/{slug}/actions/runs?per_page=20")["workflow_runs"]
        failed = [r for r in runs if r.get("conclusion") == "failure"]
        if not failed:
            print(f"no failed runs in last {len(runs)} runs of {slug}", file=sys.stderr)
            return 2
        run = failed[0]
        run_id = run["id"]
        print(f"latest failed run: {run_id} ({run['name']}) {run['created_at']}", file=sys.stderr)

    run = get(f"{API}/repos/{slug}/actions/runs/{run_id}")
    jobs = get(f"{API}/repos/{slug}/actions/runs/{run_id}/jobs?per_page=100")["jobs"]

    meta = {
        "repo": slug, "run_id": run_id, "run_name": run["name"],
        "conclusion": run["conclusion"], "head_sha": run["head_sha"][:12],
        "ref": run.get("head_branch") or run.get("head_ref"), "created_at": run["created_at"],
        "run_at": run.get("run_attempt"), "path": run.get("head_repository", {}).get("full_name"),
        "jobs": [], "logs": []}
    nlogs = 0
    for j in jobs:
        entry = {"id": j["id"], "name": j["name"], "conclusion": j.get("conclusion"),
                 "started_at": j.get("started_at"), "completed_at": j.get("completed_at"),
                 "steps": [{"name": s["name"], "conclusion": s.get("conclusion")} for s in j.get("steps", [])]}
        meta["jobs"].append(entry)
        if j.get("conclusion") == "failure":
            t0 = time.time()
            try:
                raw = get(f"{API}/repos/{slug}/actions/jobs/{j['id']}/logs", binary=True)
                try:
                    zf = zipfile.ZipFile(io.BytesIO(raw))
                    txt = b"\n".join(zf.read(n) for n in zf.namelist()).decode("utf-8", "replace")
                except zipfile.BadZipFile:
                    txt = raw.decode("utf-8", "replace").lstrip("\ufeff")
            except Exception as e:
                print(f"job {j['id']} log decode error: {e}", file=sys.stderr)
                return 4
            fn = f"{out}/job-{j['id']}.log"
            open(fn, "w").write(txt)
            entry["log"] = fn
            entry["log_lines"] = txt.count("\n") + 1
            entry["log_bytes"] = len(txt)
            entry["fetch_s"] = round(time.time() - t0, 1)
            meta["logs"].append(fn)
            nlogs += 1
    json.dump(meta, open(f"{out}/run.json", "w"), indent=1)
    print(f"run {run_id}: {len(jobs)} jobs, {nlogs} failed-job logs fetched -> {out}/")
    return 0

if __name__ == "__main__":
    sys.exit(main())
