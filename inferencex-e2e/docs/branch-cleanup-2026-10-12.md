# Branch cleanup: October 12, 2026

The audit covered all 1,304 repository branches, not only the first page of the
[active-branches view](https://github.com/SemiAnalysisAI/InferenceX/branches/active).
There are 214 branch tips with committer dates before September 12, 2026, 00:00
America/New_York (`2026-09-12T04:00:00Z`), 30 days before the audit date.
The [full inventory](branch-cleanup-2026-10-12-inventory.md) records exact SHAs,
dates, and open PRs for every candidate.

The selection contains all 214 branches, including 30 with related open PRs.
The cleanup also closes these 30 reviewed PRs, as requested. This ends their
active review without merging their changes.
No protected or default branch is selected. Full histories are backed up before
deletion because some candidates contain commits absent from `main`.

## Before and projected after

| Resource | Before cleanup PR creation | Planned reduction | Projected after |
| --- | ---: | ---: | ---: |
| Branches | 1,304 | 214 | 1,090 |
| Open PRs | 221 | 30 | 191 |

These are projections, not completed deletions. The cleanup PR and its own branch
add one each while open/present. Other concurrent repository activity can change
the totals. Every run reads current counts; `--apply` also prints actual after
counts, including on a partial failure. The checked-in manifest records all
reviewed PR numbers, branch SHAs, and the original count snapshot.

## What approval means

GitHub returned null `pushedDate` values for all 214 tip commits. Commit dates do not
prove when a branch was created, pushed, reset, or repointed. These are candidates
based on tip commit age, not verified last-update timestamps. Confirm this proxy
is acceptable, or check organization audit records before authorizing deletion.

Merging this PR does not delete branches. It adds a fixed-scope, manual command;
there is no schedule, merge hook, or dynamically expanding deletion list.
The command refuses changed SHAs, protected/default branches, and branches with
new, unreviewed open PRs. It rechecks GitHub immediately before deletion.
API failures abort. The selection is frozen in the adjacent JSON manifest and
does not expand as more branches age.
Git leases guard SHA changes through the push itself, and an atomic push prevents
partial deletion if any lease or a server-side rule rejects the operation.
GitHub PR creation and protection metadata cannot be locked by this command;
coordinate execution with maintainers to avoid concurrent changes.
PR closure is not atomic with branch deletion. Deletion may itself close some
PRs; the command then closes any reviewed PR still open and rejects PRs whose
head or base changed. On a closure failure, inspect the printed counts and the
remaining PRs; do not rerun the entire deletion against already-removed refs.

## Review and execute

Run from `inferencex-e2e/` with Python 3.12+, Git, and an authenticated `gh`.
The dry run only reads GitHub:

```bash
python3 -m infx.workflows.cleanup_stale_branches
```

After explicit approval of all 214 candidates, the 30 PR closures, and the
commit-age proxy:

```bash
python3 -m infx.workflows.cleanup_stale_branches \
  --apply --backup /absolute/durable/path/inferencex-branches-2026-10-12.bundle
```

The parent directory must exist and the backup filename must not already exist.
The command fetches all selected full branch histories, creates and
verifies the bundle, then deletes the reviewed refs. Keep the bundle in durable storage; the SHA
list alone is not a durable backup of unmerged commits. A failed fetch or archive
can leave an empty or incomplete file, but deletion does not run unless bundle
verification succeeds. On failure, inspect the file and use a new filename.

## Recovery

Restore only after approval, using an empty expected-value lease so an existing
branch is not overwritten. This example restores one named branch; repeat for
each approved branch from the inventory:

```bash
git clone --bare /absolute/durable/path/inferencex-branches-2026-10-12.bundle restored.git
git -C restored.git push \
  --force-with-lease=refs/heads/codex/mr805-validate-dsv4-source: \
  https://github.com/SemiAnalysisAI/InferenceX.git \
  refs/heads/codex/mr805-validate-dsv4-source:refs/heads/codex/mr805-validate-dsv4-source
```

Restoring a branch does not reopen a closed PR. Review and reopen each affected
PR separately if the cleanup must be reversed.
