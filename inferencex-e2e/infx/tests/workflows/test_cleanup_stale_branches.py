import subprocess

import pytest

from infx.workflows import cleanup_stale_branches as cleanup
from infx.workflows.cleanup_stale_branches import archive_and_delete, run


@pytest.fixture
def repositories(tmp_path):
    remote = str(tmp_path / "remote.git")
    work = str(tmp_path / "work")
    run("git", "init", "--bare", remote)
    run("git", "init", work)
    run("git", "-C", work, "config", "user.name", "Test")
    run("git", "-C", work, "config", "user.email", "test@example.com")
    (tmp_path / "work" / "payload.txt").write_text("recover this content\n")
    run("git", "-C", work, "add", "payload.txt")
    run("git", "-C", work, "commit", "-m", "initial")
    sha = run("git", "-C", work, "rev-parse", "HEAD")
    run(
        "git",
        "-C",
        work,
        "push",
        remote,
        "HEAD:refs/heads/stale/a",
        "HEAD:refs/heads/stale/b",
    )
    return remote, work, {"stale/a": sha, "stale/b": sha}


def test_delete_and_recover_complete_history(repositories, tmp_path):
    remote, _, candidates = repositories
    backup = tmp_path / "recovery.bundle"
    archive_and_delete(remote, candidates, backup, lambda: None)
    assert run("git", "ls-remote", "--heads", remote) == ""
    restored = str(tmp_path / "restored.git")
    run("git", "clone", "--bare", str(backup), restored)
    for name, sha in candidates.items():
        assert run("git", "-C", restored, "rev-parse", name) == sha
        assert run("git", "-C", restored, "show", f"{name}:payload.txt") == "recover this content"


def test_changed_ref_rejects_entire_atomic_deletion(repositories, tmp_path):
    remote, work, candidates = repositories

    def move_branch():
        run("git", "-C", work, "commit", "--allow-empty", "-m", "new work")
        run("git", "-C", work, "push", remote, "HEAD:refs/heads/stale/a")

    with pytest.raises(subprocess.CalledProcessError):
        archive_and_delete(remote, candidates, tmp_path / "recovery.bundle", move_branch)
    refs = run("git", "ls-remote", "--heads", remote)
    assert f"{candidates['stale/b']}\trefs/heads/stale/b" in refs
    new_sha = run("git", "-C", work, "rev-parse", "HEAD")
    assert f"{new_sha}\trefs/heads/stale/a" in refs


def test_preflight_failure_preserves_refs_and_archive(repositories, tmp_path):
    remote, _, candidates = repositories
    before = run("git", "ls-remote", "--heads", remote)
    backup = tmp_path / "recovery.bundle"

    def reject():
        raise RuntimeError("New open pull request")

    with pytest.raises(RuntimeError, match="New open pull request"):
        archive_and_delete(remote, candidates, backup, reject)
    assert run("git", "ls-remote", "--heads", remote) == before
    assert "refs/heads/stale/a" in run("git", "bundle", "list-heads", str(backup))


def test_existing_backup_is_not_overwritten(repositories, tmp_path):
    remote, _, candidates = repositories
    before = run("git", "ls-remote", "--heads", remote)
    backup = tmp_path / "recovery.bundle"
    backup.write_bytes(b"existing archive")
    with pytest.raises(FileExistsError):
        archive_and_delete(remote, candidates, backup, lambda: None)
    assert backup.read_bytes() == b"existing archive"
    assert run("git", "ls-remote", "--heads", remote) == before


def test_changed_ref_before_archive_is_not_deleted(repositories, tmp_path):
    remote, work, candidates = repositories
    run("git", "-C", work, "commit", "--allow-empty", "-m", "new work")
    run("git", "-C", work, "push", remote, "HEAD:refs/heads/stale/a")
    before = run("git", "ls-remote", "--heads", remote)
    with pytest.raises(RuntimeError, match="Branch moved before backup"):
        archive_and_delete(remote, candidates, tmp_path / "recovery.bundle", lambda: None)
    assert run("git", "ls-remote", "--heads", remote) == before


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("default", "default or protected"),
        ("protected", "default or protected"),
        ("moved", "moved since review"),
        ("cutoff", "not older than the cutoff"),
        ("head-pr", "unreviewed open pull request"),
        ("base-pr", "unreviewed open pull request"),
    ],
)
def test_preflight_rejects_unsafe_candidates(monkeypatch, failure, message):
    candidates = [
        {
            "name": "stale/a",
            "sha": "abc123",
            "committed_date": "2026-09-12T04:00:00Z"
            if failure == "cutoff"
            else "2026-07-14T08:05:06Z",
        }
    ]

    def fake_api(endpoint):
        return {"default_branch": "stale/a" if failure == "default" else "main"}

    def fake_pages(endpoint):
        if endpoint.startswith("branches"):
            return [
                {
                    "name": "stale/a",
                    "protected": failure == "protected",
                    "commit": {"sha": "changed" if failure == "moved" else "abc123"},
                }
            ]
        if failure in ("head-pr", "base-pr"):
            return [
                {
                    "number": 123,
                    "base": {"ref": "stale/a" if failure == "base-pr" else "main"},
                    "head": {"ref": "stale/a", "repo": {"full_name": cleanup.REPOSITORY}},
                }
            ]
        return []

    monkeypatch.setattr(cleanup, "api", fake_api)
    monkeypatch.setattr(cleanup, "api_pages", fake_pages)
    with pytest.raises(RuntimeError, match=message):
        cleanup.preflight(candidates, approved_prs=set())


