"""Read-only Git working-tree inspection for project workspaces.

The owner-facing surfaces (the Web UI Git panel and any later workbench
consumer) need *structured* working-tree state, while the agent-facing
``git_status`` / ``git_diff`` tools return text for a model to read.  Both
layers must still agree on how Git is executed, so this module never spawns Git
itself: it reuses :func:`opensquilla.git_runtime.run_git`, which owns safe
absolute-path resolution, the non-interactive environment, and timeouts.

Parsing is split from execution on purpose.  ``parse_porcelain_status`` is a
pure function over the porcelain v2 byte stream, so the record shapes that are
easy to get wrong (renames carry the original path in a second NUL field,
unmerged entries carry four modes and three hashes) are covered by offline unit
tests instead of requiring a live Git repository.

For a file that is not tracked yet, ``git diff`` prints nothing, so the review
surface would silently show an empty diff for exactly the files an agent most
often creates.  ``read_workspace_diff`` therefore falls back to
``git diff --no-index`` against the platform null device, which produces the
ordinary "new file" diff Git itself would print for a staged addition.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Literal

from opensquilla.git_runtime import (
    GitRunState,
    harden_read_only_git_args,
    run_git,
)

STATUS_TIMEOUT_SECONDS = 10.0
DIFF_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_ENTRIES = 500
DEFAULT_MAX_DIFF_BYTES = 512 * 1024

_MAX_REPO_PATH_CHARS = 4096

ChangeType = Literal[
    "added",
    "modified",
    "deleted",
    "renamed",
    "copied",
    "typeChanged",
    "unmerged",
    "untracked",
    "unknown",
]
AvailabilityReason = Literal[
    "git_unavailable",
    "not_repository",
    "timed_out",
    "failed",
]

_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:")
_SEPARATOR_RE = re.compile(r"[\\/]")
# `git diff` (and `--no-index`) announce binary content instead of hunks.
_BINARY_DIFF_RE = re.compile(r"^(?:Binary files .* differ|GIT binary patch)$", re.MULTILINE)

# Porcelain v2 status codes, most specific first. A rename that also modified
# content reports both, and the review list should say "renamed".
_CHANGE_PRIORITY: tuple[tuple[str, ChangeType], ...] = (
    ("R", "renamed"),
    ("C", "copied"),
    ("A", "added"),
    ("D", "deleted"),
    ("T", "typeChanged"),
    ("M", "modified"),
)


class WorkspacePathError(ValueError):
    """A caller-supplied path is not a repository-relative path."""


class WorkspaceGitUnavailableError(RuntimeError):
    """Git could not produce the requested read-only result."""

    def __init__(self, reason: AvailabilityReason) -> None:
        self.reason = reason
        super().__init__(reason)


class WorkspaceStatusParseError(RuntimeError):
    """Porcelain v2 output did not match the documented record shapes."""


@dataclass(frozen=True)
class WorkspaceChangeEntry:
    """One changed path, with staged/unstaged state kept separate.

    ``added_lines`` / ``removed_lines`` are ``None`` when the count is genuinely
    unknown (a binary file, or a path Git reports no line stats for). They are
    never reported as ``0`` for those cases, because a confident zero is a
    different claim from "not countable".
    """

    path: str
    previous_path: str | None
    change_type: ChangeType
    staged: bool
    unstaged: bool
    added_lines: int | None = None
    removed_lines: int | None = None


@dataclass(frozen=True)
class WorkspaceChanges:
    """Working-tree state for one project workspace."""

    available: bool
    availability_reason: AvailabilityReason | None
    branch: str | None
    detached: bool
    upstream: str | None
    ahead: int
    behind: int
    total_count: int
    truncated: bool
    added_lines: int
    removed_lines: int
    entries: tuple[WorkspaceChangeEntry, ...]


@dataclass(frozen=True)
class WorkspaceDiff:
    """One file's unified diff, already bounded for transport."""

    path: str
    staged: bool
    text: str
    truncated: bool
    binary: bool


@dataclass(frozen=True)
class _StatusHeader:
    branch: str | None = None
    detached: bool = False
    upstream: str | None = None
    ahead: int = 0
    behind: int = 0


