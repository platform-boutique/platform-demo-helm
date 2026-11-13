#!/usr/bin/env python3
"""Which CI lane does this pull request get?   lane=image-only | full

image-only is the FAST LANE for the automation's image bump (branch promote/<app>/<env> or rollback/<app>/<env>, written by
gitops-promote.yaml / gitops-rollback.yaml of platform-demo-workflows): nothing but the image pin of ONE values file changed, so the
charts, schemas, policies and workflows cannot have changed and re-rendering and re-scanning them tells nothing new. Everything else is
the full pipeline. The rule is STRICT and fails closed: image-only only when ALL of this holds, anything else (including every parse
problem, a missing ref, an exception) is `full`.

  1. exactly one changed file (MAX_FILES), status M, mode 100644 before and after (no add, delete, rename, copy, symlink, mode change),
     and its path is envs/<dev|stage|prod>/values/<app>.yaml
  2. the file is small, UTF-8, has no CR and no NUL; the diff is small (at most MAX_PIN_LINES lines per side, no "no newline" marker)
  3. the file minus its image.tag / image.digest lines is IDENTICAL at the merge base and at the head. "Minus" is promotion-policy.sh's
     img_mask (the same awk the promotion policy and promote.sh use), the dropped lines are its img_pins
  4. every pin line (before and after) is in the exact form the promotion writes: `  tag: "<tag>"` and
     `  digest: sha256:<64 lowercase hex>` with an optional `   # vX.Y.Z`; one digest line, at most one tag line; the digest, tag and
     version that img_digest / img_tag / img_version read are the ones the form says
  5. every added and removed line of the unified diff is one of those pin lines
  6. the chart <app>/values.yaml at the head names an image repository of ours (mladenovskistefan/...), the one cosign verifies

Output: lane, reason, files, apps, envs, images (`repository@digest` of every changed file's digest, for verify-signatures.sh --images)
to $GITHUB_OUTPUT, a table to $GITHUB_STEP_SUMMARY, one line to stdout. Always exits 0 with a lane (a failure of the script itself is
`full`); exit 1 only when it cannot write its outputs, which makes the CI job fail and so the gate.
Environment:  BASE_REF (default origin/main)  HEAD_REF (default HEAD)  EVENT_NAME (default pull_request; any other event => full)
Test:         .github/scripts/test-classify-change.py        Needs: git, bash, awk, python3 (standard library only)."""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
POLICY = os.path.join(HERE, "promotion-policy.sh")

MAX_FILES = 1          # the automation opens one pull request per app and environment; a bigger change is a person's: full
MAX_BLOB = 64 * 1024   # bytes of a values file we are willing to parse
MAX_PIN_LINES = 2      # tag + digest, per side
GIT_TIMEOUT = 60
OWN_PREFIX = "mladenovskistefan/"

PATH_RE = re.compile(r"^envs/(dev|stage|prod)/values/([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)\.yaml$")
TAG = r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}"
DIGEST = r"sha256:[0-9a-f]{64}"
SEMVER = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
# The forms gitops-promote.yaml / promote.sh write: one space after the colon, the digest comment is exactly `   # vX.Y.Z`.
TAG_LINE = re.compile(rf'^( {{1,8}})tag: (?:"({TAG})"|\'({TAG})\'|({TAG}))$')
DIGEST_LINE = re.compile(rf'^( {{1,8}})digest: (?:"({DIGEST})"|\'({DIGEST})\'|({DIGEST}))(?:   # v({SEMVER}))?$')
REPO_RE = re.compile(r"^mladenovskistefan/[a-z0-9][a-z0-9._-]{0,127}$")


class Full(Exception):
    """The change is not an image pin (or cannot be shown to be one): the reason goes to the log and the summary."""


def git(*args, check=True):
    p = subprocess.run(["git", "-c", "core.quotepath=off", *args], capture_output=True, timeout=GIT_TIMEOUT)
    if check and p.returncode != 0:
        raise Full(f"git {' '.join(args[:2])} failed: {p.stderr.decode('utf-8', 'replace').strip()[:200]}")
    return p.stdout


def bash_fn(fn, text, *args):
    """Run a helper of promotion-policy.sh on `text` (stdin) and return its stdout. The parsing is theirs, not ours."""
    p = subprocess.run(["bash", "-c", 'source "$1"; shift; "$@"', "_", POLICY, fn, *args], input=text.encode(), capture_output=True,
                       timeout=GIT_TIMEOUT)
    if p.returncode != 0:
        raise Full(f"{fn} failed: {p.stderr.decode('utf-8', 'replace').strip()[:200]}")
    return p.stdout.decode()


