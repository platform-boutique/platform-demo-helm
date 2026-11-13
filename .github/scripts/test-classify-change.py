#!/usr/bin/env python3
"""Table-driven test of classify-change.py (the CI lane of a pull request). Every case builds a throw-away git repository (a base
commit on `main` shaped like this repository, a branch `pr` with the change), runs the classifier exactly as the CI job does
(BASE_REF / HEAD_REF / GITHUB_OUTPUT) and compares lane, reason and images. No network, no GitHub. Needs git, bash, awk, python3.

usage: .github/scripts/test-classify-change.py [--history N]
  --history N  also classify the last N commits of THIS repository whose subject starts with `promote(` or `rollback(` (the real
               bot pull requests, squash-merged: must be image-only) and the last N other commits (must not be). Slow-ish, optional.
Exit 1 and one report per failed case."""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "classify-change.py")
POLICY = os.path.join(HERE, "promotion-policy.sh")
D = lambda n: "sha256:" + format(n, "064x")  # n -> a valid digest
APPS = ("frontend", "cart-service")
failures, count = [], 0


# ---- the repository every case starts from ------------------------------------------------------------------------------------------

def values(digest, tag="sha-1111111", version=None, quote=True, extra=""):
    q = '"' if quote else ""
    comment = f"   # v{version}" if version else ""
    return f"image:\n  tag: {q}{tag}{q}\n  digest: {digest}{comment}\n{extra}"


FRONTEND_EXTRA = "networkPolicy:\n  ingress:\n    from: []\n    fromNamespaces: []           # chart default\n    fromEntities: [ingress]\n"
BASE_FILES = {
    "README.md": "# helm\n",
    ".github/workflows/ci.yaml": "name: CI\n",
    ".github/scripts/classify-change.py": "# the classifier\n",
    "envs/common.yaml": "global: {}\n",
    "envs/dev/common.yaml": "replicaCount: 1\n",
    "envs/dev/workloads.yaml": "- { app: frontend, chart: frontend, namespace: dev }\n",
    "policies/values.yaml": "verifyImages: {}\n",
    "docs/promote.md": "# promote\n",
    "frontend/Chart.yaml": "name: frontend\n",
    "frontend/values.yaml": "replicaCount: 1\nimage:\n  repository: mladenovskistefan/frontend\n  tag: ''\n",
    "frontend/templates/deployment.yaml": "kind: Deployment\n",
    "cart-service/values.yaml": "image:\n  repository: mladenovskistefan/cart-service\n",
    "third-party/values.yaml": "image:\n  repository: redis\n",
    "envs/dev/values/third-party.yaml": values(D(7)),
}
for env, ver in (("dev", None), ("stage", "1.0.0"), ("prod", "1.0.0")):
    BASE_FILES[f"envs/{env}/values/frontend.yaml"] = values(D(1), version=ver, extra=FRONTEND_EXTRA)
    BASE_FILES[f"envs/{env}/values/cart-service.yaml"] = values(D(2), tag="sha-2222222", version=ver)


def sh(cwd, *cmd, env=None, check=True, inp=None):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, input=inp, env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null",
                       "GIT_CONFIG_SYSTEM": "/dev/null", **(env or {})})
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}: {p.stderr}")
    return p


def git(cwd, *args, **kw):
    return sh(cwd, "git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", *args, **kw)


def write(root, rel, content):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.lexists(path):
        os.remove(path)
    with open(path, "wb") as f:
        f.write(content if isinstance(content, bytes) else content.encode())


tmp = tempfile.mkdtemp(prefix="classify-test-")
template = os.path.join(tmp, "template")
os.makedirs(template)
git(template, "init", "-q", "-b", "main", ".")
for rel, content in BASE_FILES.items():
    write(template, rel, content)
git(template, "add", "-A")
git(template, "commit", "-q", "-m", "base")


