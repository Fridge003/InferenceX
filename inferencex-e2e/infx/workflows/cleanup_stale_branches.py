"""Review and execute the fixed 2026-10-12 branch cleanup."""

import argparse
import json
import subprocess
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

REPOSITORY = "SemiAnalysisAI/InferenceX"
REMOTE = f"https://github.com/{REPOSITORY}.git"
CUTOFF = datetime.fromisoformat("2026-09-12T00:00:00-04:00")


def run(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def api(endpoint: str) -> dict | list:
    path = f"repos/{REPOSITORY}"
    if endpoint:
        path += f"/{endpoint}"
    return json.loads(run("gh", "api", path))


def api_pages(endpoint: str) -> list[dict]:
    pages = json.loads(run("gh", "api", "--paginate", "--slurp", f"repos/{REPOSITORY}/{endpoint}"))
    return [item for page in pages for item in page]


def preflight(candidates: list[dict], *, approved_prs: set[int]) -> dict[str, int]:
    default_branch = api("")["default_branch"]
    branches = {branch["name"]: branch for branch in api_pages("branches?per_page=100")}
    pulls = api_pages("pulls?state=open&per_page=100")
    names = {row["name"] for row in candidates}
    related_prs = {
        pr["number"]
        for pr in pulls
        if pr["base"]["ref"] in names
        or (
            pr["head"]["ref"] in names
            and (pr["head"]["repo"] or {}).get("full_name", "").lower() == REPOSITORY.lower()
        )
    }
    unreviewed = related_prs - approved_prs
    if unreviewed:
        raise RuntimeError(f"Branches have unreviewed open pull requests: {sorted(unreviewed)}")
    for candidate in candidates:
        name, expected = candidate["name"], candidate["sha"]
        branch = branches.get(name)
        if branch is None:
            raise RuntimeError(f"Branch no longer exists: {name}")
        if name == default_branch or branch["protected"]:
            raise RuntimeError(f"Refusing default or protected branch: {name}")
        if branch["commit"]["sha"] != expected:
            raise RuntimeError(f"Branch moved since review: {name}")
        # A matching immutable commit SHA preserves the audited committer date.
        date = datetime.fromisoformat(candidate["committed_date"].replace("Z", "+00:00"))
        if date >= CUTOFF:
            raise RuntimeError(f"Branch is not older than the cutoff: {name}")
        print(f"Candidate: {name} @ {expected} (tip committed {date.isoformat()})")
    return {"branches": len(branches), "open_prs": len(pulls), "related_open_prs": len(related_prs)}


def archive_and_delete(
    remote: str,
    candidates: Mapping[str, str],
    backup: Path,
    pre_delete_check: Callable[[], None],
) -> None:
    """Archive complete histories before an atomic, expected-SHA-guarded deletion."""
    if not candidates:
        raise ValueError("No branches selected")
    backup = backup.resolve()
    # Reserve without overwriting an earlier archive, including on retries.
    with backup.open("xb"):
        pass
    with TemporaryDirectory(prefix="infx-branch-cleanup-") as temp:
        run("git", "init", "--bare", temp)
        refs = [f"refs/heads/{name}" for name in candidates]
        run("git", "-C", temp, "fetch", "--no-tags", remote, *(f"{ref}:{ref}" for ref in refs))
        for name, expected in candidates.items():
            actual = run("git", "-C", temp, "rev-parse", f"refs/heads/{name}")
            if actual != expected:
                raise RuntimeError(f"Branch moved before backup: {name}")
        run("git", "-C", temp, "bundle", "create", str(backup), *refs)
        run("git", "-C", temp, "bundle", "verify", str(backup))
        print(f"Verified recovery bundle: {backup}")
        pre_delete_check()
        leases = [f"--force-with-lease=refs/heads/{name}:{sha}" for name, sha in candidates.items()]
        run("git", "-C", temp, "push", "--atomic", *leases, remote, *(f":{ref}" for ref in refs))
        print(f"Deleted {len(candidates)} branches. Keep the recovery bundle.")


def close_related_prs(pulls: list[dict], candidates: set[str]) -> None:
    for approved in pulls:
        number = approved["number"]
        current = api(f"pulls/{number}")
        if current["state"] == "closed":
            continue
        # Do not close a PR that was retargeted after the reviewed snapshot.
        if (
            current["head"]["ref"] != approved["head_ref"]
            or current["head"]["sha"] != approved["head_sha"]
            or current["base"]["ref"] != approved["base_ref"]
            or (
                current["head"]["ref"] not in candidates
                and current["base"]["ref"] not in candidates
            )
        ):
            raise RuntimeError(f"PR #{number} changed since review; inspect it before closing")
        run("gh", "pr", "close", str(number), "--repo", REPOSITORY)
        print(f"Closed PR #{number}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Delete the fixed candidates and close related PRs using tip commit age as the proxy.",
    )
    parser.add_argument(
        "--backup", type=Path, help="New recovery bundle path; required with --apply."
    )
    args = parser.parse_args()
    if args.apply and args.backup is None:
        parser.error("--apply requires --backup")
    plan = json.loads(Path(__file__).with_name("branch_cleanup_2026_10_12.json").read_text())
    candidates = plan["candidates"]
    pulls = plan["related_pull_requests"]
    approved_prs = {pr["number"] for pr in pulls}

    def check() -> None:
        preflight(candidates, approved_prs=approved_prs)

    before = preflight(candidates, approved_prs=approved_prs)
    print(f"Before: {before['branches']} branches; {before['open_prs']} open PRs")
    print(
        f"Projected after: {before['branches'] - len(candidates)} branches; "
        f"{before['open_prs'] - before['related_open_prs']} open PRs"
    )
    if not args.apply:
        print(
            f"Dry run only: {len(candidates)} candidates. No remote refs changed. "
            "Push/creation timestamps are unverified."
        )
        return
    try:
        archive_and_delete(
            REMOTE, {row["name"]: row["sha"] for row in candidates}, args.backup, check
        )
        close_related_prs(pulls, {row["name"] for row in candidates})
    finally:
        after_branches = len(api_pages("branches?per_page=100"))
        after_prs = len(api_pages("pulls?state=open&per_page=100"))
        print(f"Actual after: {after_branches} branches; {after_prs} open PRs")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.stderr) from error
