"""HTTP and WebSocket routers."""

from app.api import (
    compare,
    compile,
    config,
    files,
    findings,
    git,
    groups,
    repo,
    runs,
    snapshots,
    spec,
    synctex,
    ws,
)

routers = [
    repo.router,
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
    groups.router,
    ws.router,
]

__all__ = ["routers"]
