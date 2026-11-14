#!/usr/bin/env bash
# Tests for verify-signatures.sh with a fake `cosign` (no network, no registry): what is retried, what is never retried, and what the log says.
# Run:  .github/scripts/test-verify-signatures.sh
set -uo pipefail

script="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/verify-signatures.sh"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
pass=0; fail=0
dig() { printf 'sha256:%063d%x' 0 "$1"; }

# The fake cosign: for the image it is asked about, runs $FAKE/<name>.<n> for its n-th call (the last file repeats), and counts calls.
mkdir -p "$tmp/bin" "$tmp/fake"
cat > "$tmp/bin/cosign" <<'FAKE'
#!/usr/bin/env bash
ref=${*: -1}; name=${ref#mladenovskistefan/}; name=${name%%@*}
n=$(( $(cat "$FAKE/$name.calls" 2> /dev/null || echo 0) + 1 )); echo "$n" > "$FAKE/$name.calls"
f="$FAKE/$name.$n"; [ -f "$f" ] || f=$(ls "$FAKE/$name".[0-9]* | grep -v calls | sort -V | tail -n 1)
echo "Flag --new-bundle-format has been deprecated, this will be the only supported format in future versions" >&2
exec bash "$f"
FAKE
chmod +x "$tmp/bin/cosign"

ok_script()      { printf 'echo '"'"'[{"optional":{"Subject":"https://github.com/platform-boutique/platform-demo-workflows/.github/workflows/service-publish.yaml@%s"}}]'"'"'\n' "$(printf 'a%.0s' {1..40})"; }
fail_script()    { printf 'echo "Error: %s" >&2\necho "error during command execution: %s" >&2\nexit 1\n' "$1" "$1"; }
# fixture <name> <script1> [script2 ...]: the n-th argument is what the n-th call does
fixture() { local name=$1 i=1; shift; for s in "$@"; do printf '%s\n' "$s" > "$tmp/fake/$name.$i"; i=$((i + 1)); done; }

NOSIG="no signatures found"
WRONG="no matching signatures: none of the expected identities matched what was in the certificate, got subjects [https://github.com/evil/x/.github/workflows/w.yaml@refs/heads/main] with issuer https://token.actions.githubusercontent.com"
RATE='GET https://index.docker.io/v2/mladenovskistefan/frontend/manifests/sha256-abc.sig: TOOMANYREQUESTS: You have reached your unauthenticated pull rate limit'
UNAVAIL='GET https://registry-1.docker.io/v2/: unexpected status code 503 Service Unavailable'
GONE='GET https://index.docker.io/v2/mladenovskistefan/frontend/manifests/x: MANIFEST_UNKNOWN: manifest unknown'

# case <name> <expect exit 0|1> <expected calls "name=n ..."> <output fragment, or ->  [absent fragment]
# Needs fixtures, and $images (names to put in the rendered dir), set by the caller.
run() {
  local name=$1 want_rc=$2 want_calls=$3 frag=${4:--} absent=${5:--}
  rm -rf "$tmp/rendered" "$tmp/fake"/*.calls; mkdir "$tmp/rendered"
  { for i in $images; do echo "        image: \"mladenovskistefan/$i\""; done; echo "        image: redis:7"; } > "$tmp/rendered/a.yaml"
  : > "$tmp/summary.md"
  out=$(FAKE="$tmp/fake" COSIGN="$tmp/bin/cosign" PATH="$tmp/bin:$PATH" ATTEMPTS=5 BACKOFF=0 SIGNER_REGEXP='^x$' \
        GITHUB_STEP_SUMMARY="$tmp/summary.md" UNSIGNED_ALLOWLIST="${ALLOW:-}" bash "$script" "$tmp/rendered" 2>&1); rc=$?
  out="$out"$'\n'"$(cat "$tmp/summary.md")"
  local problems=()
  [ "$rc" = "$want_rc" ] || problems+=("exit code $rc, expected $want_rc")
  for c in $want_calls; do
    got=$(cat "$tmp/fake/${c%%=*}.calls" 2> /dev/null || echo 0)
    [ "$got" = "${c##*=}" ] || problems+=("${c%%=*} was verified $got time(s), expected ${c##*=}")
  done
  [ "$frag" = - ] || grep -qF -- "$frag" <<< "$out" || problems+=("output lacks: $frag")
  [ "$absent" = - ] || ! grep -qF -- "$absent" <<< "$out" || problems+=("output has: $absent")
  if [ ${#problems[@]} -eq 0 ]; then pass=$((pass + 1)); echo "ok   $name"
  else fail=$((fail + 1)); echo "FAIL $name"; printf '     %s\n' "${problems[@]}"; printf '%s\n' "$out" | sed 's/^/     | /'; fi
}

F="frontend@$(dig 1)"; G="cart-service@$(dig 2)"; D="product-catalog-db@$(dig 3)"

rm -f "$tmp/fake"/*
images=$F; fixture frontend "$(ok_script)"
run "signed image: one call, signer is logged" 0 "frontend=1" "service-publish.yaml@"

rm -f "$tmp/fake"/*; images=$F; fixture frontend "$(fail_script "$NOSIG")"
run "no signature: fails at once (never retried) and says so" 1 "frontend=1" "no signature found"
run "no signature: the reason is not thrown away" 1 "frontend=1" "digest does not exist in the registry"

rm -f "$tmp/fake"/*; images=$F; fixture frontend "$(fail_script "$WRONG")"
run "wrong signer: fails at once (never retried), names who signed" 1 "frontend=1" "got subjects [https://github.com/evil/x"

rm -f "$tmp/fake"/*; images=$F; fixture frontend "$(fail_script "$RATE")" "$(fail_script "$UNAVAIL")" "$(ok_script)"
run "rate limit then 503 then success: three calls, passes" 0 "frontend=3" "attempt 1/5 failed"

rm -f "$tmp/fake"/*; images=$F; fixture frontend "$(fail_script "$RATE")"
run "registry error all five times: five calls, fails, says it is NOT a missing signature" 1 "frontend=5" "this is not a missing signature"

rm -f "$tmp/fake"/*; images=$F; fixture frontend "$(fail_script "$GONE")"
run "MANIFEST_UNKNOWN is a verdict: one call" 1 "frontend=1" "the registry refused the lookup"

rm -f "$tmp/fake"/*; images=$F; fixture frontend 'echo "Error: Get \"https://tuf-repo-cdn.sigstore.dev/15.root.json\": net/http: TLS handshake timeout" >&2; exit 1' "$(ok_script)"
run "TUF / Rekor hiccup is retried" 0 "frontend=2" "attempt 1/5 failed"

rm -f "$tmp/fake"/*; images="$F $D"; fixture frontend "$(ok_script)"; fixture product-catalog-db "$(fail_script "$NOSIG")"
ALLOW=product-catalog-db run "unsigned but allow-listed image: warning, passes" 0 "product-catalog-db=1" "allow-listed"

rm -f "$tmp/fake"/*; images="$F $D"; fixture frontend "$(ok_script)"; fixture product-catalog-db "$(fail_script "$RATE")"
ALLOW=product-catalog-db run "allow-listed image with a REGISTRY error: still fails (no verdict)" 1 "product-catalog-db=5" "no verdict"

rm -f "$tmp/fake"/*; images="$F $D"; fixture frontend "$(ok_script)"; fixture product-catalog-db "$(fail_script "$WRONG")"
ALLOW=product-catalog-db run "allow-listed image signed by a stranger: fails" 1 "product-catalog-db=1" "not by one of our publish workflows"

rm -f "$tmp/fake"/*; images="$F $G"; fixture frontend "$(fail_script "$NOSIG")"; fixture cart-service "$(ok_script)"
run "one failure does not stop the others; failure is in the summary table" 1 "frontend=1 cart-service=1" "| FAILED |"

rm -f "$tmp/fake"/*; images="frontend:1.0.0"
run "not pinned by digest: fails without calling cosign" 1 "frontend=0" "is not pinned by digest"

rm -f "$tmp/fake"/*; images=""
run "no application images at all: fails" 1 "" "no application images found"

# ---- --images: the fast lane verifies exactly the listed digests (no rendered directory) ----------------------------------------------
# run_images <name> <rc> <calls> <fragment|-> <absent|-> <script args...>   (fixtures as above; the arguments go to the script as given)
run_images() {
  local name=$1 want_rc=$2 want_calls=$3 frag=${4:--} absent=${5:--}; shift 5
  rm -rf "$tmp/fake"/*.calls; : > "$tmp/summary.md"
  out=$(FAKE="$tmp/fake" COSIGN="$tmp/bin/cosign" PATH="$tmp/bin:$PATH" ATTEMPTS=5 BACKOFF=0 SIGNER_REGEXP='^x$' \
        GITHUB_STEP_SUMMARY="$tmp/summary.md" UNSIGNED_ALLOWLIST="${ALLOW:-}" bash "$script" "$@" 2>&1); rc=$?
  out="$out"$'\n'"$(cat "$tmp/summary.md")"
  local problems=()
  [ "$rc" = "$want_rc" ] || problems+=("exit code $rc, expected $want_rc")
  for c in $want_calls; do
    got=$(cat "$tmp/fake/${c%%=*}.calls" 2> /dev/null || echo 0)
    [ "$got" = "${c##*=}" ] || problems+=("${c%%=*} was verified $got time(s), expected ${c##*=}")
  done
  [ "$frag" = - ] || grep -qF -- "$frag" <<< "$out" || problems+=("output lacks: $frag")
  [ "$absent" = - ] || ! grep -qF -- "$absent" <<< "$out" || problems+=("output has: $absent")
  if [ ${#problems[@]} -eq 0 ]; then pass=$((pass + 1)); echo "ok   $name"
  else fail=$((fail + 1)); echo "FAIL $name"; printf '     %s\n' "${problems[@]}"; printf '%s\n' "$out" | sed 's/^/     | /'; fi
}
R() { echo "mladenovskistefan/$1"; }   # R frontend@sha256:... -> the full reference

rm -f "$tmp/fake"/*; fixture frontend "$(ok_script)"; fixture cart-service "$(fail_script "$NOSIG")"
run_images "--images: only the listed image is verified (cart-service, which would fail, is never asked)" 0 "frontend=1 cart-service=0" "Fast lane" "FAILED" --images "$(R "$F")"

rm -f "$tmp/fake"/*; fixture frontend "$(ok_script)"; fixture cart-service "$(ok_script)"
run_images "--images: two images, one call each" 0 "frontend=1 cart-service=1" - - --images "$(R "$F") $(R "$G")"
run_images "--images: the same image twice is verified once" 0 "frontend=1" - - --images "$(R "$F")"$'\n'"$(R "$F")"

rm -f "$tmp/fake"/*; fixture frontend "$(fail_script "$NOSIG")"
run_images "--images: unsigned image fails at once (never retried)" 1 "frontend=1" "no signature found" - --images "$(R "$F")"

rm -f "$tmp/fake"/*; fixture frontend "$(fail_script "$WRONG")"
run_images "--images: wrong signer fails" 1 "frontend=1" "not by one of our publish workflows" - --images "$(R "$F")"

rm -f "$tmp/fake"/*; fixture frontend "$(fail_script "$RATE")" "$(fail_script "$UNAVAIL")" "$(ok_script)"
run_images "--images: rate limit then 503 then success: retried like in the full lane" 0 "frontend=3" "attempt 1/5 failed" - --images "$(R "$F")"

rm -f "$tmp/fake"/*; fixture frontend "$(fail_script "$RATE")"
run_images "--images: registry error five times: no verdict, fails" 1 "frontend=5" "this is not a missing signature" - --images "$(R "$F")"

rm -f "$tmp/fake"/*; fixture frontend "$(fail_script "$GONE")"
run_images "--images: MANIFEST_UNKNOWN is a verdict" 1 "frontend=1" "the registry refused the lookup" - --images "$(R "$F")"

rm -f "$tmp/fake"/*; fixture product-catalog-db "$(fail_script "$NOSIG")"
ALLOW=product-catalog-db run_images "--images: allow-listed unsigned image still passes" 0 "product-catalog-db=1" "allow-listed" - --images "$(R "$D")"

rm -f "$tmp/fake"/*; fixture frontend "$(ok_script)"
run_images "--images: a tag without a digest is refused, cosign not called" 1 "frontend=0" "is not mladenovskistefan/<name>@sha256" - --images "mladenovskistefan/frontend:1.0.0"
run_images "--images: another registry is refused, not skipped" 1 "frontend=0" "is not mladenovskistefan/<name>@sha256" - --images "$(R "$F") docker.io/library/redis@sha256:$(printf 'a%.0s' {1..64})"
run_images "--images: a short digest is refused" 1 "frontend=0" "is not mladenovskistefan" - --images "mladenovskistefan/frontend@sha256:abc"
run_images "--images: shell metacharacters in an entry are refused" 1 "frontend=0" "is not mladenovskistefan" - --images 'mladenovskistefan/frontend@sha256:$(id)'
run_images "--images: an empty list fails (nothing verified is not a pass)" 1 "" "list of images to verify is empty" - --images ""
run_images "--images: only blanks fails" 1 "" "list of images to verify is empty" - --images "   "
run_images "--images without its list is a usage error" 2 "" "usage" - --images
run_images "--images and a directory is a usage error" 2 "" "usage" - --images "$(R "$F")" "$tmp"
run_images "no arguments is a usage error" 2 "" "usage" -
run_images "an unknown option is a usage error" 2 "" "usage" - --image "$(R "$F")"

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
