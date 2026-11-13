#!/usr/bin/env python3
"""Test of the `gate` job of .github/workflows/ci.yaml. The script is taken out of the YAML as written, run with bash and fed what
GitHub gives it (NEEDS = toJSON(needs), EVENT_NAME). The workflow has two lanes (the `classify` job: image-only | full); this test
holds the expected result of every job PER LANE on its own (the tables LANE_* below, not read from the script) and checks the gate
against them: every combination of lane and result, a full-lane job that ran in the fast lane (classifier output ignored), a skipped
job in the full lane, a failed / cancelled / missing / garbled `classify`, manual runs. Also checks the wiring: gate needs EVERY other
job, runs always, no path filter, the full-lane-only jobs are conditioned on the lane and the always-jobs are not, every job has a
timeout, and the jobs the gate script knows are the jobs it needs.
Needs bash, jq and PyYAML.   usage: .github/scripts/test-gate.py [path/to/ci.yaml]    exit 1 and one report per failed scenario."""
import json, os, re, subprocess, sys, tempfile
import yaml

path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "workflows", "ci.yaml")
with open(path) as f:
    wf = yaml.safe_load(f)
jobs = wf["jobs"]
gate = jobs["gate"]
others = [j for j in jobs if j != "gate"]
step = next(s for s in gate["steps"] if s.get("name", "").startswith("Every job has the result"))
script = step["run"]
failures, count = [], 0


def wiring(name, ok, why=""):
    global count
    count += 1
    if not ok:
        failures.append(f"wiring: {name}: {why}")


# The contract, written down independently of the script: which jobs run in which lane.
ALWAYS = ["classify", "promotion-policy", "secret-scan", "verify-signatures"]
FULL_ONLY = ["lint-and-render", "kubeconform", "trivy-config", "kyverno-policies", "workflows"]
LANE_IMAGE_ONLY = {**{j: "success" for j in ALWAYS}, **{j: "skipped" for j in FULL_ONLY}}
LANE_FULL = {j: "success" for j in ALWAYS + FULL_ONLY}
FULL_IF = "needs.classify.outputs.lane == 'full'"

triggers = wf.get("on", wf.get(True))  # PyYAML reads the key `on` as True
wiring("the table covers every job", sorted(ALWAYS + FULL_ONLY) == sorted(others), f"table {sorted(ALWAYS + FULL_ONLY)}, jobs {sorted(others)}")
wiring("gate needs every other job", sorted(gate["needs"]) == sorted(others), f"needs {sorted(gate['needs'])}, jobs {sorted(others)}")
wiring("gate always runs", "always()" in str(gate.get("if", "")), f"if: {gate.get('if')!r}")
m = re.search(r"^\s*jobs=\(([^)]*)\)", script, re.M)
wiring("the script lists the same jobs", bool(m) and sorted(m.group(1).split()) == sorted(others), f"script {m and m.group(1)}, jobs {others}")
m = re.search(r"^\s*full_only=\(([^)]*)\)", script, re.M)
wiring("the script lists the same full-lane-only jobs", bool(m) and sorted(m.group(1).split()) == sorted(FULL_ONLY), f"script {m and m.group(1)}, table {FULL_ONLY}")
wiring("pull_request has no path filter", not any(k in (triggers.get("pull_request") or {}) for k in ("paths", "paths-ignore")),
       "a workflow skipped by a path filter leaves its required checks pending forever")
