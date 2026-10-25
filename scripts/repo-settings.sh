#!/bin/sh
# The GitHub settings the privacy guardrails rely on (SB-1428, docs/privacy.md).
# Idempotent; run by a human with `gh` after creating or re-creating the repo.
# Needs the repo public, or a paid plan: branch protection is not available on
# a private repo on the Free plan.
#
#   scripts/repo-settings.sh [owner/repo]
set -eu
repo=${1:-$(gh repo view --json nameWithOwner --jq .nameWithOwner)}
echo "repo: $repo"
# Squash and merge commits carry the PR TITLE only. Branch commit messages are
# scanned on this machine and in CI, but the merge dialog's title and body are
# editable by hand and nothing scans the result — so nothing from them reaches
# main. CI scans the PR title on every PR.
gh api -X PATCH "repos/$repo" \
    -f squash_merge_commit_title=PR_TITLE -f squash_merge_commit_message=BLANK \
    -f merge_commit_title=PR_TITLE -f merge_commit_message=BLANK \
    -F delete_branch_on_merge=true \
    --jq '{squash_merge_commit_title, squash_merge_commit_message, merge_commit_title, merge_commit_message, delete_branch_on_merge}'
# main: a PR is required, both CI jobs must pass, and that holds for admins too.
gh api -X PUT "repos/$repo/branches/main/protection" \
    --input - <<'JSON' --jq '{contexts: .required_status_checks.contexts, enforce_admins: .enforce_admins.enabled}'
{
  "required_status_checks": {"strict": true, "contexts": ["test", "privacy"]},
  "enforce_admins": true,
  "required_pull_request_reviews": {"required_approving_review_count": 0},
  "restrictions": null
}
JSON
echo "secret TRD_PRIVATE_TERMS_B64: $(gh secret list --json name --jq '[.[].name] | index("TRD_PRIVATE_TERMS_B64") != null')"
echo "  (set with: base64 < ~/.config/trd/private-terms.txt | gh secret set TRD_PRIVATE_TERMS_B64 — an owner decision; the terms leave this machine, encrypted)"