class Repo:
    def __init__(self, name):
        self.dir = os.path.join(tmp, name)
        git(tmp, "clone", "-q", template, self.dir)
        git(self.dir, "checkout", "-q", "-b", "pr")

    def read(self, rel):
        with open(os.path.join(self.dir, rel), "rb") as f:
            return f.read().decode()

    def write(self, rel, content):
        write(self.dir, rel, content)

    def commit(self, msg="change"):
        git(self.dir, "add", "-A")
        git(self.dir, "commit", "-q", "--allow-empty", "-m", msg)

    def main_commit(self, rel, content):
        """A commit on main made after the branch point (main moved on)."""
        git(self.dir, "checkout", "-q", "main")
        self.write(rel, content)
        self.commit("main moved")
        git(self.dir, "checkout", "-q", "pr")


def env_path(env, app):
    return f"envs/{env}/values/{app}.yaml"


# ---- the table -----------------------------------------------------------------------------------------------------------------------
# (name, change(repo), expected lane, text the reason must contain (or None), images expected (image-only only))
cases = []


def case(name, lane, reason=None, images=None):
    def deco(fn):
        cases.append((name, fn, lane, reason, images))
        return fn
    return deco


FE_IMG = lambda n: [f"mladenovskistefan/frontend@{D(n)}"]


# --- the automation's own shapes: image-only
@case("dev bump: tag and digest (promote/frontend/dev)", "image-only", "image pin only (digest)", FE_IMG(9))
def _(r): r.write(env_path("dev", "frontend"), values(D(9), tag="sha-9999999", extra=FRONTEND_EXTRA)); r.commit()


@case("stage bump: tag, digest and the # vX.Y.Z comment", "image-only", None, FE_IMG(9))
def _(r): r.write(env_path("stage", "frontend"), values(D(9), tag="sha-9999999", version="1.1.0", extra=FRONTEND_EXTRA)); r.commit()


@case("prod bump: comment version changes with the digest", "image-only", None, FE_IMG(9))
def _(r): r.write(env_path("prod", "frontend"), values(D(9), tag="sha-9999999", version="1.0.1", extra=FRONTEND_EXTRA)); r.commit()


@case("digest only, tag unchanged", "image-only", None, FE_IMG(9))
def _(r): r.write(env_path("dev", "frontend"), values(D(9), extra=FRONTEND_EXTRA)); r.commit()


@case("tag only, digest unchanged (the digest is still verified)", "image-only", "(tag)", FE_IMG(1))
def _(r): r.write(env_path("dev", "frontend"), values(D(1), tag="sha-abcdef0", extra=FRONTEND_EXTRA)); r.commit()


@case("version comment only (same digest gets its release version)", "image-only", "version comment", FE_IMG(1))
def _(r): r.write(env_path("stage", "frontend"), values(D(1), version="1.0.1", extra=FRONTEND_EXTRA)); r.commit()


@case("single-quoted and unquoted values", "image-only", None, FE_IMG(9))
def _(r): r.write(env_path("dev", "frontend"), "image:\n  tag: 'sha-9'\n  digest: '" + D(9) + "'\n" + FRONTEND_EXTRA); r.commit()


@case("bare tag", "image-only", None, FE_IMG(9))
def _(r): r.write(env_path("dev", "frontend"), values(D(9), tag="1.2.3", quote=False, extra=FRONTEND_EXTRA)); r.commit()


@case("another app: cart-service in prod", "image-only", None, [f"mladenovskistefan/cart-service@{D(9)}"])
def _(r): r.write(env_path("prod", "cart-service"), values(D(9), tag="sha-9", version="1.0.1")); r.commit()


@case("the tag line is added where there was none", "image-only", None, FE_IMG(9))
def _(r):
    r.write(env_path("dev", "frontend"), "image:\n  digest: " + D(1) + "\n" + FRONTEND_EXTRA); r.commit("notag")
    git(r.dir, "checkout", "-q", "main"); git(r.dir, "merge", "-q", "--ff-only", "pr"); git(r.dir, "checkout", "-q", "pr")
    r.write(env_path("dev", "frontend"), values(D(9), tag="sha-9", extra=FRONTEND_EXTRA)); r.commit()


