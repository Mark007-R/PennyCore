#!/usr/bin/env bash
# Regenerate .githooks/evaluate-checksums.txt from the current state of
# the takehome evaluate.py files.
#
# Use this ONLY when the external assessment repo legitimately publishes
# a new evaluate.py. The output should be committed in a SEPARATE commit
# from any other change so the audit trail makes the refresh visible.
#
#   bash scripts/refresh_evaluate_checksums.sh
#   git add .githooks/evaluate-checksums.txt
#   git commit -m "experiment: refresh takehome evaluate.py checksum baseline"

set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "${repo_root}"

target=".githooks/evaluate-checksums.txt"

# Preserve the leading comment block (lines starting with `#` plus blank lines
# until the first non-comment, non-blank line); regenerate the data lines.
header="$(awk '/^[^#[:space:]]/{exit} {print}' "${target}")"
{
    printf '%s\n' "${header%$'\n'}"
    sha256sum takehome/context-engine/evaluate.py takehome/orchestrator/evaluate.py
} > "${target}.tmp"

mv "${target}.tmp" "${target}"

echo "✓ refreshed ${target}:"
grep -v '^#' "${target}" | grep -v '^[[:space:]]*$' | sed 's/^/    /'
echo ""
echo "Next: git add ${target} && git commit -m \"experiment: refresh takehome evaluate.py checksum baseline\""
