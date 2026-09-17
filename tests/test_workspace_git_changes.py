"""Offline parser coverage and real-Git integration for workspace inspection.

The parser fixtures are the exact NUL-separated byte streams Git 2.53 produced
for a real repository, so a change to the porcelain v2 assumptions fails here
instead of in the UI.  The integration tests drive a real repository because the
two untracked-file behaviours they pin (an empty ``git diff``, and the
``--no-index`` fallback against the platform null device) are properties of Git
itself, not of this module.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

import pytest

from opensquilla import git_runtime, workspace_git_changes
from opensquilla.git_runtime import GitRunState
from opensquilla.workspace_git_changes import (
    WorkspaceGitUnavailableError,
    WorkspacePathError,
    WorkspaceStatusParseError,
    is_untracked_path,
    normalize_repo_path,
    parse_numstat,
    parse_porcelain_status,
    read_workspace_changes,
    read_workspace_diff,
)

# Captured from `git status --porcelain=v2 --branch --untracked-files=all -z`
# after staging a modification (a.txt), a deletion (b.txt) and a rename
# ("c c.txt" -> "e e.txt"), plus one untracked file with a space in its name.
STATUS_WITH_RENAME = (
    "# branch.oid ca8238aacefd33f6888648691a11400f7508f1f8\x00"
    "# branch.head main\x00"
    "# branch.upstream origin/main\x00"
    "# branch.ab +0 -0\x00"
    "1 M. N... 100644 100644 100644 5626abf 0a93aab a.txt\x00"
    "1 D. N... 100644 000000 000000 f719efd 0000000 b.txt\x00"
    "2 R. N... 100644 100644 100644 2bdf67a 2bdf67a R100 e e.txt\x00c c.txt\x00"
    "? d d.txt\x00"
)

# Captured from a repository left in an unresolved merge conflict.
STATUS_UNMERGED = (
    "# branch.oid 4eef2e5664e7de9ca011bd955ce2bcd982883ac2\x00"
    "# branch.head main\x00"
    "u UU N... 100644 100644 100644 100644 df967b9 ba2906d 2299c37 f.txt\x00"
)


@pytest.fixture
def git_environment(tmp_path: Path) -> dict[str, str]:
    """Isolate Git configuration so output does not depend on the machine.

    A developer's ``diff.algorithm`` or ``core.quotepath`` must not change these
    assertions, so the system config is disabled and a synthetic global config
    only pins the initial branch name.
    """

    capability = git_runtime.resolve_git_capability(force_refresh=True)
    if not capability.available:
        pytest.skip(f"Git capability is unavailable: {capability.reason}")
    global_config = tmp_path / "isolated-gitconfig"
    global_config.write_text("[init]\n\tdefaultBranch = main\n", encoding="utf-8")
    environment = dict(os.environ)
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = str(global_config)
    return environment


def _git(
    args: tuple[str, ...],
    *,
    cwd: Path,
    environment: Mapping[str, str],
) -> str:
    result = git_runtime.run_git(args, cwd=cwd, timeout=10.0, environment=environment)
    assert result.state is GitRunState.OK, result.stderr_text
    return result.stdout_text


def _init_repository(repository: Path, environment: Mapping[str, str]) -> None:
    repository.mkdir(parents=True, exist_ok=True)
    _git(("-c", "init.templateDir=", "init", "-q"), cwd=repository, environment=environment)
    _git(("config", "user.email", "tests@example.invalid"), cwd=repository, environment=environment)
    _git(("config", "user.name", "OpenSquilla Tests"), cwd=repository, environment=environment)
    _git(("config", "commit.gpgsign", "false"), cwd=repository, environment=environment)


def _commit_all(repository: Path, environment: Mapping[str, str], message: str = "commit") -> None:
    _git(("add", "--all"), cwd=repository, environment=environment)
    # `--allow-empty` keeps the helper usable for tests that only need a clean
    # baseline repository rather than a first commit.
    _git(("commit", "-q", "--allow-empty", "-m", message), cwd=repository, environment=environment)


def test_parse_status_projects_staged_and_unstaged_state() -> None:
    header, entries = parse_porcelain_status(STATUS_WITH_RENAME)

    assert header.branch == "main"
    assert header.detached is False
    assert header.upstream == "origin/main"
    assert (header.ahead, header.behind) == (0, 0)
    assert [
        (entry.path, entry.previous_path, entry.change_type, entry.staged, entry.unstaged)
        for entry in entries
    ] == [
        ("a.txt", None, "modified", True, False),
        ("b.txt", None, "deleted", True, False),
        # The rename record keeps the original path in the following NUL field
        # while the path itself still contains a space.
        ("e e.txt", "c c.txt", "renamed", True, False),
        ("d d.txt", None, "untracked", False, True),
    ]


def test_parse_status_reports_unmerged_entries() -> None:
    _, entries = parse_porcelain_status(STATUS_UNMERGED)

    assert len(entries) == 1
    entry = entries[0]
    assert (entry.path, entry.change_type) == ("f.txt", "unmerged")
    assert (entry.staged, entry.unstaged) == (True, True)


def test_parse_status_reads_detached_head_and_divergence() -> None:
    header, entries = parse_porcelain_status(
        "# branch.oid abc\x00"
        "# branch.head (detached)\x00"
        "# branch.upstream origin/main\x00"
        "# branch.ab +3 -2\x00"
    )

    assert header.detached is True
    assert header.branch is None
    assert (header.ahead, header.behind) == (3, 2)
    assert entries == []


def test_parse_status_skips_ignored_records() -> None:
    _, entries = parse_porcelain_status("! build/\x00? keep.txt\x00")

    assert [entry.path for entry in entries] == ["keep.txt"]


def test_parse_status_rejects_unknown_records() -> None:
    # An unrecognized record must fail loudly instead of silently shortening
    # the review list.
    with pytest.raises(WorkspaceStatusParseError):
        parse_porcelain_status("x unexpected record\x00")


# Captured from `git diff --numstat HEAD -z` after modifying a file, turning
# another into binary, and staging a rename.
NUMSTAT_WITH_RENAME = "2\t1\ta.ts\x00-\t-\tblob.bin\x000\t0\t\x00rename-me.ts\x00renamed.ts\x00"


def test_parse_numstat_reads_counts_and_keeps_binary_unknown() -> None:
    counts = parse_numstat(NUMSTAT_WITH_RENAME)

    assert counts["a.ts"] == (2, 1)
    # `-` means "no countable lines"; it must not become 0.
    assert counts["blob.bin"] == (None, None)
    # A rename record leaves its path field empty and carries both paths in the
    # following NUL fields, so either spelling resolves.
    assert counts["renamed.ts"] == (0, 0)
    assert counts["rename-me.ts"] == (0, 0)


def test_parse_numstat_ignores_stray_tokens() -> None:
    assert parse_numstat("\x00not-a-record\x00\x00") == {}


def test_read_workspace_changes_reports_counts_and_keeps_unknowns_unknown(
    tmp_path: Path,
    git_environment: dict[str, str],
) -> None:
    repository = tmp_path / "project"
    _init_repository(repository, git_environment)
    (repository / "tracked.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repository, git_environment)
    (repository / "tracked.txt").write_text("two\n", encoding="utf-8")
    (repository / "untracked.txt").write_text("new\n", encoding="utf-8")

    changes = read_workspace_changes(str(repository), environment=git_environment)

    counted = {entry.path: (entry.added_lines, entry.removed_lines) for entry in changes.entries}
    assert counted == {
        "tracked.txt": (1, 1),
        "untracked.txt": (None, None),
    }
    # Totals sum only what is known, so one unknown file cannot fake a zero.
    assert (changes.added_lines, changes.removed_lines) == (1, 1)


@pytest.mark.parametrize(
    "value",
    (
        "",
        "   ",
        "/etc/passwd",
        "\\\\server\\share\\file",
        "C:/Windows/system32",
        "..",
        "../../etc/passwd",
        "src/../../etc/passwd",
        "src/..",
        "nul\x00name",
    ),
)
def test_normalize_repo_path_rejects_escaping_values(value: str) -> None:
    with pytest.raises(WorkspacePathError):
        normalize_repo_path(value)


def test_normalize_repo_path_rejects_non_strings_and_long_values() -> None:
    with pytest.raises(WorkspacePathError):
        normalize_repo_path(None)
    with pytest.raises(WorkspacePathError):
        normalize_repo_path("src/" + "a" * 5000)


def test_normalize_repo_path_returns_the_caller_string_unchanged() -> None:
    # Callers round-trip the exact path reported by the status projection, so
    # normalizing separators here would break the follow-up diff request.
    assert normalize_repo_path("src/pkg/a b.txt") == "src/pkg/a b.txt"
    assert normalize_repo_path("src\\pkg\\a.txt") == "src\\pkg\\a.txt"
    assert normalize_repo_path("a/./b.txt") == "a/./b.txt"


def test_read_workspace_changes_projects_a_real_repository(
    tmp_path: Path,
    git_environment: dict[str, str],
) -> None:
    repository = tmp_path / "project"
    _init_repository(repository, git_environment)
    (repository / "tracked.txt").write_text("one\n", encoding="utf-8")
    (repository / "renamed-source.txt").write_text("stable\n", encoding="utf-8")
    _commit_all(repository, git_environment)

    (repository / "tracked.txt").write_text("two\n", encoding="utf-8")
    (repository / "untracked.txt").write_text("new\n", encoding="utf-8")
    _git(
        ("mv", "renamed-source.txt", "renamed-target.txt"),
        cwd=repository,
        environment=git_environment,
    )

    changes = read_workspace_changes(
        str(repository),
        environment=git_environment,
    )

    assert changes.available is True
    assert changes.availability_reason is None
    assert changes.branch == "main"
    assert changes.detached is False
    assert changes.truncated is False
    assert changes.total_count == len(changes.entries) == 3
    projected = {
        entry.path: (entry.change_type, entry.staged, entry.unstaged, entry.previous_path)
        for entry in changes.entries
    }
    assert projected == {
        "tracked.txt": ("modified", False, True, None),
        "renamed-target.txt": ("renamed", True, False, "renamed-source.txt"),
        "untracked.txt": ("untracked", False, True, None),
    }


def test_read_workspace_changes_marks_truncation(
    tmp_path: Path,
    git_environment: dict[str, str],
) -> None:
    repository = tmp_path / "project"
    _init_repository(repository, git_environment)
    _commit_all(repository, git_environment)
    for index in range(4):
        (repository / f"file-{index}.txt").write_text("x\n", encoding="utf-8")

    changes = read_workspace_changes(
        str(repository),
        max_entries=2,
        environment=git_environment,
    )

    assert changes.total_count == 4
    assert len(changes.entries) == 2
    assert changes.truncated is True


def test_read_workspace_changes_reports_a_non_repository(
    tmp_path: Path,
    git_environment: dict[str, str],
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    changes = read_workspace_changes(str(plain), environment=git_environment)

    assert changes.available is False
    assert changes.availability_reason == "not_repository"
    assert changes.entries == ()
    assert changes.total_count == 0


def test_read_workspace_diff_shows_unstaged_and_staged_halves(
    tmp_path: Path,
    git_environment: dict[str, str],
) -> None:
    repository = tmp_path / "project"
    _init_repository(repository, git_environment)
    (repository / "file.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repository, git_environment)

    (repository / "file.txt").write_text("two\n", encoding="utf-8")
    _git(("add", "file.txt"), cwd=repository, environment=git_environment)
    (repository / "file.txt").write_text("three\n", encoding="utf-8")

    staged = read_workspace_diff(
        str(repository),
        "file.txt",
        staged=True,
        environment=git_environment,
    )
    unstaged = read_workspace_diff(
        str(repository),
        "file.txt",
        environment=git_environment,
    )

    assert staged.staged is True
    assert "-one" in staged.text
    assert "+two" in staged.text
    assert "+three" not in staged.text
    assert unstaged.staged is False
    assert "-two" in unstaged.text
    assert "+three" in unstaged.text
    assert (staged.truncated, unstaged.truncated) == (False, False)
    assert (staged.binary, unstaged.binary) == (False, False)


def test_read_workspace_diff_renders_untracked_files_as_new(
    tmp_path: Path,
    git_environment: dict[str, str],
) -> None:
    repository = tmp_path / "project"
    _init_repository(repository, git_environment)
    _commit_all(repository, git_environment)
    (repository / "brand new.txt").write_text("hello untracked\n", encoding="utf-8")

    # `git diff` prints nothing for a path with no index entry, so the untracked
    # branch must be selected explicitly.
    assert is_untracked_path(
        str(repository),
        "brand new.txt",
        environment=git_environment,
    ) is True
    diff = read_workspace_diff(
        str(repository),
        "brand new.txt",
        untracked=True,
        environment=git_environment,
    )

    assert "new file mode" in diff.text
    assert "+hello untracked" in diff.text
    assert diff.staged is False
    assert diff.binary is False


def test_is_untracked_path_is_false_for_a_tracked_file(
    tmp_path: Path,
    git_environment: dict[str, str],
) -> None:
    repository = tmp_path / "project"
    _init_repository(repository, git_environment)
    (repository / "file.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repository, git_environment)

    assert is_untracked_path(
        str(repository),
        "file.txt",
        environment=git_environment,
    ) is False


def test_read_workspace_diff_flags_binary_content(
    tmp_path: Path,
    git_environment: dict[str, str],
) -> None:
    repository = tmp_path / "project"
    _init_repository(repository, git_environment)
    (repository / "blob.bin").write_bytes(b"\x00\x01\x02binary\x00")
    _commit_all(repository, git_environment)
    (repository / "blob.bin").write_bytes(b"\x00\x01\x03changed\x00\xff")

    diff = read_workspace_diff(
        str(repository),
        "blob.bin",
        environment=git_environment,
    )

    assert diff.binary is True


def test_read_workspace_diff_bounds_oversized_output(
    tmp_path: Path,
    git_environment: dict[str, str],
) -> None:
    repository = tmp_path / "project"
    _init_repository(repository, git_environment)
    (repository / "file.txt").write_text("one\n", encoding="utf-8")
    _commit_all(repository, git_environment)
    (repository / "file.txt").write_text("two\n" * 200, encoding="utf-8")

    diff = read_workspace_diff(
        str(repository),
        "file.txt",
        max_bytes=64,
        environment=git_environment,
    )

    assert diff.truncated is True
    assert len(diff.text) == 64


def test_read_workspace_diff_raises_outside_a_repository(
    tmp_path: Path,
    git_environment: dict[str, str],
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(WorkspaceGitUnavailableError) as raised:
        read_workspace_diff(str(plain), "file.txt", environment=git_environment)

    assert raised.value.reason == "not_repository"


def test_read_workspace_diff_raises_for_an_escaping_path(
    tmp_path: Path,
    git_environment: dict[str, str],
) -> None:
    repository = tmp_path / "project"
    _init_repository(repository, git_environment)

    with pytest.raises(WorkspacePathError):
        read_workspace_diff(str(repository), "../../etc/passwd")


def test_read_only_reads_apply_the_shared_git_hardening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every Git invocation must disable repository-controlled helpers.

    A repository can configure ``core.fsmonitor``, ``diff.external`` or a
    textconv driver that would otherwise make these reads execute code the
    repository chose, so the argv is asserted directly instead of inferred from
    output.  The stub keeps this a hermetic unit test.
    """

    captured: list[tuple[str, ...]] = []

    def _capture(args: tuple[str, ...], **_kwargs: object) -> git_runtime.GitRunResult:
        captured.append(tuple(args))
        return git_runtime.GitRunResult(
            state=GitRunState.OK,
            returncode=0,
            stdout=b"",
            stderr=b"",
            capability=git_runtime.GitCapability(
                state=git_runtime.GitCapabilityState.AVAILABLE,
                executable=Path("git"),
                source="test",
            ),
        )

    monkeypatch.setattr(workspace_git_changes, "run_git", _capture)

    read_workspace_changes(".")           # status + numstat
    read_workspace_diff(".", "file.txt")
    is_untracked_path(".", "file.txt")

    for args in captured:
        assert args[:3] == ("--no-optional-locks", "-c", "core.fsmonitor=false"), args

    def find(marker: str) -> tuple[str, ...]:
        return next(args for args in captured if marker in args)

    # Both diff-shaped reads must disable repository-controlled helpers.
    for marker in ("--numstat", "--unified=3"):
        args = find(marker)
        assert args[3:5] == ("diff", "--no-ext-diff"), args
        assert "--no-textconv" in args
    assert "ls-files" in find("ls-files")
    assert "status" in find("status")