def normalize_repo_path(value: object) -> str:
    """Validate a repository-relative path supplied by a caller.

    The value is returned unchanged rather than re-written: callers round-trip
    the exact strings ``read_workspace_changes`` reported, so normalizing
    separators here could turn a valid path into one that matches no file.
    """

    if not isinstance(value, str):
        raise WorkspacePathError("path must be a string")
    if len(value) > _MAX_REPO_PATH_CHARS:
        raise WorkspacePathError("path is too long")
    if "\x00" in value:
        raise WorkspacePathError("path must not contain NUL")
    candidate = value.strip()
    if not candidate:
        raise WorkspacePathError("path must not be empty")
    if candidate.startswith(("/", "\\")) or _WINDOWS_ABSOLUTE.match(candidate):
        raise WorkspacePathError("path must be relative to the workspace")
    segments = [segment for segment in _SEPARATOR_RE.split(candidate) if segment not in ("", ".")]
    if not segments:
        raise WorkspacePathError("path must not be empty")
    if any(segment == ".." for segment in segments):
        raise WorkspacePathError("path must stay inside the workspace")
    return candidate


def _change_type(xy: str) -> ChangeType:
    for code, change_type in _CHANGE_PRIORITY:
        if code in xy:
            return change_type
    return "unknown"


def _tracked_entry(path: str, previous_path: str | None, xy: str) -> WorkspaceChangeEntry:
    return WorkspaceChangeEntry(
        path=path,
        previous_path=previous_path,
        change_type=_change_type(xy),
        staged=xy[:1] not in ("", "."),
        unstaged=xy[1:2] not in ("", "."),
    )


def _apply_header(header: _StatusHeader, record: str) -> _StatusHeader:
    key, _, value = record.partition(" ")
    if key == "branch.head":
        # Git reports a detached HEAD with the literal placeholder "(detached)".
        if value == "(detached)":
            return _StatusHeader(
                branch=header.branch,
                detached=True,
                upstream=header.upstream,
                ahead=header.ahead,
                behind=header.behind,
            )
        return _StatusHeader(
            branch=value,
            detached=header.detached,
            upstream=header.upstream,
            ahead=header.ahead,
            behind=header.behind,
        )
    if key == "branch.upstream":
        return _StatusHeader(
            branch=header.branch,
            detached=header.detached,
            upstream=value,
            ahead=header.ahead,
            behind=header.behind,
        )
    if key == "branch.ab":
        ahead, behind = header.ahead, header.behind
        for token in value.split():
            match = re.fullmatch(r"([+-])(\d+)", token)
            if match is None:
                continue
            count = int(match.group(2))
            if match.group(1) == "+":
                ahead = count
            else:
                behind = count
        return _StatusHeader(
            branch=header.branch,
            detached=header.detached,
            upstream=header.upstream,
            ahead=ahead,
            behind=behind,
        )
    # branch.oid and any future header field do not change the projection.
    return header


def parse_porcelain_status(output: str) -> tuple[_StatusHeader, list[WorkspaceChangeEntry]]:
    """Parse ``git status --porcelain=v2 --branch -z`` output.

    Every record is NUL-terminated, and a rename/copy record is followed by one
    extra NUL field holding the original path, so the scan is index-based rather
    than a plain comprehension.
    """

    header = _StatusHeader()
    entries: list[WorkspaceChangeEntry] = []
    tokens = output.split("\x00")
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        if token.startswith("# "):
            header = _apply_header(header, token[2:])
            continue
        record = token[0]
        if record == "?":
            entries.append(
                WorkspaceChangeEntry(
                    path=token[2:],
                    previous_path=None,
                    change_type="untracked",
                    staged=False,
                    unstaged=True,
                )
            )
            continue
        if record == "!":
            # Ignored files are not part of a change review.
            continue
        fields = token.split(" ")
        if record == "1" and len(fields) >= 9:
            entries.append(_tracked_entry(" ".join(fields[8:]), None, fields[1]))
            continue
        if record == "2" and len(fields) >= 10:
            previous = tokens[index] if index < len(tokens) else ""
            index += 1
            entries.append(
                _tracked_entry(" ".join(fields[9:]), previous or None, fields[1])
            )
            continue
        if record == "u" and len(fields) >= 11:
            entries.append(
                WorkspaceChangeEntry(
                    path=" ".join(fields[10:]),
                    previous_path=None,
                    change_type="unmerged",
                    staged=True,
                    unstaged=True,
                )
            )
            continue
        raise WorkspaceStatusParseError(
            f"unrecognized porcelain v2 record: {token[:64]!r}"
        )
    return header, entries