@case("main moved on (chart change after the branch point): only the PR's own diff counts", "image-only", None, FE_IMG(9))
def _(r):
    r.write(env_path("dev", "frontend"), values(D(9), tag="sha-9", extra=FRONTEND_EXTRA)); r.commit()
    r.main_commit("frontend/templates/deployment.yaml", "kind: Deployment\nmoved: true\n")


@case("several commits on the branch that add up to one pin change", "image-only", None, FE_IMG(9))
def _(r):
    r.write(env_path("dev", "frontend"), values(D(5), tag="sha-5", extra=FRONTEND_EXTRA)); r.commit("one")
    r.write(env_path("dev", "frontend"), values(D(9), tag="sha-9", extra=FRONTEND_EXTRA)); r.commit("two")


@case("a comment line inside the image block is left alone", "image-only", None, FE_IMG(9))
def _(r):
    base = "image:\n  # pinned by the promotion\n  tag: \"a\"\n  digest: " + D(1) + "\n  pullPolicy: Always\n"
    r.write(env_path("dev", "frontend"), base); r.commit("base")
    git(r.dir, "checkout", "-q", "main"); git(r.dir, "merge", "-q", "--ff-only", "pr"); git(r.dir, "checkout", "-q", "pr")
    r.write(env_path("dev", "frontend"), base.replace(D(1), D(9)).replace('"a"', '"b"')); r.commit()


# --- everything that is not exactly an image pin: full
@case("image line plus ONE extra key in the same file", "full", "something other than the image")
def _(r): r.write(env_path("dev", "frontend"), values(D(9), extra=FRONTEND_EXTRA.replace("[ingress]", "[world]"))); r.commit()  # digest + 1 line: within the line budget


@case("image line plus an extra key INSIDE the image block", "full", "something other than the image")
def _(r): r.write(env_path("dev", "frontend"), values(D(9), extra=FRONTEND_EXTRA).replace("\n  digest", "\n  pullPolicy: Always\n  digest", 1).replace("sha-1111111", "sha-1111111")); r.commit()


@case("an unrelated key alone", "full", "something other than the image")
def _(r): r.write(env_path("dev", "frontend"), values(D(1), extra=FRONTEND_EXTRA.replace("from: []", "from: [x]"))); r.commit()


@case("a comment changed in the file", "full", "something other than the image")
def _(r): r.write(env_path("dev", "frontend"), values(D(1), extra=FRONTEND_EXTRA.replace("chart default", "other note"))); r.commit()


@case("two environments in one PR", "full", "2 files changed")
def _(r):
    for env in ("dev", "stage"):
        r.write(env_path(env, "frontend"), values(D(9), tag="sha-9", extra=FRONTEND_EXTRA))
    r.commit()


@case("two apps in one environment (a human's PR, not the automation's)", "full", "2 files changed")
def _(r):
    r.write(env_path("dev", "frontend"), values(D(9), tag="sha-9", extra=FRONTEND_EXTRA))
    r.write(env_path("dev", "cart-service"), values(D(8), tag="sha-8")); r.commit()


@case("a file rename (values file moved to another app name)", "full", "2 files changed")
def _(r): git(r.dir, "mv", env_path("dev", "cart-service"), env_path("dev", "cart-service2")); r.commit()


@case("a rename with a one-line image edit (and a new chart file)", "full", "files changed")
def _(r):
    git(r.dir, "mv", env_path("dev", "cart-service"), env_path("dev", "cart-service2"))
    r.write(env_path("dev", "cart-service2"), values(D(9), tag="sha-9")); r.write("cart-service2/values.yaml", "image:\n  repository: mladenovskistefan/x\n")
    r.commit()  # a rename plus a new chart file: more than one file