def blob(rev, path):
    """Content of a file at a revision as text. Refuses what the pin parser was not written for."""
    spec = f"{rev}:{path}"
    size = git("cat-file", "-s", spec).decode().strip()
    if not size.isdigit() or int(size) > MAX_BLOB:
        raise Full(f"{path} is {size} bytes at {rev[:7]} (limit {MAX_BLOB})")
    raw = git("cat-file", "blob", spec)
    if b"\0" in raw or b"\r" in raw:
        raise Full(f"{path} has NUL or CR bytes (binary, or Windows line endings)")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise Full(f"{path} is not valid UTF-8")


def parse_pins(text, where):
    """-> (tag, digest, version) of the pin lines img_pins finds, each in the exact written form; Full otherwise."""
    pins = bash_fn("img_pins", text).splitlines()
    tag = digest = version = None
    n_tag = n_digest = 0
    indents = set()
    for line in pins:
        m = DIGEST_LINE.match(line)
        if m:
            n_digest += 1
            digest = next(g for g in m.group(2, 3, 4) if g)
            version = m.group(5) or ""
            indents.add(len(m.group(1)))
            continue
        m = TAG_LINE.match(line)
        if m:
            n_tag += 1
            tag = next(g for g in m.group(2, 3, 4) if g)
            indents.add(len(m.group(1)))
            continue
        raise Full(f"{where}: image line not in the written form: {line.strip()[:80]!r}")
    if n_digest != 1:
        raise Full(f"{where}: {n_digest} image.digest lines (exactly one expected)")
    if n_tag > 1:
        raise Full(f"{where}: {n_tag} image.tag lines")
    if len(pins) > MAX_PIN_LINES or len(indents) != 1:
        raise Full(f"{where}: unexpected image lines")
    # the same values by the parser the policy and promote.sh use
    if bash_fn("img_digest", text).strip() != digest or bash_fn("img_tag", text).strip() != (tag or "") \
            or bash_fn("img_version", text).strip() != (version or ""):
        raise Full(f"{where}: the image lines read differently by promotion-policy.sh")
    return tag or "", digest, version or ""


def chart_repository(head, app):
    try:
        values = blob(head, f"{app}/values.yaml")
    except Full as e:
        raise Full(f"no chart values for {app} to read the image repository from ({e})")
    repo = bash_fn("img_get", values, "repository").strip()
    if not REPO_RE.match(repo):
        raise Full(f"{app}/values.yaml: image.repository {repo!r} is not one of our images ({OWN_PREFIX}...)")
    return repo


