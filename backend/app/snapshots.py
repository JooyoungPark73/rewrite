"""Revision snapshots: "flag major revision" (PLAN.md "Revisions and git"; phase-5 brief).

A snapshot is an annotated git tag `rewrite/rev-<n>-<slug>` on HEAD whose message is the
note; n is one more than the highest n in use and the slug comes from the note. A dirty tree
is committed first (every changed path, message `Snapshot: <note>`), so the tag always names
the text the author is looking at.

Rows in state.db (`snapshots`) give each tag a stable id; the tags are the truth. Listing
reconciles the two: a tag without a row (a fresh clone, a tag pushed by a collaborator)
gets one, and rows whose tag is gone are not listed.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
import time
from pathlib import Path

from pydantic import BaseModel

from app import db, gitops
from app.gitops import GitError, NotARepoError, TagInfo
from app.repo import Repo
from app.spec import slugify

TAG_NAMESPACE = "rewrite/"
REV_RE = re.compile(r"^rewrite/rev-(?P<n>\d+)(?:-|$)")
SLUG_MAX = 32


class Snapshot(BaseModel):
    id: str
    tag: str
    sha: str  # the commit the tag points at
    note: str
    created_at: float


class SnapshotCreated(BaseModel):
    snapshot: Snapshot
    commit: str | None = None  # + the commit made first because the tree was dirty


def new_id() -> str:
    return f"s{int(time.time() * 1000):x}{secrets.token_hex(2)}"


def rev_number(tag: str) -> int | None:
    match = REV_RE.match(tag)
    return int(match.group("n")) if match else None


def tag_name(n: int, note: str) -> str:
    return f"{TAG_NAMESPACE}rev-{n}-{slugify(note, SLUG_MAX, 'snapshot')}"


def _rows(conn: sqlite3.Connection) -> dict[str, tuple[str, str, str, float]]:
    """tag → (id, sha, note, created_at)."""
    return {
        str(tag): (str(id_), str(sha), str(note), float(created))
        for id_, tag, sha, note, created in conn.execute(
            "SELECT id, tag, sha, note, created_at FROM snapshots"
        )
    }


def _sync(conn: sqlite3.Connection, tags: list[TagInfo]) -> list[Snapshot]:
    """Snapshots for the given tags, creating or correcting rows as needed."""
    rows = _rows(conn)
    out: list[Snapshot] = []
    with db.transaction(conn):
        for tag in tags:
            row = rows.get(tag.name)
            if row is None:
                snapshot = Snapshot(
                    id=new_id(), tag=tag.name, sha=tag.sha, note=tag.message, created_at=tag.date
                )
                conn.execute(
                    "INSERT INTO snapshots (id, tag, sha, note, created_at) VALUES (?, ?, ?, ?, ?)",
                    (snapshot.id, snapshot.tag, snapshot.sha, snapshot.note, snapshot.created_at),
                )
            else:
                id_, sha, note, created = row
                if sha != tag.sha:  # the tag was moved (or re-created) outside the app
                    conn.execute("UPDATE snapshots SET sha = ? WHERE id = ?", (tag.sha, id_))
                snapshot = Snapshot(
                    id=id_, tag=tag.name, sha=tag.sha, note=tag.message or note, created_at=created
                )
            out.append(snapshot)
    out.sort(key=lambda s: (rev_number(s.tag) or 0, s.created_at), reverse=True)
    return out


def list_snapshots(repo: Repo) -> list[Snapshot]:
    """Every `rewrite/*` tag as a snapshot, newest revision first (empty outside git)."""
    tags = gitops.list_tags(repo.root, TAG_NAMESPACE)
    if not tags:
        return []
    with db.connect(repo) as conn:
        return _sync(conn, tags)


def next_number(root: Path, conn: sqlite3.Connection) -> int:
    """1 + the highest rev number among the tags and the rows (a deleted tag's number is
    not handed out again, so a collaborator's copy of it cannot clash)."""
    used = [rev_number(tag.name) for tag in gitops.list_tags(root, TAG_NAMESPACE)]
    used += [rev_number(str(row[0])) for row in conn.execute("SELECT tag FROM snapshots")]
    return 1 + max((n for n in used if n is not None), default=0)


def create_snapshot(repo: Repo, note: str) -> SnapshotCreated:
    """Commit a dirty tree, then tag HEAD. Blocking; the caller holds the git lock."""
    text = note.strip()
    if not text:
        raise GitError("a snapshot needs a note")
    root = repo.root
    state = gitops.status(root)
    if not state.is_repo:
        raise NotARepoError(root)
    made: str | None = None
    if not state.clean:
        subject = text.splitlines()[0]
        body = "\n".join(text.splitlines()[1:]).strip()
        message = f"Snapshot: {subject}" + (f"\n\n{body}" if body else "")
        made = gitops.commit(root, message).sha
    if gitops.head_sha(root) is None:
        raise GitError("nothing to snapshot: the repository has no commits yet")
    with db.connect(repo) as conn:
        tag = tag_name(next_number(root, conn), text)
        sha = gitops.create_tag(root, tag, text)
        snapshot = Snapshot(id=new_id(), tag=tag, sha=sha, note=text, created_at=time.time())
        conn.execute(
            "INSERT INTO snapshots (id, tag, sha, note, created_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(tag) DO UPDATE SET sha = excluded.sha, note = excluded.note, "
            "created_at = excluded.created_at",
            (snapshot.id, snapshot.tag, snapshot.sha, snapshot.note, snapshot.created_at),
        )
        row = conn.execute("SELECT id FROM snapshots WHERE tag = ?", (tag,)).fetchone()
        snapshot.id = str(row[0])
    return SnapshotCreated(snapshot=snapshot, commit=made)


def resolve(repo: Repo, ref: str) -> tuple[str, str]:
    """(tag, commit sha) of a snapshot named by its id or its tag (GitError 400 if none)."""
    ref = ref.strip()
    for snapshot in list_snapshots(repo):
        if ref in (snapshot.id, snapshot.tag):
            return snapshot.tag, snapshot.sha
    raise GitError(f"unknown snapshot {ref!r}")
