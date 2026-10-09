#!/bin/bash
# Fail if a commit in the range has no Signed-off-by from its own author: the Developer
# Certificate of Origin that CONTRIBUTING.md asks for. Merges are skipped, and so are bots
# (Dependabot), which can't certify anything.
#   scripts/check-signoff.sh <base>..<head>
set -euo pipefail

range="$1"
failed=0
while read -r sha; do
    author=$(git log -1 --format='%an <%ae>' "$sha")
    case "$author" in *"[bot]"*) continue ;; esac
    if ! git log -1 --format='%(trailers:key=Signed-off-by,valueonly)' "$sha" | grep -qxF "$author"; then
        echo "${sha:0:12} isn't signed off by its author, $author: see CONTRIBUTING.md" >&2
        failed=1
    fi
done < <(git rev-list --no-merges "$range")
exit "$failed"