@case("a rename alone, no other change (--no-renames shows D+A)", "full", "files changed")
def _(r): git(r.dir, "mv", env_path("prod", "frontend"), env_path("prod", "frontend-copy")); r.commit()


@case("a values file deleted", "full", None)
def _(r): git(r.dir, "rm", "-q", env_path("dev", "cart-service")); r.commit()


@case("a new values file", "full", "status A")
def _(r): r.write(env_path("dev", "newapp"), values(D(9))); r.commit()


@case("the digest line removed", "full", None)
def _(r): r.write(env_path("dev", "frontend"), 'image:\n  tag: "sha-9"\n' + FRONTEND_EXTRA); r.commit()


@case("a second digest line added", "full", "2 image.digest lines")
def _(r): r.write(env_path("dev", "frontend"), values(D(1), extra=FRONTEND_EXTRA).replace('  tag:', '  digest: ' + D(9) + '\n  tag:', 1)); r.commit()


@case("a second tag line added", "full", "2 image.tag lines")
def _(r): r.write(env_path("dev", "frontend"), values(D(1), extra=FRONTEND_EXTRA).replace('  tag:', '  tag: "dup"\n  tag:', 1)); r.commit()


# values that merely look like a digest
LOOKALIKES = [
    ("uppercase hex", "sha256:" + "A" * 64),
    ("63 hex characters", "sha256:" + "a" * 63),
    ("65 hex characters", "sha256:" + "a" * 65),
    ("a non-hex character", "sha256:" + "a" * 63 + "g"),
    ("sha512 prefix", "sha512:" + "a" * 64),
    ("trailing text", "sha256:" + "a" * 64 + "x"),
    ("a tag instead of a digest", "latest"),
    ("a digest followed by a second value", "sha256:" + "a" * 64 + " extra"),
]
for _name, _val in LOOKALIKES:
    def _mk(val):
        def f(r): r.write(env_path("dev", "frontend"), f'image:\n  tag: "sha-9"\n  digest: {val}\n' + FRONTEND_EXTRA); r.commit()
        return f
    case(f"digest that merely looks like one: {_name}", "full", "not in the written form")(_mk(_val))


@case("a digest-looking value under another key of the image block", "full", "something other than the image")
def _(r): r.write(env_path("dev", "frontend"), values(D(1), extra=FRONTEND_EXTRA).replace('  digest:', '  other: ' + D(9) + '\n  digest:', 1)); r.commit()


@case("a digest line under ANOTHER top-level key changed", "full", "something other than the image")
def _(r):
    r.write(env_path("dev", "frontend"), values(D(1), extra=FRONTEND_EXTRA + "sidecar:\n  digest: " + D(3) + "\n")); r.commit("sidecar")
    git(r.dir, "checkout", "-q", "main"); git(r.dir, "merge", "-q", "--ff-only", "pr"); git(r.dir, "checkout", "-q", "pr")
    r.write(env_path("dev", "frontend"), values(D(1), extra=FRONTEND_EXTRA + "sidecar:\n  digest: " + D(9) + "\n")); r.commit()


@case("digest nested one level deeper than the image keys", "full", "something other than the image")
def _(r): r.write(env_path("dev", "frontend"), values(D(1), extra=FRONTEND_EXTRA).replace("digest: " + D(1) + "\n", "digest: " + D(1) + "\n  nested:\n    digest: " + D(9) + "\n")); r.commit()


@case("a foreign comment on the digest line", "full", "not in the written form")
def _(r): r.write(env_path("dev", "frontend"), values(D(9), extra=FRONTEND_EXTRA).replace(D(9), D(9) + "   # hand edited")); r.commit()


@case("a comment on the tag line", "full", "not in the written form")
def _(r): r.write(env_path("dev", "frontend"), values(D(9), extra=FRONTEND_EXTRA).replace('"sha-1111111"', '"sha-1111111" # x')); r.commit()


