"""HTTP and WebSocket routers."""

from app.api import (
    compare,
    compile,
    config,
    drafts,
    files,
    findings,
    fs,
    git,
    groups,
    repo,
    revise,
    runs,
    snapshots,
    spec,
    synctex,
    ws,
)

routers = [
    repo.router,
    fs.router,
    files.router,
    compile.router,
    config.router,
    synctex.router,
    spec.router,
    runs.router,
    findings.router,
    git.router,
    snapshots.router,
    compare.router,
    drafts.router,
    revise.router,
    groups.router,
    ws.router,
]

__all__ = ["routers"]