wiring("no merge_group trigger", "merge_group" not in triggers, "not wanted")
wiring("promotion-policy runs on every pull request", "if" in jobs["promotion-policy"] and jobs["promotion-policy"]["if"] == "github.event_name == 'pull_request'", "its `if` changed")
wiring("promotion-policy does not wait for the lane", not jobs["promotion-policy"].get("needs"), f"needs {jobs['promotion-policy'].get('needs')}")
wiring("secret-scan has no lane condition", not jobs["secret-scan"].get("needs") and "if" not in jobs["secret-scan"], "it must run in both lanes")
wiring("classify has no needs and no condition", not jobs["classify"].get("needs") and "if" not in jobs["classify"], "it is the first job")
wiring("classify has no credentials", all(st.get("with", {}).get("persist-credentials") is False for st in jobs["classify"]["steps"] if str(st.get("uses", "")).startswith("actions/checkout")), "persist-credentials: false")
wiring("classify checks out the whole history", any(st.get("with", {}).get("fetch-depth") == 0 for st in jobs["classify"]["steps"]), "fetch-depth: 0")
wiring("classify exposes lane and images", set(jobs["classify"].get("outputs", {})) >= {"lane", "images"}, f"{jobs['classify'].get('outputs')}")
for j in FULL_ONLY:
    needs_j = jobs[j].get("needs") or []
    needs_j = [needs_j] if isinstance(needs_j, str) else needs_j
    wiring(f"{j} needs classify and runs only in the full lane", "classify" in needs_j and str(jobs[j].get("if", "")).strip() in (FULL_IF, "${{ " + FULL_IF + " }}"),
           f"needs {needs_j}, if {jobs[j].get('if')!r}")
for j in ("kubeconform", "trivy-config"):
    wiring(f"{j} still waits for the render", "lint-and-render" in jobs[j]["needs"], f"needs {jobs[j]['needs']}")
wiring("kyverno-policies does not wait for the render", jobs["kyverno-policies"]["needs"] == ["classify"], f"needs {jobs['kyverno-policies']['needs']}")
vs = jobs["verify-signatures"]
wiring("verify-signatures runs in both lanes", set(vs["needs"]) == {"classify", "lint-and-render"} and "!cancelled()" in vs["if"]
       and "needs.classify.outputs.lane == 'image-only'" in vs["if"] and "needs.lint-and-render.result == 'success'" in vs["if"]
       and "needs.classify.result == 'success'" in vs["if"], f"needs {vs['needs']}, if {vs['if']!r}")
wiring("verify-signatures downloads the render only in the full lane", any("download-artifact" in str(st.get("uses", "")) and st.get("if") == FULL_IF for st in vs["steps"]), "the artifact does not exist in the fast lane")
vsrun = " ".join(str(st.get("run", "")) for st in vs["steps"])
wiring("verify-signatures verifies the changed images in the fast lane and everything in the full lane", "verify-signatures.sh --images" in vsrun and "verify-signatures.sh rendered" in vsrun, "both forms")
for j, job in jobs.items():
    if "uses" in job and "steps" not in job:
        continue  # a call of a reusable workflow: `timeout-minutes` is not allowed here; the called workflow sets its own
    wiring(f"{j} has a timeout", isinstance(job.get("timeout-minutes"), int), "timeout-minutes")
wiring("gate is not a dependency of anything", all("gate" not in (j.get("needs") or []) for j in jobs.values() if isinstance(j.get("needs"), list)))


def run(needs, event="pull_request"):
    with tempfile.TemporaryDirectory() as tmp:
        summary = os.path.join(tmp, "summary.md")
        open(summary, "w").close()
        env = {"PATH": os.environ["PATH"], "NEEDS": json.dumps(needs), "EVENT_NAME": event, "GITHUB_STEP_SUMMARY": summary}
        p = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script], env=env, capture_output=True, text=True)
        return p.returncode, p.stdout, open(summary).read()


def needs(event="pull_request", lane="full", **override):
    """What GitHub hands the gate for a run in `lane` where every job behaves as the lane says; `override` changes single results."""
    table = LANE_IMAGE_ONLY if lane == "image-only" else LANE_FULL
    n = {j: {"result": table[j], "outputs": {}} for j in others}
    n["classify"]["outputs"] = {"lane": lane, "images": "mladenovskistefan/frontend@sha256:" + "a" * 64 if lane == "image-only" else ""}
    if event == "workflow_dispatch":
        n["promotion-policy"]["result"] = "skipped"
    for j, r in override.items():
        n[j.replace("_", "-")]["result"] = r
    return n


def fast(**override):
    return needs(lane="image-only", **override)


