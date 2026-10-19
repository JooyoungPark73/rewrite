"""DockerLatexmk: latexmk inside `texlive/texlive:TL<year>-historic` (PLAN.md "Compiler details").

The repo is bind-mounted at its own host path and used as the working directory, so the
paths TeX writes into the log and the .synctex.gz are host paths and need no translation.
The command (verified on Linux, docker 29, TL2024-historic, 2026-10-18):

    docker run --rm --pull=never --name rewrite-<hex>
        -v <repo>:<repo> -w <repo> -u <uid>:<gid> -e HOME=/tmp
        -e max_print_line=10000 -e error_line=254 -e half_error_line=238
        texlive/texlive:TL<year>-historic
        latexmk -pdf -synctex=1 -interaction=nonstopmode -output-directory=.rewrite/build <main>

- `-u <uid>:<gid>`: the build output is owned by the host user instead of root.
- `-e HOME=/tmp`: that uid has no passwd entry in the image, so HOME would be `/`, which it
  cannot write; TEXMFVAR (font caches, mktexpk output) lives under $HOME/.texlive<year>.
- `-e max_print_line=...`: the same unwrapped-log settings as the native compiler.
- `--pull=never`: pulling is explicit (POST /api/compile/docker/pull); a compile must never
  start a multi-gigabyte download behind a timeout.
- `--name`: killing the docker CLI on a timeout does not stop the container; `docker kill
  <name>` does.

`pull()` streams `docker pull` output to the event bus. Both compilers share
`run_process` and `collect_result` (log parsing) from app/compiler/native.py.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path

from app.compiler import CompileResult
from app.compiler.native import (
    BUILD_DIR,
    DEFAULT_TIMEOUT_S,
    TEX_LOG_ENV,
    collect_result,
    failure_result,
    latexmk_command,
    run_process,
    timeout_error,
)
from app.events import DockerPull, DockerPullFinished

IMAGE_REPOSITORY = "texlive/texlive"
CONTAINER_HOME = "/tmp"
PROBE_TIMEOUT_S = 10.0  # `docker version` / `docker image inspect`
KILL_TIMEOUT_S = 15.0
# docker run's own failures (daemon error, cannot exec, command not found in the image).
# latexmk's exit codes are all small, so these never come from the compile itself.
DOCKER_RUN_ERRORS = (125, 126, 127)


def docker_image(year: int) -> str:
    """`texlive/texlive:TL<year>-historic`, the image pinned to one TeX Live release."""
    return f"{IMAGE_REPOSITORY}:TL{year}-historic"


def container_name() -> str:
    return f"rewrite-{uuid.uuid4().hex[:12]}"


def user_args() -> list[str]:
    """`-u uid:gid` so files written into the bind mount belong to the host user."""
    if not hasattr(os, "getuid"):  # not on POSIX; Docker Desktop maps ownership itself
        return []
    return ["-u", f"{os.getuid()}:{os.getgid()}"]


def mount_point(repo: Path, container_root: str | None) -> str:
    """Where the repo appears inside the container: its host path unless configured."""
    return (container_root or "").rstrip("/") or str(repo)


def run_prefix(
    repo: Path,
    image: str,
    *,
    container_root: str | None = None,
    env: Mapping[str, str] | None = None,
    name: str | None = None,
    docker: str = "docker",
) -> list[str]:
    """`docker run ... <image>`: everything before the command run in the container."""
    target = mount_point(repo, container_root)
    cmd = [docker, "run", "--rm", "--pull=never"]
    if name:
        cmd += ["--name", name]
    cmd += ["-v", f"{repo}:{target}", "-w", target, *user_args(), "-e", f"HOME={CONTAINER_HOME}"]
    for key, value in (env or {}).items():
        cmd += ["-e", f"{key}={value}"]
    cmd.append(image)
    return cmd


def kill_container(name: str, docker: str = "docker") -> None:
    """Stop a running container started with `--name` (best effort; `--rm` removes it)."""
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            [docker, "kill", name],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=KILL_TIMEOUT_S,
            check=False,
        )


def _probe(cmd: list[str]) -> bool:
    try:
        done = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def docker_available(docker: str = "docker") -> bool:
    """The docker CLI is on PATH and its daemon answers."""
    if shutil.which(docker) is None:
        return False
    return _probe([docker, "version", "--format", "{{.Server.Version}}"])


def image_present(year: int, docker: str = "docker") -> bool:
    """`docker image inspect` finds the TeX Live image for `year` locally."""
    if shutil.which(docker) is None:
        return False
    return _probe([docker, "image", "inspect", "--format", "{{.Id}}", docker_image(year)])


class DockerLatexmk:
    """latexmk in `texlive/texlive:TL<year>-historic`, repo mounted at its host path."""

    def __init__(
        self,
        texlive_year: int = 2024,
        *,
        container_root: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        docker: str = "docker",
    ) -> None:
        self.texlive_year = texlive_year
        self.container_root = container_root
        self.timeout_s = timeout_s
        self.docker = docker

    @property
    def image(self) -> str:
        return docker_image(self.texlive_year)

    def command(self, repo: Path, main: str, *, name: str | None = None) -> list[str]:
        prefix = run_prefix(
            repo,
            self.image,
            container_root=self.container_root,
            env=TEX_LOG_ENV,
            name=name,
            docker=self.docker,
        )
        return prefix + latexmk_command(main)

    def compile(self, repo: Path, main: str) -> CompileResult:
        started = time.monotonic()
        repo = repo.resolve()
        if shutil.which(self.docker) is None:
            return failure_result(main, started, "docker was not found on PATH")
        if not (repo / main).is_file():
            return failure_result(main, started, f"main file not found: {main}")
        (repo / BUILD_DIR).mkdir(parents=True, exist_ok=True)

        name = container_name()
        console, exit_code = run_process(
            self.command(repo, main, name=name),
            cwd=repo,
            env=os.environ,
            timeout_s=self.timeout_s,
            on_timeout=lambda: kill_container(name, self.docker),
        )
        if exit_code in DOCKER_RUN_ERRORS:
            # The container never ran latexmk; whatever is in build/ is from an earlier run.
            return failure_result(main, started, self._run_error(console, exit_code), console)
        extra = [] if exit_code is not None else [timeout_error(main, "latexmk", self.timeout_s)]
        roots = [self.container_root] if self.container_root else []
        return collect_result(
            repo,
            main,
            exit_code=exit_code,
            console=console,
            started=started,
            extra_errors=extra,
            extra_roots=roots,
        )

    def _run_error(self, console: str, exit_code: int | None) -> str:
        if "No such image" in console:
            return (
                f"Docker image {self.image} is not present; pull it first "
                "(Settings → Pull image, or `docker pull " + self.image + "`)"
            )
        if "Cannot connect to the Docker daemon" in console:
            return "the Docker daemon is not running"
        tail = [ln.strip() for ln in console.strip().splitlines() if ln.strip()][-2:]
        detail = " / ".join(tail) if tail else "no output"
        return f"docker run failed with exit code {exit_code}: {detail}"


PullPublish = Callable[[DockerPull | DockerPullFinished], None]


async def pull(year: int, publish: PullPublish, *, docker: str = "docker") -> bool:
    """`docker pull` the image for `year`, publishing each output line, then a finished event.

    Must run on the event loop (EventBus.publish is not thread-safe). Returns success.
    Cancelling the task kills `docker pull` and still publishes `docker.pull.finished`.
    """
    image = docker_image(year)
    if shutil.which(docker) is None:
        publish(DockerPullFinished(ok=False, error="docker was not found on PATH", image=image))
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            docker,
            "pull",
            image,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
            limit=1024 * 1024,
        )
    except OSError as exc:
        publish(DockerPullFinished(ok=False, error=f"could not run docker: {exc}", image=image))
        return False

    tail: collections.deque[str] = collections.deque(maxlen=3)
    try:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            # Without a TTY docker prints one status per line; split on \r just in case.
            for part in raw.decode("utf-8", errors="replace").split("\r"):
                line = part.rstrip("\n").rstrip()
                if line:
                    tail.append(line)
                    publish(DockerPull(line=line, image=image))
        code = await proc.wait()
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=5)
        publish(DockerPullFinished(ok=False, error="pull cancelled", image=image))
        raise

    if code == 0:
        publish(DockerPullFinished(ok=True, error=None, image=image))
        return True
    error = tail[-1] if tail else f"docker pull exited with {code}"
    publish(DockerPullFinished(ok=False, error=error, image=image))
    return False
