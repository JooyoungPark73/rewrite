"""`rewrite` command line: `init <repo>` and `serve [--repo PATH] [--port N]`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app import __version__


def _cmd_init(args: argparse.Namespace) -> int:
    from app.repo import init_repo

    try:
        report = init_repo(Path(args.repo))
    except (NotADirectoryError, FileNotFoundError, OSError) as exc:
        print(f"rewrite init: {exc}", file=sys.stderr)
        return 1
    print(f"rewrite init: {report.repo.root}")
    for label, items in (("created", report.created), ("updated", report.updated)):
        for item in items:
            print(f"  {label:8} {item}")
    if not report.changed:
        print("  nothing to do; already initialised")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from app.main import HOST, create_app, repo_from_env
    from app.repo import Repo

    repo_path: Path | None
    if args.repo:
        repo = Repo.at(args.repo)
        if not repo.is_initialized:
            print(
                f"rewrite serve: {repo.root} has no .rewrite/; "
                f"run `rewrite init {args.repo}` first",
                file=sys.stderr,
            )
            return 1
        repo_path = repo.root
    else:
        repo_path = repo_from_env()
    app = create_app(repo_path)
    # Loopback only: there is no auth, so the bind address is the security boundary.
    uvicorn.run(app, host=HOST, port=args.port, log_level=args.log_level)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rewrite", description="rewrite")
    parser.add_argument("--version", action="version", version=f"rewrite {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="create .rewrite/ in a paper repo (idempotent)")
    p_init.add_argument("repo", help="path to the paper repo")
    p_init.set_defaults(func=_cmd_init)

    p_serve = sub.add_parser("serve", help="run the backend on 127.0.0.1")
    p_serve.add_argument("--repo", help="paper repo to open (default: $REWRITE_REPO)")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--log-level", default="info")
    p_serve.set_defaults(func=_cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