def check(name, expect_ok, n, event="pull_request", contains=(), absent=(), first=None):
    global count
    count += 1
    rc, out, summ = run(n, event)
    text = out + summ
    problems = []
    if (rc == 0) != expect_ok:
        problems.append(f"exit code {rc}, expected {'0' if expect_ok else 'non-zero'}")
    problems += [f"missing {s!r}" for s in contains if s not in text]
    problems += [f"unexpected {s!r}" for s in absent if s in text]
    if first:  # this job must come first in the report (log and table)
        for where, t in (("log", out), ("summary table", summ.split("| job |", 1)[-1])):
            rows = [l.split()[0] if where == "log" else l.split("|")[1].strip() for l in t.splitlines()
                    if (where == "log" and re.match(r"^[a-z-]+ +(success|failure|cancelled|skipped) ", l)) or (where == "summary table" and re.match(r"^\| [a-z-]+ \|", l))]
            if not rows or rows[0] != first:
                problems.append(f"{first} is not the first row of the {where}: {rows[:3]}")
    if problems:
        failures.append(f"{name}: {'; '.join(problems)}\n{text}")


# ---- full lane -----------------------------------------------------------------------------------------------------------------
check("full lane, all green (pull request)", True, needs(), contains=["lane: full", "| lint-and-render | success | success |"])
check("full lane, all green (manual run: promotion policy skipped)", True, needs("workflow_dispatch"), "workflow_dispatch", contains=["| promotion-policy | skipped | skipped |", "lane: full"])
check("manual run but the promotion policy ran", False, needs(), "workflow_dispatch", contains=["promotion-policy is 'success', expected 'skipped'"])
check("pull request but the promotion policy was skipped", False, needs(promotion_policy="skipped"), contains=["promotion-policy is 'skipped', expected 'success'"])
check("one job failed: named first", False, needs(trivy_config="failure"), contains=["trivy-config failure"], absent=["expected 'success'"], first="trivy-config")
check("the LAST job in the list failed: still first in the report", False, needs(workflows="failure"), contains=["workflows failure"], first="workflows")
check("a job was cancelled: named first", False, needs(kyverno_policies="cancelled"), contains=["kyverno-policies cancelled"], first="kyverno-policies")
check("failed and cancelled", False, needs(kubeconform="failure", secret_scan="cancelled"), contains=["kubeconform failure", "secret-scan cancelled"])
for job in ALWAYS + FULL_ONLY:
    if job == "classify":
        continue
    check(f"full lane, {job} skipped but nothing failed", False, needs(**{job: "skipped"}), contains=[f"{job} is 'skipped', expected 'success'"] if job != "promotion-policy" else [f"{job} is 'skipped'"])
check("the required job behind the render was skipped but nothing failed", False, needs(verify_signatures="skipped"), contains=["verify-signatures is 'skipped'"])
n = needs(lint_and_render="failure")
for j in ("kubeconform", "trivy-config", "verify-signatures"):
    n[j]["result"] = "skipped"
check("render failed, the three jobs behind it skipped: only the render is the cause", False, n,
      contains=["lint-and-render failure"], absent=["expected 'success'"], first="lint-and-render")
n = needs(promotion_policy="failure")
n["kubeconform"]["result"] = "skipped"
check("a failure and an unrelated skip: only the failure is reported", False, n, contains=["promotion-policy failure"], absent=["kubeconform is 'skipped'"])
n = needs()
del n["kyverno-policies"]
check("a job is missing from needs", False, n, contains=["job kyverno-policies is missing from needs"])
n = needs()
del n["workflows"]
check("the reusable-workflow job is missing from needs", False, n, contains=["job workflows is missing from needs"])
n = needs()
del n["classify"]
check("classify is missing from needs", False, n, contains=["job classify is missing from needs"])
check("a job nobody told the gate about", False, {**needs(), "lint-extra": {"result": "success", "outputs": {}}}, contains=["lint-extra", "(unknown job)", "expected '(unknown job)'"])
check("an unknown result value", False, needs(kubeconform="startup_failure"), contains=["kubeconform is 'startup_failure'"])
check("an event the workflow is not written for", False, needs(), "push", contains=["event 'push'"])
check("step summary has the table", True, needs(), contains=["| job | result | expected |", "| lint-and-render | success | success |", "### Gate (pull_request, lane: full)"])

# ---- image-only lane (the fast lane) -----------------------------------------------------------------------------------------------
check("fast lane, all good: the full-lane jobs skipped, the others green", True, fast(), contains=["lane: image-only", "| kubeconform | skipped | skipped |", "| verify-signatures | success | success |"])
for job in FULL_ONLY:
    check(f"fast lane, {job} RAN (classifier output ignored): the gate fails", False, fast(**{job: "success"}),
          contains=[f"{job} ran in the image-only lane", "the classifier output was ignored"])
