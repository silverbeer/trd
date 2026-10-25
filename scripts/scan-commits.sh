#!/bin/sh
# Scan the MESSAGE of every commit in a range with scripts/privacy_scan.py
# (SB-1428, docs/privacy.md). One scanner run per commit, so a finding names
# the commit it is in. Exit 1 if any message blocks.
#
#   scripts/scan-commits.sh <rev-range>        e.g. origin/main..HEAD
#   scripts/scan-commits.sh <sha> --not --remotes   (a branch never pushed)
#
# Used by .githooks/pre-push for the commits being pushed and by the CI
# `privacy` job for a pull request's range and for a push to main. Why: the
# commit-msg hook only sees this machine's commits, and GitHub's squash merge
# used to copy branch messages onto main unscanned.
set -u
root=$(git rev-parse --show-toplevel) || exit 1
status=0
count=0
for sha in $(git rev-list "$@" 2>/dev/null); do
    count=$((count + 1))
    short=$(git rev-parse --short "$sha")
    git log -1 --format=%B "$sha" \
        | python3 "$root/scripts/privacy_scan.py" --text --label "commit $short" || status=1
done
[ "$count" -gt 0 ] || echo "privacy-scan: no commits in range $*" >&2
exit $status