def test_default_cli_is_read_only(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["cleanup_stale_branches"])

    def fake_api(endpoint):
        return {"default_branch": "main"}

    # The fixture supplies API state matching the selected plan; assertions concern
    # CLI behavior, not the plan's checked-in contents.
    plan = cleanup.json.loads(
        cleanup.Path(cleanup.__file__).with_name("branch_cleanup_2026_10_12.json").read_text()
    )

    def fake_pages(endpoint):
        if endpoint.startswith("branches"):
            return [
                {"name": row["name"], "protected": False, "commit": {"sha": row["sha"]}}
                for row in plan["candidates"]
            ]
        return []

    monkeypatch.setattr(cleanup, "api", fake_api)
    monkeypatch.setattr(cleanup, "api_pages", fake_pages)

    def forbidden_delete(*args):
        pytest.fail("Dry run tried to delete branches")

    monkeypatch.setattr(cleanup, "archive_and_delete", forbidden_delete)
    cleanup.main()
    output = capsys.readouterr().out
    assert "Candidate:" in output
    assert "Dry run only:" in output
    assert "No remote refs changed." in output


def test_approved_pr_and_fork_name_collision(monkeypatch, capsys):
    candidates = [{"name": "stale/a", "sha": "abc123", "committed_date": "2026-08-01T00:00:00Z"}]
    monkeypatch.setattr(cleanup, "api", lambda _: {"default_branch": "main"})
    owner = {"full_name": "another-owner/InferenceX"}

    def pages(endpoint):
        if endpoint.startswith("branches"):
            return [{"name": "stale/a", "protected": False, "commit": {"sha": "abc123"}}]
        return [{"number": 123, "head": {"ref": "stale/a", "repo": owner}, "base": {"ref": "main"}}]

    monkeypatch.setattr(cleanup, "api_pages", pages)
    snapshot = cleanup.preflight(candidates, approved_prs=set())
    assert snapshot == {"branches": 1, "open_prs": 1, "related_open_prs": 0}
    owner["full_name"] = cleanup.REPOSITORY
    snapshot = cleanup.preflight(candidates, approved_prs={123})
    assert snapshot == {"branches": 1, "open_prs": 1, "related_open_prs": 1}
    assert capsys.readouterr().out.count("Candidate: stale/a @ abc123") == 2


def test_close_only_reviewed_open_prs(monkeypatch):
    state = {
        1: {"state": "open", "head": {"ref": "stale/a", "sha": "abc"}, "base": {"ref": "main"}},
        2: {"state": "closed"},
    }
    monkeypatch.setattr(cleanup, "api", lambda path: state[int(path.split("/")[-1])])

    def run_close(*args):
        assert args[:3] == ("gh", "pr", "close")
        state[int(args[3])]["state"] = "closed"

    monkeypatch.setattr(cleanup, "run", run_close)
    cleanup.close_related_prs(
        [
            {"number": 1, "head_ref": "stale/a", "head_sha": "abc", "base_ref": "main"},
            {"number": 2},
        ],
        {"stale/a"},
    )
    assert state[1]["state"] == "closed"
    assert state[2]["state"] == "closed"


def test_changed_pr_is_not_closed(monkeypatch):
    state = {"state": "open", "head": {"ref": "active", "sha": "new"}, "base": {"ref": "main"}}
    monkeypatch.setattr(cleanup, "api", lambda _: state)
    with pytest.raises(RuntimeError, match="changed since review"):
        cleanup.close_related_prs(
            [{"number": 1, "head_ref": "stale/a", "head_sha": "abc", "base_ref": "main"}],
            {"stale/a"},
        )
    assert state["state"] == "open"