@case("a tag that is not a tag (quote injection)", "full", "not in the written form")
def _(r): r.write(env_path("dev", "frontend"), values(D(9), extra=FRONTEND_EXTRA).replace('"sha-1111111"', '"a\\" b: c"')); r.commit()


@case("a tag with a space or a slash", "full", "not in the written form")
def _(r): r.write(env_path("dev", "frontend"), values(D(9), tag="x/y z", extra=FRONTEND_EXTRA)); r.commit()


@case("a flow-style image block", "full", None)
def _(r): r.write(env_path("dev", "frontend"), "image: {tag: x, digest: " + D(9) + "}\n" + FRONTEND_EXTRA); r.commit()


@case("tab indentation", "full", "not in the written form")
def _(r): r.write(env_path("dev", "frontend"), "image:\n\ttag: \"x\"\n\tdigest: " + D(9) + "\n" + FRONTEND_EXTRA); r.commit()


@case("two spaces after the colon (not what the promotion writes)", "full", "not in the written form")
def _(r): r.write(env_path("dev", "frontend"), "image:\n  tag:  \"x\"\n  digest: " + D(9) + "\n" + FRONTEND_EXTRA); r.commit()


@case("Windows line endings: the whole file converted", "full", "lines (an image pin is at most")
def _(r): r.write(env_path("dev", "frontend"), values(D(9), tag="sha-9", extra=FRONTEND_EXTRA).replace("\n", "\r\n")); r.commit()


@case("Windows line endings only on the image lines", "full", "NUL or CR")
def _(r): r.write(env_path("dev", "frontend"), values(D(9), tag="sha-9", extra=FRONTEND_EXTRA).replace("sha-9\"\n", "sha-9\"\r\n")); r.commit()


@case("no newline at the end of the file (the diff carries a \"no newline\" marker): the safe side is full", "full", "not an image pin line")
def _(r):
    r.write(env_path("dev", "cart-service"), values(D(2), tag="sha-2222222").rstrip("\n")); r.commit("nonl")
    git(r.dir, "checkout", "-q", "main"); git(r.dir, "merge", "-q", "--ff-only", "pr"); git(r.dir, "checkout", "-q", "pr")
    r.write(env_path("dev", "cart-service"), values(D(9), tag="sha-2222222").rstrip("\n")); r.commit()


@case("a huge diff in one values file (20 000 lines)", "full", "lines (an image pin is at most")
def _(r): r.write(env_path("dev", "frontend"), values(D(9), extra=FRONTEND_EXTRA + "".join(f"k{i}: {i}\n" for i in range(20000)))); r.commit()


@case("a values file of 5 MB", "full", "bytes")
def _(r):
    r.write(env_path("dev", "frontend"), values(D(1), extra=FRONTEND_EXTRA + "# " + "x" * 5_000_000 + "\n")); r.commit("big")
    git(r.dir, "checkout", "-q", "main"); git(r.dir, "merge", "-q", "--ff-only", "pr"); git(r.dir, "checkout", "-q", "pr")
    r.write(env_path("dev", "frontend"), values(D(9), extra=FRONTEND_EXTRA + "# " + "x" * 5_000_000 + "\n")); r.commit()


@case("300 values files changed", "full", "300 files changed")
def _(r):
    for i in range(300):
        r.write(env_path("dev", f"app{i}"), values(D(i)));
    r.commit()


@case("binary content in the values file", "full", None)
def _(r): r.write(env_path("dev", "frontend"), b"image:\n  tag: \"x\"\n  digest: " + D(9).encode() + b"\n\x00\x01\n"); r.commit()


@case("invalid UTF-8 in a comment line", "full", "not valid UTF-8")
def _(r): r.write(env_path("dev", "frontend"), values(D(9), extra=FRONTEND_EXTRA).replace("chart default", "\x00").encode("latin-1").replace(b"\x00", b"\xff\xfe")); r.commit()


