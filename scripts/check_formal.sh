#!/usr/bin/env bash
set -euo pipefail

# Install/download the pinned tools separately; this runner never uses the network.
: "${TLA2TOOLS_JAR:?Set TLA2TOOLS_JAR to the path of tla2tools.jar (v1.7.4)}"
jar_path=$(realpath "$TLA2TOOLS_JAR")
expected=936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88
actual=$(sha256sum "$jar_path")
if [[ "${actual%% *}" != "$expected" ]]; then
    echo "TLA+ tools checksum mismatch; use the documented v1.7.4 artifact." >&2
    exit 1
fi
project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
scratch=$(mktemp -d)
trap 'rm -rf -- "$scratch"' EXIT
cd "$project_root/formal"
java -XX:+UseParallelGC -Xmx1g -cp "$jar_path" tlc2.TLC \
    -workers 1 -metadir "$scratch/valid" -config UniqueTags.cfg UniqueTags.tla

# A successful positive model alone could have a vacuous invariant.
# Require the intentionally wrong visibility rule to fail the expected invariant.
set +e
java -XX:+UseParallelGC -Xmx1g -cp "$jar_path" tlc2.TLC \
    -workers 1 -metadir "$scratch/mutant" -config UniqueTagsMutant.cfg UniqueTags.tla \
    > "$scratch/mutant.log" 2>&1
status=$?
set -e
if [[ "$status" != 12 ]] || ! grep -q 'Invariant CorrectDecisions is violated' "$scratch/mutant.log"; then
    cat "$scratch/mutant.log"
    echo "Negative control did not produce the expected counterexample." >&2
    exit 1
fi
echo "Negative control: configured-model visibility mutant rejected (CorrectDecisions)."
