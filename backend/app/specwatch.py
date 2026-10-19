"""Which content of the spec file the app already knows about (phase-5 brief, "watcher").

The file watcher (app/events.py) and the post-pull hook (app/gitops.py) publish
`spec.changed` only when the spec file's content differs from what the app last wrote or
last announced. app.spec.write_spec records the hash of every text it writes *before* the
write, so the watcher never mistakes the editor's own save for an external change.

One entry per repo root. Thread-safe: write_spec runs in worker threads, the watcher on the
event loop. Nothing here writes files.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

from app.repo import Repo

_KNOWN: dict[Path, str] = {}
_LOCK = threading.Lock()


def content_hash(data: bytes | str) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


def _key(root: Path) -> Path:
    return root.resolve()


def _current(repo: Repo) -> str | None:
    """Hash of the spec file on disk, or None when it is missing or unreadable."""
    try:
        return content_hash(repo.paper_json_path.read_bytes())
    except OSError:
        return None


def remember(root: Path, text: str) -> None:
    """The app is about to write (or just confirmed) this spec text."""
    with _LOCK:
        _KNOWN[_key(root)] = content_hash(text)


def known(root: Path) -> str | None:
    with _LOCK:
        return _KNOWN.get(_key(root))


def prime(root: Path) -> None:
    """Remember the current content if nothing is known yet (watcher start-up), so a touch
    without a content change is not announced."""
    current = _current(Repo.at(root))
    with _LOCK:
        _KNOWN.setdefault(_key(root), current or "")


def changed_externally(root: Path) -> bool:
    """True once per new content: the file differs from the last write or announcement.

    The new hash is remembered, so the watcher and the pull hook (whichever looks first)
    announce one change once."""
    current = _current(Repo.at(root)) or ""
    with _LOCK:
        key = _key(root)
        if _KNOWN.get(key) == current:
            return False
        _KNOWN[key] = current
        return True


def forget(root: Path) -> None:
    with _LOCK:
        _KNOWN.pop(_key(root), None)