@case("mode change (executable bit) together with an image bump", "full", "file mode")
def _(r):
    r.write(env_path("dev", "frontend"), values(D(9), tag="sha-9", extra=FRONTEND_EXTRA)); os.chmod(os.path.join(r.dir, env_path("dev", "frontend")), 0o755); r.commit()


@case("mode change alone", "full", "file mode")
def _(r): os.chmod(os.path.join(r.dir, env_path("dev", "frontend")), 0o755); r.commit()


@case("the values file becomes a symlink", "full", "status")
def _(r):
    p = os.path.join(r.dir, env_path("dev", "frontend")); os.remove(p); os.symlink(env_path("stage", "frontend").replace("envs/stage/", "../../stage/"), p); r.commit()


@case("symlink to an existing file with an image diff", "full", None)
def _(r):
    p = os.path.join(r.dir, env_path("dev", "cart-service")); os.remove(p); os.symlink("../../prod/values/cart-service.yaml", p); r.commit()


# non-values files, alone
for _name, _rel in [
    ("a chart template", "frontend/templates/deployment.yaml"),
    ("a chart values.yaml", "frontend/values.yaml"),
    ("a Chart.yaml", "frontend/Chart.yaml"),
    ("a policy", "policies/values.yaml"),
    ("this workflow", ".github/workflows/ci.yaml"),
    ("a script (the classifier itself)", ".github/scripts/classify-change.py"),
    ("docs", "docs/promote.md"),
    ("the README", "README.md"),
    ("envs/common.yaml", "envs/common.yaml"),
    ("envs/dev/common.yaml", "envs/dev/common.yaml"),
    ("envs/dev/workloads.yaml", "envs/dev/workloads.yaml"),
]:
    def _mk2(rel):
        def f(r): r.write(rel, r.read(rel) + "# changed\n"); r.commit()
        return f
    case(f"only {_name}", "full", "is not an envs/<env>/values/<app>.yaml file")(_mk2(_rel))


@case("image bump AND a chart touched (the brief's case c)", "full", "2 files changed")
def _(r):
    r.write(env_path("dev", "frontend"), values(D(9), tag="sha-9", extra=FRONTEND_EXTRA))
    r.write("frontend/templates/deployment.yaml", "kind: Deployment\nchanged: true\n"); r.commit()


@case("image bump and the workflow", "full", "2 files changed")
def _(r):
    r.write(env_path("dev", "frontend"), values(D(9), tag="sha-9", extra=FRONTEND_EXTRA)); r.write(".github/workflows/ci.yaml", "name: CI2\n"); r.commit()


@case("a values file in a wrong place: wrong environment", "full", "is not an envs/<env>/values/<app>.yaml file")
def _(r): r.write("envs/test/values/frontend.yaml", values(D(9))); r.commit()


@case("a values file in a subdirectory", "full", "is not an envs/<env>/values/<app>.yaml file")
def _(r): r.write("envs/dev/values/sub/frontend.yaml", values(D(9))); r.commit()


@case(".yml instead of .yaml", "full", "is not an envs/<env>/values/<app>.yaml file")
def _(r): r.write("envs/dev/values/frontend.yml", values(D(9))); r.commit()


@case("an app whose chart does not name one of our images", "full", "not one of our images")
def _(r): r.write(env_path("dev", "third-party"), values(D(9))); r.commit()


@case("an app with no chart directory", "full", "no chart values")
def _(r):
    r.write(env_path("dev", "ghost"), values(D(1))); r.commit("ghost")
    git(r.dir, "checkout", "-q", "main"); git(r.dir, "merge", "-q", "--ff-only", "pr"); git(r.dir, "checkout", "-q", "pr")
    r.write(env_path("dev", "ghost"), values(D(9))); r.commit()


@case("no change at all", "full", "no changed files")
def _(r): r.commit("empty")