def classify(base_ref, head_ref):
    """-> dict(lane='image-only', files, apps, envs, images, reason). Raises Full."""
    def rev(ref):
        out = git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False).decode().strip()
        if not re.fullmatch(r"[0-9a-f]{40,64}", out):
            raise Full(f"ref {ref!r} not found (is the checkout complete? fetch-depth: 0)")
        return out

    base, head = rev(base_ref), rev(head_ref)
    mb = git("merge-base", base, head, check=False).decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", mb):
        raise Full(f"no merge base between {base_ref} and {head_ref} (fetch-depth: 0)")

    # --- 1. which files, which kind of change
    raw = git("diff", "--raw", "--no-abbrev", "--no-renames", "--no-ext-diff", "-z", mb, head).split(b"\0")
    entries = []  # (oldmode, newmode, status, path)
    i = 0
    while i < len(raw) and raw[i]:
        meta = raw[i].decode("utf-8", "replace").lstrip(":").split()
        if len(meta) != 5 or i + 1 >= len(raw):
            raise Full("unreadable git diff --raw output")
        path = raw[i + 1].decode("utf-8", "replace")
        entries.append((meta[0], meta[1], meta[4], path))
        i += 2
    if not entries:
        raise Full("no changed files")
    if len(entries) > MAX_FILES:
        raise Full(f"{len(entries)} files changed (the automation changes {MAX_FILES}); first: {entries[0][3]}")
    oldmode, newmode, status, path = entries[0]
    m = PATH_RE.match(path)
    if not m:
        raise Full(f"{path} is not an envs/<env>/values/<app>.yaml file")
    env, app = m.group(1), m.group(2)
    if status != "M":
        raise Full(f"{path}: status {status} (added, deleted, renamed or type-changed), not a plain modification")
    if oldmode != "100644" or newmode != "100644":
        raise Full(f"{path}: file mode {oldmode} -> {newmode} (symlink, executable bit or submodule)")

    # --- 2. size of the change
    num = git("diff", "--numstat", "--no-renames", "--no-ext-diff", "-z", mb, head, "--", path).decode().split("\t")
    if len(num) < 2 or not (num[0].isdigit() and num[1].isdigit()):
        raise Full(f"{path}: binary or unreadable numstat")
    if int(num[0]) > MAX_PIN_LINES or int(num[1]) > MAX_PIN_LINES:
        raise Full(f"{path}: +{num[0]}/-{num[1]} lines (an image pin is at most {MAX_PIN_LINES} per side)")

    # --- 3. + 4. everything but the pin lines identical, pin lines in the written form
    old, new = blob(mb, path), blob(head, path)
    if bash_fn("img_mask", old) != bash_fn("img_mask", new):
        raise Full(f"{path}: something other than the image.tag / image.digest lines changed")
    old_pin = parse_pins(old, f"{path} before")
    new_pin = parse_pins(new, f"{path} after")

    # --- 5. every changed line of the unified diff is a pin line
    old_lines = set(bash_fn("img_pins", old).splitlines())
    new_lines = set(bash_fn("img_pins", new).splitlines())
    udiff = git("diff", "-U0", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", mb, head, "--", path).decode("utf-8", "replace")
    in_hunk = False
    for line in udiff.split("\n")[:-1]:  # split on \n only: other separators are content
        if line.startswith("@@"):
            in_hunk = True
        elif not in_hunk:
            continue
        elif line.startswith("-") and line[1:] in old_lines:
            continue
        elif line.startswith("+") and line[1:] in new_lines:
            continue
        else:
            raise Full(f"{path}: changed line is not an image pin line: {line[:80]!r}")
    if not in_hunk:
        raise Full(f"{path}: the diff has no hunk")

    # --- 6. what cosign has to verify
    repo = chart_repository(head, app)
    tag, digest, version = new_pin
    what = "digest" if old_pin[1] != digest else ("tag" if old_pin[0] != tag else "version comment")
    return {"lane": "image-only", "files": [path], "apps": [app], "envs": [env], "images": [f"{repo}@{digest}"],
            "reason": f"image pin only ({what}) in {path}"}


def full(reason, files=()):
    return {"lane": "full", "files": list(files), "apps": [], "envs": [], "images": [], "reason": " ".join(str(reason).split())[:300]}


def write_outputs(res):
    one = lambda v: " ".join(v)
    pairs = {"lane": res["lane"], "reason": res["reason"], "files": one(res["files"]), "apps": one(res["apps"]),
             "envs": one(res["envs"]), "images": one(res["images"])}
    for k, v in pairs.items():  # every value is validated or built from validated parts; belt and braces for the output file
        if "\n" in v or "\r" in v:
            raise ValueError(f"newline in output {k}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            for k, v in pairs.items():
                f.write(f"{k}={v}\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"### Change classification: `{res['lane']}`\n\n{res['reason']}\n\n")
            if res["lane"] == "image-only":
                f.write("Fast lane: the full pipeline (helm lint and render, kubeconform, Trivy, Kyverno tests, workflow lint) is skipped, "
                        "because only an image pin changed. Promotion policy, secret scan and the signature check of this digest run.\n\n"
                        "| file | app | env | image to verify |\n|---|---|---|---|\n")
                f.write(f"| `{res['files'][0]}` | {res['apps'][0]} | {res['envs'][0]} | `{res['images'][0]}` |\n")
            else:
                f.write("Full pipeline. (Any change that is not exactly one image pin of one values file is full: that is the safe side.)\n")
    print(f"lane={pairs['lane']}  {pairs['reason']}" + (f"  images={pairs['images']}" if pairs["images"] else ""))


def main():
    event = os.environ.get("EVENT_NAME", "pull_request")
    base = os.environ.get("BASE_REF", "origin/main")
    head = os.environ.get("HEAD_REF", "HEAD")
    try:
        if event != "pull_request":
            res = full(f"event '{event}': there is no pull request to compare, so the whole pipeline runs")
        else:
            res = classify(base, head)
    except Full as e:
        res = full(e)
    except Exception as e:  # noqa: BLE001 - fail closed: whatever went wrong, the full pipeline runs
        res = full(f"classifier error ({type(e).__name__}: {e})")
    try:
        write_outputs(res)
    except Exception as e:  # noqa: BLE001
        print(f"::error::cannot write the outputs: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