def _numstat_value(raw: str) -> int | None:
    """`git diff --numstat` prints `-` when a file has no countable lines."""

    return int(raw) if raw.isdigit() else None


def parse_numstat(output: str) -> dict[str, tuple[int | None, int | None]]:
    """Parse ``git diff --numstat HEAD -z`` into ``path -> (added, removed)``.

    With ``-z`` a rename/copy record leaves its path field empty and carries the
    original and new path in the following two NUL fields, so the scan is
    index-based. Both paths of a rename are registered, because callers looking
    up counts may hold either spelling.
    """

    counts: dict[str, tuple[int | None, int | None]] = {}
    tokens = output.split("\x00")
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        added_raw, separator, remainder = token.partition("\t")
        if not separator:
            continue
        removed_raw, separator, path = remainder.partition("\t")
        if not separator:
            # Not a numstat record (a stray token); skip rather than invent one.
            continue
        value = (_numstat_value(added_raw), _numstat_value(removed_raw))
        if path:
            counts[path] = value
            continue
        previous = tokens[index] if index < len(tokens) else ""
        current = tokens[index + 1] if index + 1 < len(tokens) else ""
        index += 2
        for candidate in (current, previous):
            if candidate:
                counts[candidate] = value
    return counts


def _apply_line_counts(
    entries: list[WorkspaceChangeEntry],
    counts: dict[str, tuple[int | None, int | None]],
) -> list[WorkspaceChangeEntry]:
    return [
        replace(
            entry,
            added_lines=counts[entry.path][0] if entry.path in counts else None,
            removed_lines=counts[entry.path][1] if entry.path in counts else None,
        )
        for entry in entries
    ]


def _total(values: tuple[int | None, ...]) -> int:
    return sum(value for value in values if value is not None)


def _availability_reason(result_state: GitRunState) -> AvailabilityReason | None:
    if result_state is GitRunState.UNAVAILABLE:
        return "git_unavailable"
    if result_state is GitRunState.NOT_REPOSITORY:
        return "not_repository"
    if result_state is GitRunState.TIMED_OUT:
        return "timed_out"
    if result_state is GitRunState.FAILED:
        return "failed"
    return None


def read_workspace_changes(
    workspace_path: str,
    *,
    timeout: float = STATUS_TIMEOUT_SECONDS,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    environment: Mapping[str, str] | None = None,
) -> WorkspaceChanges:
    """Return the working-tree state of *workspace_path*.

    A missing Git, a timeout, or a directory that is not a repository is
    reported as ``available=False`` with a reason instead of raising: the panel
    needs to distinguish "nothing changed" from "cannot tell".
    """

    result = run_git(
        harden_read_only_git_args(
            (
                "status",
                "--porcelain=v2",
                "--branch",
                "--untracked-files=all",
                "-z",
            )
        ),
        cwd=workspace_path,
        timeout=timeout,
        environment=environment,
    )
    reason = _availability_reason(result.state)
    if reason is not None:
        return WorkspaceChanges(
            available=False,
            availability_reason=reason,
            branch=None,
            detached=False,
            upstream=None,
            ahead=0,
            behind=0,
            total_count=0,
            truncated=False,
            added_lines=0,
            removed_lines=0,
            entries=(),
        )
    header, entries = parse_porcelain_status(result.stdout_text)
    entries = _apply_line_counts(entries, _read_line_counts(workspace_path, timeout, environment))
    kept = entries[: max(0, max_entries)]
    return WorkspaceChanges(
        available=True,
        availability_reason=None,
        branch=header.branch,
        detached=header.detached,
        upstream=header.upstream,
        ahead=header.ahead,
        behind=header.behind,
        total_count=len(entries),
        truncated=len(entries) > len(kept),
        added_lines=_total(tuple(entry.added_lines for entry in entries)),
        removed_lines=_total(tuple(entry.removed_lines for entry in entries)),
        entries=tuple(kept),
    )