@case("a revert-style change that restores the base", "full", "no changed files")
def _(r):
    r.write(env_path("dev", "frontend"), values(D(9), extra=FRONTEND_EXTRA)); r.commit("a")
    r.write(env_path("dev", "frontend"), values(D(1), extra=FRONTEND_EXTRA)); r.commit("b")


# ---- the runner -----------------------------------------------------------------------------------------------------------------------

def run_classifier(repo_dir, **env):
    out = os.path.join(repo_dir, "..", f"out-{os.path.basename(repo_dir)}")
    summary = out + ".md"
    for f in (out, summary):
        open(f, "w").close()
    e = {"PATH": os.environ["PATH"], "GITHUB_OUTPUT": out, "GITHUB_STEP_SUMMARY": summary, "BASE_REF": "main", "HEAD_REF": "pr",
         "EVENT_NAME": "pull_request", **env}
    p = subprocess.run([sys.executable, "-I", SCRIPT], cwd=repo_dir, env=e, capture_output=True, text=True)
    kv = {}
    for line in open(out).read().splitlines():
        k, _, v = line.partition("=")
        kv[k] = v
    return p, kv, open(summary).read()


def check(name, p, kv, summ, lane, reason=None, images=None):
    global count
    count += 1
    problems = []
    if p.returncode != 0:
        problems.append(f"exit code {p.returncode}")
    if kv.get("lane") != lane:
        problems.append(f"lane {kv.get('lane')!r}, expected {lane!r}")
    if reason and reason not in kv.get("reason", ""):
        problems.append(f"reason {kv.get('reason')!r} lacks {reason!r}")
    if images is not None and kv.get("images", "").split() != images:
        problems.append(f"images {kv.get('images')!r}, expected {images}")
    if lane == "full" and (kv.get("images") or kv.get("apps") or kv.get("envs")):
        problems.append("the full lane must not report images, apps or envs")
    if lane == "image-only" and (len(kv.get("files", "").split()) != 1 or not kv.get("apps") or not kv.get("envs")):
        problems.append("the image-only lane must report its one file, app and env")
    if "\n" in kv.get("reason", "") or f"### Change classification: `{lane}`" not in summ:
        problems.append("summary / output format")
    if problems:
        failures.append(f"{name}: {'; '.join(problems)}\n    stdout: {p.stdout.strip()}\n    stderr: {p.stderr.strip()[:500]}")


for i, (name, fn, lane, reason, images) in enumerate(cases):
    r = Repo(f"c{i}")
    fn(r)
    p, kv, summ = run_classifier(r.dir)
    check(name, p, kv, summ, lane, reason, images)
    if os.environ.get("VERBOSE"):
        print(f"{kv.get('lane'):10} {name}\n           {kv.get('reason')}")
    shutil.rmtree(r.dir, ignore_errors=True)

