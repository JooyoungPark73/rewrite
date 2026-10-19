"""git for the paper repo (PLAN.md "Revisions and git"; phase-5 brief "Decisions fixed").

Everything shells out to the host `git`; nothing reimplements it. Every call:
- runs with cwd = the paper repo root (which may be a subdirectory of a larger work tree:
  paths are then taken relative to the paper root, see `GitRepo.prefix`);
- has `GIT_TERMINAL_PROMPT=0` (and editors set to `true`), so git never waits for input;
- passes paths as literal pathspecs (`GIT_LITERAL_PATHSPECS=1`) after `--`;
- pins the few user settings that change output we parse or show (quoted paths, colour,
  diff prefixes, signatures in `log`);
- has a 20 s timeout, except push and pull (120 s, streamed line by line).

Commits name their paths explicitly (`git add -- <paths>`, or `git rm --cached` for paths
no longer in the working tree, then `git commit --only -- <paths>`), never `git add -A`;
the default is every changed or untracked path that `git status` lists, which already
leaves out gitignored files (state.db, build/).

Push and pull run in the background (`GitManager`), one git operation at a time, with their
output forwarded as `git.output` events and a final `git.finished`. After a pull the paths
that changed between the old and the new HEAD are announced as `file.changed` (re-anchoring
findings in .tex files) and, for the spec file, as `spec.changed`. A pull never prunes local
tags (`fetch.pruneTags=false`): snapshot tags that were not pushed yet must survive a pull
even when the user's config prunes tags.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import os
import re
import shutil
import signal
import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel

from app import specwatch
from app.events import EventBus, FileChanged, GitFinished, GitOutput, SpecChanged
from app.files import MAX_TEXT_BYTES, PathEscapeError, normalize_rel, resolve_in_repo
from app.repo import PAPER_JSON_NAME, Repo

log = logging.getLogger(__name__)

GIT_TIMEOUT_S = 20.0
REMOTE_TIMEOUT_S = 120.0
KILL_GRACE_S = 3.0
MAX_DIFF_BYTES = 4 * 1024 * 1024
OUTPUT_TAIL = 200  # lines of push/pull output kept for the error message

# Settings that change output we parse or show; everything else is the user's config.
GIT_CONFIG: tuple[str, ...] = (
    "-c",
    "core.quotepath=off",
    "-c",
    "color.ui=never",
    "-c",
    "diff.mnemonicPrefix=false",
    "-c",
    "diff.noprefix=false",
    "-c",
    "log.showSignature=false",
    "-c",
    "i18n.logOutputEncoding=UTF-8",
)
FIELD = "\x1f"
RECORD = "\x1e"

RemoteOp = Literal["push", "pull"]


class GitError(Exception):
    """A git request that failed; `status` is the HTTP status the API returns."""

    def __init__(self, detail: str, status: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status


class NotARepoError(GitError):
    def __init__(self, root: Path) -> None:
        super().__init__(f"{root} is not inside a git work tree", 400)


# --------------------------------------------------------------------------- API models


class GitFile(BaseModel):
    path: str  # relative to the paper repo root
    status: str  # "M", "A", "D", "R", "C", "T", "U" (conflict) or "??" (untracked)
    staged: bool  # something about this path is in the index
    xy: str = ""  # + git's two-letter code (porcelain v2), e.g. "MM", ".M", "A.", "??"
    orig_path: str | None = None  # + renames and copies: the old path


class GitStatus(BaseModel):
    branch: str | None  # null when HEAD is detached
    upstream: str | None
    ahead: int
    behind: int
    head_sha: str | None  # null before the first commit
    head_subject: str | None
    files: list[GitFile]
    clean: bool
    is_repo: bool
    remotes: list[str] = []  # + configured remote names


class Commit(BaseModel):
    sha: str
    subject: str
    author: str
    date: float  # author date, seconds since the epoch
    tags: list[str]


class TagInfo(BaseModel):
    name: str  # without refs/tags/
    sha: str  # the commit the tag points at (peeled)
    date: float  # tagger date (annotated) or commit date (lightweight)
    message: str


class Diff(BaseModel):
    diff: str
    truncated: bool = False  # + the diff was cut at MAX_DIFF_BYTES


# --------------------------------------------------------------------------- running git


def git_available() -> bool:
    return shutil.which("git") is not None


def git_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        GIT_TERMINAL_PROMPT="0",
        GIT_LITERAL_PATHSPECS="1",
        GIT_EDITOR="true",
        GIT_SEQUENCE_EDITOR="true",
        GIT_MERGE_AUTOEDIT="no",
        GIT_PAGER="cat",
        PAGER="cat",
    )
    return env


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def error_line(output: str, fallback: str) -> str:
    """The most telling line of git's output: the first `fatal:`/`error:` line (with a
    `! [rejected]` line in front of it when there is one), else the last non-hint line."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    rejected = next((line for line in lines if line.startswith("! [")), None)
    fatal = next((line for line in lines if re.match(r"^(fatal|error):", line)), None)
    if fatal is not None:
        return f"{rejected} / {fatal}" if rejected else fatal
    others = [line for line in lines if not line.startswith("hint:")]
    return rejected or (others[-1] if others else fallback)


def run_git(
    root: Path,
    *args: str,
    timeout: float = GIT_TIMEOUT_S,
    stdin_text: str | None = None,
    ok_codes: Sequence[int] = (0,),
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    """`git <args>` in `root`; GitError on a failure (unless `check` is false)."""
    if not git_available():
        raise GitError("git is not installed (not found on PATH)", 503)
    argv = ["git", *GIT_CONFIG, *args]
    try:
        proc = subprocess.run(
            argv,
            cwd=root,
            input=stdin_text.encode("utf-8") if stdin_text is not None else None,
            stdin=subprocess.DEVNULL if stdin_text is None else None,
            capture_output=True,
            timeout=timeout,
            env=git_env(),
            start_new_session=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {args[0]} timed out after {timeout:.0f} s", 504) from exc
    except OSError as exc:
        raise GitError(f"could not run git: {exc}", 500) from exc
    if check and proc.returncode not in ok_codes:
        output = _decode(proc.stderr) + "\n" + _decode(proc.stdout)
        raise GitError(error_line(output, f"git {args[0]} exited with {proc.returncode}"))
    return proc


def git_text(root: Path, *args: str, **kwargs: object) -> str:
    return _decode(run_git(root, *args, **kwargs).stdout)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- the work tree


@dataclass(frozen=True)
class GitRepo:
    root: Path  # the paper repo root: cwd of every command
    top: Path  # the work tree's top level
    prefix: str  # the paper root relative to `top`, with a trailing "/" ("" at the top)

    def from_top(self, path: str) -> str | None:
        """A top-relative path (what porcelain status prints) relative to the paper root;
        None when it lies outside the paper repo."""
        if not self.prefix:
            return path
        return path[len(self.prefix) :] if path.startswith(self.prefix) else None


def locate(root: Path) -> GitRepo | None:
    """The git work tree the paper repo is in, or None (no git, or not a work tree)."""
    if not git_available() or not root.is_dir():
        return None
    try:
        proc = run_git(root, "rev-parse", "--show-toplevel", "--show-prefix", check=False)
    except GitError:
        return None
    if proc.returncode != 0:
        return None
    lines = _decode(proc.stdout).splitlines()
    if not lines or not lines[0].strip():
        return None
    prefix = lines[1].strip() if len(lines) > 1 else ""
    return GitRepo(root=root, top=Path(lines[0].strip()), prefix=prefix)


def require_repo(root: Path) -> GitRepo:
    found = locate(root)
    if found is None:
        if not git_available():
            raise GitError("git is not installed (not found on PATH)", 503)
        raise NotARepoError(root)
    return found


def head_sha(root: Path) -> str | None:
    proc = run_git(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}", check=False)
    sha = _decode(proc.stdout).strip()
    return sha if proc.returncode == 0 and sha else None


def empty_tree(root: Path) -> str:
    """The id of the empty tree in this repo's hash function (sha1 or sha256)."""
    return git_text(root, "hash-object", "-t", "tree", "/dev/null").strip()


_REF_BAD = re.compile(r"[\x00-\x20\x7f:]")


def check_ref(ref: str) -> str:
    """A revision the user may name (sha, branch, tag, HEAD~2, ...): no option-like
    leading dash, no whitespace or control characters, no `:` (which would select a path)."""
    ref = ref.strip()
    if not ref:
        raise GitError("the ref is empty")
    if ref.startswith("-") or _REF_BAD.search(ref) or len(ref) > 256:
        raise GitError(f"not a valid ref: {ref!r}")
    return ref


def resolve_commit(root: Path, ref: str) -> str:
    """The full sha of the commit `ref` names (GitError 400 when it names none)."""
    ref = check_ref(ref)
    target = f"{ref}^{{commit}}"
    proc = run_git(
        root, "rev-parse", "--verify", "--quiet", "--end-of-options", target, check=False
    )
    sha = _decode(proc.stdout).strip()
    if proc.returncode != 0 or not sha:
        raise GitError(f"unknown ref {ref!r}: no such commit, branch or tag")
    return sha


def check_path(root: Path, rel: str) -> str:
    """A repo-relative path for git: inside the repo, not under .git/ (GitError 400)."""
    try:
        resolve_in_repo(root, rel)
    except PathEscapeError as exc:
        raise GitError(str(exc)) from exc
    path = normalize_rel(rel.strip())
    if path in ("", ".") or PurePosixPath(path).parts[0] == ".git":
        raise GitError(f"not a path git can use here: {rel!r}")
    return path


# --------------------------------------------------------------------------- status


def _xy_status(xy: str) -> str:
    index, worktree = xy[0], xy[1]
    return index if index != "." else worktree


def parse_status(output: str, repo: GitRepo) -> GitStatus:
    """Parse `git status --porcelain=v2 --branch -z` output."""
    branch: str | None = None
    upstream: str | None = None
    ahead = behind = 0
    head: str | None = None
    files: list[GitFile] = []
    tokens = output.split("\0")
    i = 0
    while i < len(tokens):
        entry = tokens[i]
        i += 1
        if not entry:
            continue
        if entry.startswith("# "):
            key, _, value = entry[2:].partition(" ")
            if key == "branch.oid":
                head = None if value == "(initial)" else value
            elif key == "branch.head":
                branch = None if value == "(detached)" else value
            elif key == "branch.upstream":
                upstream = value
            elif key == "branch.ab":
                match = re.match(r"\+(\d+) -(\d+)", value)
                if match:
                    ahead, behind = int(match.group(1)), int(match.group(2))
            continue
        kind = entry[0]
        orig: str | None = None
        if kind == "1":
            parts = entry.split(" ", 8)
            xy, raw = parts[1], parts[8]
            status = _xy_status(xy)
        elif kind == "2":
            parts = entry.split(" ", 9)
            xy, raw = parts[1], parts[9]
            orig = tokens[i] if i < len(tokens) else None
            i += 1
            status = _xy_status(xy)
        elif kind == "u":
            parts = entry.split(" ", 10)
            xy, raw = parts[1], parts[10]
            status = "U"
        elif kind == "?":
            xy, raw, status = "??", entry[2:], "??"
        else:  # "!" ignored files (not requested) or something newer than we know
            continue
        path = repo.from_top(raw)
        if path is None:
            continue
        files.append(
            GitFile(
                path=path,
                status=status,
                staged=kind in ("1", "2") and xy[0] != ".",
                xy=xy,
                orig_path=repo.from_top(orig) if orig else None,
            )
        )
    return GitStatus(
        branch=branch,
        upstream=upstream,
        ahead=ahead,
        behind=behind,
        head_sha=head,
        head_subject=None,
        files=files,
        clean=not files,
        is_repo=True,
    )


def not_a_repo_status() -> GitStatus:
    return GitStatus(
        branch=None,
        upstream=None,
        ahead=0,
        behind=0,
        head_sha=None,
        head_subject=None,
        files=[],
        clean=True,
        is_repo=False,
    )


def status(root: Path) -> GitStatus:
    """`git status` of the paper repo (limited to the paper root when it is a subdirectory
    of a larger work tree). `is_repo: false` with empty fields outside git."""
    repo = locate(root)
    if repo is None:
        return not_a_repo_status()
    output = git_text(
        root, "status", "--porcelain=v2", "--branch", "-z", "--untracked-files=all", "--", "."
    )
    result = parse_status(output, repo)
    if result.head_sha is not None:
        result.head_subject = git_text(root, "log", "-1", "--format=%s", result.head_sha).strip()
    result.remotes = git_text(root, "remote").split()
    return result


def untracked(root: Path, rel: str | None = None) -> list[str]:
    """Untracked, not ignored paths (relative to the paper root)."""
    out = git_text(
        root, "ls-files", "--others", "--exclude-standard", "-z", "--", rel if rel else "."
    )
    return [path for path in out.split("\0") if path]


# --------------------------------------------------------------------------- diff, log, show


def diff(root: Path, rel: str | None = None, staged: bool = False) -> Diff:
    """Unified diff of one path or the whole paper repo.

    staged=false: the working tree against HEAD (what committing the path records), plus a
    full-add diff for untracked files; staged=true: the index against HEAD. Before the
    first commit, HEAD is the empty tree."""
    require_repo(root)
    path = check_path(root, rel) if rel else None
    base = ["diff", "--no-color", "--no-ext-diff", "--relative"]
    if staged:
        args = [*base, "--cached"]
    else:
        args = [*base, head_sha(root) or empty_tree(root)]
    chunks = [_decode(run_git(root, *args, "--", path or ".").stdout)]
    if not staged:
        for new in untracked(root, path):
            proc = run_git(
                root,
                "diff",
                "--no-index",
                "--no-color",
                "--no-ext-diff",
                "--",
                "/dev/null",
                new,
                ok_codes=(0, 1),
            )
            chunks.append(_decode(proc.stdout))
    text = "".join(chunks)
    data = text.encode("utf-8")
    if len(data) > MAX_DIFF_BYTES:
        cut = data[:MAX_DIFF_BYTES].decode("utf-8", errors="ignore")
        return Diff(diff=cut.rsplit("\n", 1)[0] + "\n", truncated=True)
    return Diff(diff=text)


def log_commits(root: Path, limit: int = 50) -> list[Commit]:
    """The newest `limit` commits reachable from HEAD, with the tags that point at them
    (empty outside git or before the first commit)."""
    repo = locate(root)
    if repo is None or head_sha(root) is None:
        return []
    fmt = FIELD.join(("%H", "%s", "%an", "%at", "%D")) + RECORD
    args = ["log", f"--max-count={max(1, limit)}", f"--format={fmt}", "HEAD"]
    if repo.prefix:
        args += ["--", "."]
    commits = []
    for record in git_text(root, *args).split(RECORD):
        record = record.strip("\n")
        if not record:
            continue
        sha, subject, author, date, refs = (record.split(FIELD) + [""] * 5)[:5]
        tags = [ref[5:] for ref in refs.split(", ") if ref.startswith("tag: ")]
        commits.append(
            Commit(sha=sha, subject=subject, author=author, date=float(date or 0), tags=tags)
        )
    return commits


def show_bytes(root: Path, ref: str, rel: str) -> bytes | None:
    """The file `rel` as it is in commit `ref`, or None when it is not a file there.
    GitError for a bad ref or path."""
    path = check_path(root, rel)
    sha = resolve_commit(root, ref)
    spec = f"{sha}:./{path}"
    kind = run_git(root, "cat-file", "-t", spec, check=False)
    if kind.returncode != 0 or _decode(kind.stdout).strip() != "blob":
        return None
    size = int(git_text(root, "cat-file", "-s", spec).strip() or 0)
    if size > MAX_TEXT_BYTES:
        raise GitError(f"{path} at {ref} is larger than {MAX_TEXT_BYTES} bytes", 413)
    return run_git(root, "cat-file", "blob", spec).stdout


def show(root: Path, ref: str, rel: str) -> str:
    """The text of `rel` at `ref`: GitError 404 when the path is absent there, 415 when it
    is not UTF-8."""
    require_repo(root)
    data = show_bytes(root, ref, rel)
    if data is None:
        raise GitError(f"{rel} does not exist at {ref}", 404)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GitError(f"{rel} at {ref} is not a UTF-8 text file", 415) from exc


# --------------------------------------------------------------------------- commit, tags


class CommitResult(BaseModel):
    sha: str
    subject: str
    files: list[str] = []  # + the paths given to the commit


def changed_paths(state: GitStatus) -> list[str]:
    """Every path `git status` lists (both sides of a rename), in order, without repeats."""
    out: list[str] = []
    for item in state.files:
        for path in (item.orig_path, item.path):
            if path and path not in out:
                out.append(path)
    return out


def commit(root: Path, message: str, paths: Sequence[str] | None = None) -> CommitResult:
    """Commit exactly `paths` (default: every changed or untracked path), then
    `git commit --only -- <paths>`, so changes staged elsewhere stay out. GitError 400 for
    an empty message or when nothing changed in those paths.

    Before committing, paths that exist in the working tree are staged with `git add`;
    paths that do not (a deletion, the old side of a rename) are dropped from the index
    with `git rm -r --cached --ignore-unmatch`, which is a no-op when `git rm` / `git mv`
    already did that: `git add` (with or without `-A`) refuses a path that is in neither
    the index nor the working tree. Selecting the new path of a staged rename selects the
    old path too, so the rename is committed whole (the git panel sends `path` only)."""
    text = message.strip()
    if not text:
        raise GitError("the commit message is empty")
    state = status(root)
    if not state.is_repo:
        raise NotARepoError(root)
    if paths is None:
        chosen = changed_paths(state)
    else:
        renamed = {f.path: f.orig_path for f in state.files if f.status == "R" and f.orig_path}
        chosen = []
        for rel in paths:
            path = check_path(root, rel)
            for item in (path, renamed.get(path)):
                if item and item not in chosen:
                    chosen.append(item)
    if not chosen:
        raise GitError("nothing to commit: the working tree is clean")
    present = [path for path in chosen if os.path.lexists(root / path)]
    gone = [path for path in chosen if path not in present]
    if present:
        run_git(root, "add", "--", *present)
    if gone:
        run_git(root, "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", *gone)
    pending = run_git(root, "diff", "--cached", "--quiet", "--", *chosen, check=False)
    if pending.returncode == 0:
        raise GitError("nothing to commit: those paths have no changes")
    if pending.returncode != 1:
        raise GitError(error_line(_decode(pending.stderr), "git diff --cached failed"))
    run_git(
        root,
        "commit",
        "--only",
        "--cleanup=whitespace",
        "-F",
        "-",
        "--",
        *chosen,
        stdin_text=text + "\n",
    )
    sha = head_sha(root)
    assert sha is not None
    subject = git_text(root, "log", "-1", "--format=%s", sha).strip()
    return CommitResult(sha=sha, subject=subject, files=chosen)


def list_tags(root: Path, prefix: str) -> list[TagInfo]:
    """Tags under refs/tags/<prefix> with the commit they point at, their date and message
    (`git for-each-ref`). Empty outside git."""
    if locate(root) is None:
        return []
    fmt = FIELD.join(
        (
            "%(refname)",
            "%(objectname)",
            "%(*objectname)",
            "%(creatordate:unix)",
            "%(contents:subject)",
            "%(contents:body)",
        )
    )
    out = git_text(root, "for-each-ref", f"--format={fmt}%1e", f"refs/tags/{prefix}")
    tags = []
    for record in out.split(RECORD):
        record = record.strip("\n")
        if not record:
            continue
        ref, obj, peeled, date, subject, body = (record.split(FIELD) + [""] * 6)[:6]
        message = subject.strip() + ("\n\n" + body.strip() if body.strip() else "")
        tags.append(
            TagInfo(
                name=ref.removeprefix("refs/tags/"),
                sha=peeled or obj,
                date=float(date or 0),
                message=message,
            )
        )
    return tags


def create_tag(root: Path, name: str, message: str) -> str:
    """An annotated tag `name` on HEAD with `message`; returns the commit sha it tags."""
    run_git(root, "check-ref-format", f"refs/tags/{name}")
    run_git(
        root,
        "tag",
        "-a",
        "--cleanup=whitespace",
        "-F",
        "-",
        "--",
        name,
        "HEAD",
        stdin_text=message.strip() + "\n",
    )
    return resolve_commit(root, f"refs/tags/{name}")


# --------------------------------------------------------------------------- push / pull


def remote_command(root: Path, op: RemoteOp) -> list[str]:
    """The git arguments for a push or pull (GitError 400 when there is nothing to talk to).

    push: `git push --follow-tags` (annotated snapshot tags go along); a branch without an
    upstream is pushed with `-u <origin or the only remote> HEAD`.
    pull: `git -c fetch.pruneTags=false pull` (the user's merge/rebase settings apply)."""
    state = status(root)
    if not state.is_repo:
        raise NotARepoError(root)
    if not state.remotes:
        raise GitError("no remote is configured (git remote add origin <url>)")
    if op == "pull":
        return ["-c", "fetch.pruneTags=false", "pull"]
    if state.upstream is None and state.branch is not None:
        remote = "origin" if "origin" in state.remotes else state.remotes[0]
        return ["push", "--follow-tags", "-u", remote, "HEAD"]
    return ["push", "--follow-tags"]


ChangeKind = Literal["added", "modified", "deleted"]


def changed_between(
    root: Path, before: str | None, after: str | None
) -> list[tuple[str, ChangeKind]]:
    """(path relative to the paper root, change) for every path that differs between two
    commits; a rename is a deletion plus an addition."""
    if after is None or before == after:
        return []
    base = before or empty_tree(root)
    out = git_text(
        root, "diff", "--name-status", "--no-renames", "--relative", "-z", base, after, "--", "."
    )
    tokens = [token for token in out.split("\0") if token]
    kinds: dict[str, ChangeKind] = {"A": "added", "D": "deleted"}
    return [
        (tokens[i + 1], kinds.get(tokens[i][:1], "modified")) for i in range(0, len(tokens) - 1, 2)
    ]


def is_spec_path(path: str) -> bool:
    return path == PAPER_JSON_NAME


def _kill_group(pid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, sig)


Reanchor = Callable[[Repo, str], Awaitable[int]]


class GitManager:
    """One git operation at a time (commit, snapshot, push, pull) and the background
    push/pull tasks. Pulls also hold the spec lock, so a spec edit cannot write a stale
    spec over what the pull brought in, and the repo's source lock (taken after the spec
    lock, as everywhere), so a save or a marker insertion cannot interleave with the
    files the pull rewrites. Commit and snapshot do not change the working tree."""

    def __init__(
        self,
        bus: EventBus,
        *,
        spec_lock: asyncio.Lock,
        source_lock: Callable[[Path], asyncio.Lock] | None = None,
        reanchor: Reanchor | None = None,
        timeout_s: float = REMOTE_TIMEOUT_S,
    ) -> None:
        self.bus = bus
        self.spec_lock = spec_lock
        self.source_lock = source_lock
        self.reanchor = reanchor
        self.timeout_s = timeout_s
        self.current: str | None = None
        self._task: asyncio.Task[None] | None = None

    # ----------------------------------------------------------------- the lock

    def begin(self, op: str) -> None:
        if self.current is not None:
            raise GitError(f"git {self.current} is already running; wait for it to finish", 409)
        self.current = op

    def end(self) -> None:
        self.current = None

    @contextlib.asynccontextmanager
    async def exclusive(self, op: str) -> AsyncIterator[None]:
        self.begin(op)
        try:
            yield
        finally:
            self.end()

    # ----------------------------------------------------------------- push / pull

    async def start(self, repo: Repo, op: RemoteOp) -> list[str]:
        """Start a push or pull in the background; returns its git arguments.
        GitError 409 while another git operation runs, 400 when it cannot start."""
        self.begin(op)
        try:
            args = await asyncio.to_thread(remote_command, repo.root, op)
        except BaseException:
            self.end()
            raise
        self._task = asyncio.create_task(self._run(repo, op, args), name=f"git:{op}")
        return ["git", *args]

    async def wait(self) -> None:
        """Wait for the running push/pull (tests)."""
        task = self._task
        if task is not None:
            await asyncio.wait({task})

    async def shutdown(self) -> None:
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _run(self, repo: Repo, op: RemoteOp, args: list[str]) -> None:
        changed: list[str] = []
        try:
            if op == "pull":
                async with contextlib.AsyncExitStack() as locks:
                    await locks.enter_async_context(self.spec_lock)
                    if self.source_lock is not None:
                        await locks.enter_async_context(self.source_lock(repo.root))
                    before = await asyncio.to_thread(head_sha, repo.root)
                    ok, error = await self._stream(repo.root, op, args)
                    changed = await self._after_pull(repo, before)
            else:
                ok, error = await self._stream(repo.root, op, args)
        except asyncio.CancelledError:
            self.bus.publish(GitFinished(op=op, ok=False, error="cancelled"))
            raise
        except Exception as exc:  # a bug, or git vanished: still end the operation
            log.exception("git %s failed", op)
            ok, error = False, f"internal error: {exc}"
        finally:
            self.end()
        self.bus.publish(GitFinished(op=op, ok=ok, error=error, changed=changed))

    async def _stream(self, root: Path, op: RemoteOp, args: list[str]) -> tuple[bool, str | None]:
        """Run git, publishing each output line (stdout and stderr, split on \\n and \\r)."""
        if not git_available():
            return False, "git is not installed (not found on PATH)"
        try:
            process = await asyncio.create_subprocess_exec(
                "git",
                *GIT_CONFIG,
                *args,
                cwd=root,
                env=git_env(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            return False, f"could not run git: {exc}"
        tail: collections.deque[str] = collections.deque(maxlen=OUTPUT_TAIL)

        def emit(raw: bytes) -> None:
            line = _decode(raw).rstrip()
            if line.strip():
                tail.append(line)
                self.bus.publish(GitOutput(op=op, line=line))

        async def pump() -> None:
            assert process.stdout is not None
            pending = b""
            while True:
                chunk = await process.stdout.read(4096)
                if not chunk:
                    break
                pending += chunk
                parts = re.split(rb"\r\n|\r|\n", pending)
                pending = parts.pop()
                for part in parts:
                    emit(part)
            if pending:
                emit(pending)

        try:
            await asyncio.wait_for(asyncio.gather(pump(), process.wait()), self.timeout_s)
        except TimeoutError:
            await self._kill(process)
            return False, f"git {op} timed out after {self.timeout_s:.0f} s and was stopped"
        except asyncio.CancelledError:
            await self._kill(process)
            raise
        if process.returncode == 0:
            return True, None
        fallback = f"git {op} exited with {process.returncode}"
        return False, error_line("\n".join(tail), fallback)

    @staticmethod
    async def _kill(process: asyncio.subprocess.Process) -> None:
        _kill_group(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), KILL_GRACE_S)
        except TimeoutError:
            _kill_group(process.pid, signal.SIGKILL)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), KILL_GRACE_S)

    async def _after_pull(self, repo: Repo, before: str | None) -> list[str]:
        """Announce what the pull changed: `file.changed` per path (and re-anchoring of the
        findings in changed .tex files), `spec.changed` when the spec file changed."""

        def work() -> list[tuple[str, ChangeKind]]:
            after = head_sha(repo.root)
            return changed_between(repo.root, before, after)

        changed = await asyncio.to_thread(work)
        for path, change in changed:
            if is_spec_path(path):
                if await asyncio.to_thread(specwatch.changed_externally, repo.root):
                    self.bus.publish(SpecChanged())
                continue
            target = repo.root / path
            if change != "deleted" and target.is_file():
                event = FileChanged(path=path, change=change, mtime=target.stat().st_mtime)
            else:
                event = FileChanged(path=path, change="deleted", mtime=None)
            self.bus.publish(event)
            if path.endswith(".tex") and self.reanchor is not None:
                try:
                    await self.reanchor(repo, path)
                except Exception:  # best-effort, like the watcher's re-anchoring
                    log.exception("re-anchoring %s after a pull failed", path)
        return [path for path, _ in changed]