def _read_line_counts(
    workspace_path: str,
    timeout: float,
    environment: Mapping[str, str] | None,
) -> dict[str, tuple[int | None, int | None]]:
    """Per-file line counts against HEAD, or an empty map when unavailable.

    A missing count is reported as unknown rather than failing the whole read:
    the change list itself is still correct and useful without stats.
    """

    result = run_git(
        harden_read_only_git_args(("diff", "--numstat", "HEAD", "-z")),
        cwd=workspace_path,
        timeout=timeout,
        environment=environment,
    )
    if result.state is not GitRunState.OK:
        return {}
    return parse_numstat(result.stdout_text)


def _null_device() -> str:
    return "NUL" if os.name == "nt" else "/dev/null"


def is_untracked_path(
    workspace_path: str,
    path: str,
    *,
    timeout: float = DIFF_TIMEOUT_SECONDS,
    environment: Mapping[str, str] | None = None,
) -> bool:
    """Return whether *path* has no index entry yet.

    ``git diff`` says nothing about such a file, so the caller must pick the
    ``--no-index`` comparison instead. ``ls-files --error-unmatch`` reports an
    unmatched path as exit status 1, which is the untracked answer rather than a
    Git failure.
    """

    repo_path = normalize_repo_path(path)
    result = run_git(
        harden_read_only_git_args(
            ("ls-files", "--error-unmatch", "--", repo_path)
        ),
        cwd=workspace_path,
        timeout=timeout,
        environment=environment,
    )
    if result.state is GitRunState.OK:
        return False
    if result.returncode == 1:
        return True
    raise WorkspaceGitUnavailableError(_availability_reason(result.state) or "failed")


def read_workspace_diff(
    workspace_path: str,
    path: str,
    *,
    staged: bool = False,
    untracked: bool = False,
    timeout: float = DIFF_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_DIFF_BYTES,
    environment: Mapping[str, str] | None = None,
) -> WorkspaceDiff:
    """Return one file's unified diff.

    ``untracked`` selects the ``--no-index`` comparison against the null device,
    because a not-yet-tracked file has no index or HEAD entry to diff against.
    """

    repo_path = normalize_repo_path(path)
    if untracked:
        # `--no-ext-diff` / `--no-textconv` come from the shared read-only
        # hardening, so only the comparison mode is selected here.
        args: tuple[str, ...] = (
            "diff",
            "--no-color",
            "--no-index",
            "--",
            _null_device(),
            repo_path,
        )
    else:
        args = (
            "diff",
            "--no-color",
            "--unified=3",
            *(("--cached",) if staged else ()),
            "--",
            repo_path,
        )
    result = run_git(
        harden_read_only_git_args(args),
        cwd=workspace_path,
        timeout=timeout,
        environment=environment,
    )
    # `--no-index` reports "differences found" as exit status 1, which is the
    # success path for an untracked file rather than a Git failure.
    succeeded = result.state is GitRunState.OK or (
        untracked and result.returncode == 1
    )
    if not succeeded:
        raise WorkspaceGitUnavailableError(
            _availability_reason(result.state) or "failed"
        )
    text = result.stdout_text
    binary = _BINARY_DIFF_RE.search(text) is not None
    truncated = len(text) > max_bytes
    return WorkspaceDiff(
        path=repo_path,
        staged=staged and not untracked,
        text=text[:max_bytes] if truncated else text,
        truncated=truncated,
        binary=binary,
    )


__all__ = [
    "AvailabilityReason",
    "ChangeType",
    "DEFAULT_MAX_DIFF_BYTES",
    "DEFAULT_MAX_ENTRIES",
    "DIFF_TIMEOUT_SECONDS",
    "STATUS_TIMEOUT_SECONDS",
    "WorkspaceChangeEntry",
    "WorkspaceChanges",
    "WorkspaceDiff",
    "WorkspaceGitUnavailableError",
    "WorkspacePathError",
    "WorkspaceStatusParseError",
    "is_untracked_path",
    "normalize_repo_path",
    "parse_numstat",
    "parse_porcelain_status",
    "read_workspace_changes",
    "read_workspace_diff",
]