check("fast lane, ALL full-lane jobs ran", False, needs(lane="image-only", **{j: "success" for j in FULL_ONLY}), contains=["the classifier output was ignored"])
for job in ("verify-signatures", "promotion-policy", "secret-scan"):
    check(f"fast lane, {job} skipped (an always-job must run)", False, fast(**{job: "skipped"}), contains=[f"{job} is 'skipped', expected 'success' (lane: image-only)"])
check("fast lane, verify-signatures failed: named first", False, fast(verify_signatures="failure"), contains=["verify-signatures failure"], absent=["expected 'success'"], first="verify-signatures")
check("fast lane, promotion-policy failed: named first, the skipped full-lane jobs are not findings", False, fast(promotion_policy="failure"), contains=["promotion-policy failure"], absent=["expected 'skipped'"], first="promotion-policy")
check("fast lane, secret-scan cancelled", False, fast(secret_scan="cancelled"), contains=["secret-scan cancelled"], first="secret-scan")
check("fast lane, a full-lane job failed (it should not even have run)", False, fast(kubeconform="failure"), contains=["kubeconform failure"])
check("fast lane, a full-lane job cancelled", False, fast(workflows="cancelled"), contains=["workflows cancelled"])
check("fast lane, a full-lane job has a result nobody expects", False, fast(trivy_config="startup_failure"), contains=["trivy-config is 'startup_failure', expected 'skipped'"])
check("fast lane in the step summary", True, fast(), contains=["### Gate (pull_request, lane: image-only)"])

# ---- classify itself ----------------------------------------------------------------------------------------------------------------
n = needs(classify="failure")
for j in others:
    if j not in ("classify", "secret-scan", "promotion-policy"):
        n[j]["result"] = "skipped"
check("classify failed: it is the cause, first; the skipped jobs behind it are consequences", False, n, contains=["classify failure"], absent=["expected 'success'"], first="classify")
n = needs(classify="cancelled")
for j in others:
    if j not in ("classify", "secret-scan", "promotion-policy"):
        n[j]["result"] = "skipped"
check("classify cancelled: first", False, n, contains=["classify cancelled"], first="classify")
n = needs(classify="failure")
n["classify"]["outputs"] = {}
check("classify failed and nothing else ran at all", False, {**n, **{j: {"result": "skipped", "outputs": {}} for j in others if j != "classify"}}, contains=["classify failure"], first="classify")
check("classify skipped (impossible, but fail closed)", False, needs(classify="skipped"), contains=["classify is 'skipped', expected 'success'"])
for bad in ("", "IMAGE-ONLY", "image-only ", "fast", "Full", "full image-only", "true"):
    n = needs()
    n["classify"]["outputs"] = {"lane": bad}
    check(f"classify succeeded with the lane {bad!r}: fails", False, n, contains=["neither image-only nor full"])
n = needs()
n["classify"]["outputs"] = {}
check("classify succeeded without a lane output: fails", False, n, contains=["neither image-only nor full"])
n = needs(lane="image-only")
n["classify"]["outputs"] = {"lane": "garbage"}
check("garbage lane and the fast-lane results: fails (strict expectations apply)", False, n, contains=["neither image-only nor full", "lint-and-render is 'skipped', expected 'success'"])

# ---- manual runs ----------------------------------------------------------------------------------------------------------------------
check("manual run claiming the fast lane: fails (it is always full)", False, needs("workflow_dispatch", lane="image-only"), "workflow_dispatch", contains=["only a pull request can be an image pin"])
check("manual run with fast-lane results", False, fast(), "workflow_dispatch", contains=["image-only"])
n = needs("workflow_dispatch")
n["kubeconform"]["result"] = "skipped"
check("manual run, full lane, kubeconform skipped", False, n, "workflow_dispatch", contains=["kubeconform is 'skipped', expected 'success'"])

if failures:
    print("\n\n".join(failures))
    print(f"\n{len(failures)} of {count} gate checks failed")
    sys.exit(1)
print(f"gate: {count} checks ok")