# cases that are not a change of files
r = Repo("misc")
r.write(env_path("dev", "frontend"), values(D(9), tag="sha-9", extra=FRONTEND_EXTRA)); r.commit()
p, kv, s = run_classifier(r.dir, EVENT_NAME="workflow_dispatch")
check("manual run (workflow_dispatch): always full, no git needed", p, kv, s, "full", "there is no pull request")
p, kv, s = run_classifier(r.dir, EVENT_NAME="push")
check("any other event: full", p, kv, s, "full", "event 'push'")
p, kv, s = run_classifier(r.dir, BASE_REF="origin/nope")
check("base ref missing: full, not an error", p, kv, s, "full", "not found")
p, kv, s = run_classifier(r.dir, HEAD_REF="nope")
check("head ref missing: full", p, kv, s, "full", "not found")
p, kv, s = run_classifier(r.dir, BASE_REF="-x")
check("a ref that looks like an option: full", p, kv, s, "full", None)
p, kv, s = run_classifier(r.dir, BASE_REF="pr", HEAD_REF="pr")
check("base equals head: nothing changed, full", p, kv, s, "full", "no changed files")
p, kv, s = run_classifier(r.dir)
check("the same repository, no overrides: image-only", p, kv, s, "image-only", None, FE_IMG(9))
# shallow clone: no merge base => full (the same failure promotion-policy.sh reports)
shallow = os.path.join(tmp, "shallow")
git(tmp, "clone", "-q", "--depth", "1", "--branch", "pr", "file://" + r.dir, shallow)
git(shallow, "fetch", "-q", "--depth", "1", "origin", "main:refs/remotes/origin/main")
p, kv, s = run_classifier(shallow, BASE_REF="origin/main", HEAD_REF="HEAD")
check("shallow checkout (no merge base): full", p, kv, s, "full", "no merge base")
# not a git repository at all: the script must still produce a lane
nogit = os.path.join(tmp, "nogit"); os.makedirs(nogit)
p, kv, s = run_classifier(nogit)
check("not a git repository: full", p, kv, s, "full", None)
# GITHUB_OUTPUT not writable: the job must fail (exit 1), not silently pass
e = {"PATH": os.environ["PATH"], "GITHUB_OUTPUT": "/nonexistent-dir/out", "BASE_REF": "main", "HEAD_REF": "pr"}
p = subprocess.run([sys.executable, "-I", SCRIPT], cwd=r.dir, env=e, capture_output=True, text=True)
count += 1
if p.returncode != 1:
    failures.append(f"unwritable GITHUB_OUTPUT: exit {p.returncode}, expected 1")
# the classifier uses promotion-policy.sh: if that file is unusable the answer is full, never image-only
count += 1
broken = os.path.join(tmp, "broken-scripts"); os.makedirs(broken)
shutil.copy(SCRIPT, broken)
open(os.path.join(broken, "promotion-policy.sh"), "w").write("img_mask() { :; }\nimg_pins() { :; }\nimg_digest() { :; }\nimg_tag() { :; }\nimg_version() { :; }\nimg_get() { :; }\n")
e = {"PATH": os.environ["PATH"], "GITHUB_OUTPUT": os.path.join(tmp, "o2"), "BASE_REF": "main", "HEAD_REF": "pr"}
open(e["GITHUB_OUTPUT"], "w").close()
subprocess.run([sys.executable, "-I", os.path.join(broken, "classify-change.py")], cwd=r.dir, env=e, capture_output=True, text=True)
if "lane=full" not in open(e["GITHUB_OUTPUT"]).read():
    failures.append("a parser that finds no image lines must give full")

# optional: the real history of this repository
if "--history" in sys.argv:
    n = int(sys.argv[sys.argv.index("--history") + 1])
    repo_root = os.path.abspath(os.path.join(HERE, "..", ".."))
    log = subprocess.run(["git", "log", "--format=%H %s", "-n", "400"], cwd=repo_root, capture_output=True, text=True).stdout.splitlines()
    promos = [l for l in log if l.split(" ", 1)[1].startswith(("promote(", "rollback("))][:n]
    others = [l for l in log if not l.split(" ", 1)[1].startswith(("promote(", "rollback("))][:n]
    for want, group in (("image-only", promos), ("full", others)):
        for line in group:
            sha, subject = line.split(" ", 1)
            if subprocess.run(["git", "rev-parse", "--verify", "-q", f"{sha}^2"], cwd=repo_root, capture_output=True).returncode == 0:
                continue  # a merge commit: no single parent diff
            p, kv, s = run_classifier(repo_root, BASE_REF=f"{sha}^", HEAD_REF=sha)
            if kv.get("lane") != want:
                failures.append(f"history {sha[:7]} {subject!r}: lane {kv.get('lane')!r}, expected {want!r} ({kv.get('reason')})")
            count += 1
    print(f"history: {len(promos)} bot commits and {len(others)} other commits classified")

shutil.rmtree(tmp, ignore_errors=True)
if failures:
    print("\n\n".join(failures))
    print(f"\n{len(failures)} of {count} classifier checks failed")
    sys.exit(1)
print(f"classify-change: {count} checks ok")
