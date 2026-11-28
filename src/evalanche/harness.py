"""Run one evaluation per evaluator process and report what happened.

Each evaluation is a `nix-instantiate --eval --strict` process in its own
session, so the process and every child it starts share one process group.
The Boehm heap is capped with `GC_MAXIMUM_HEAP_SIZE`, and a wall-clock timeout
kills the whole group. The result is raw. It records the exit status or
signal, the level 0 messages from `--log-format internal-json`, the elapsed
time and the peak RSS, and leaves classifying them to the caller.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Iterable, Iterator, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from evalanche.evaluators import Evaluator

DEFAULT_TIMEOUT = 10.0
DEFAULT_HEAP = "256M"
DEFAULT_OUTPUT_LIMIT = 16 * 2**20

# Seconds to wait for a killed process group to disappear.
GROUP_EXIT_GRACE = 5.0


@dataclass(frozen=True)
class Expression:
    """Nix source text, passed with `-E`. Relative paths resolve against `cwd`."""

    text: str


@dataclass(frozen=True)
class File:
    """A Nix file, passed as a path argument."""

    path: Path


Source = Expression | File


@dataclass(frozen=True)
class ReadOnlyStore:
    """The default store, opened read-only. Store paths are computed and not written."""


@dataclass(frozen=True)
class LocalStore:
    """A private local store under `root`, opened read-write.

    `root` must be an absolute path with no symlink in it. On macOS that
    rules out `/var` and `/tmp`, so resolve the path first.
    """

    root: Path


Store = ReadOnlyStore | LocalStore


@dataclass(frozen=True)
class Limits:
    """Resource limits for one evaluation.

    `heap` is the value of `GC_MAXIMUM_HEAP_SIZE`, or `None` for no cap. It
    bounds only the Boehm heap. `wrapper` is an argv prefix that runs the
    evaluator under a further limit, such as `address_space_wrapper` or
    `cgroup_wrapper` on Linux. The wrapper must exec the evaluator in place so
    that the process it starts stays the group leader.
    """

    timeout: float = DEFAULT_TIMEOUT
    heap: str | None = DEFAULT_HEAP
    wrapper: tuple[str, ...] = ()


def address_space_wrapper(limit_bytes: int) -> tuple[str, ...]:
    """An argv prefix that sets `ulimit -v` before exec. Linux only, since macOS rejects it."""
    kib = max(1, limit_bytes // 1024)
    return ("/bin/sh", "-c", 'ulimit -v "$0" && exec "$@"', str(kib))


def cgroup_wrapper(memory_max: str) -> tuple[str, ...]:
    """An argv prefix that runs the evaluator in a transient systemd user scope.

    The scope has `MemoryMax=memory_max` and no swap. The kernel kills the
    scope with SIGKILL and no message when it exceeds the limit. This needs a
    systemd user manager with the `memory` controller delegated.
    """
    return (
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "-p",
        f"MemoryMax={memory_max}",
        "-p",
        "MemorySwapMax=0",
        "--",
    )


@dataclass(frozen=True)
class Evaluation:
    """One evaluation of `source` under `evaluator`."""

    evaluator: Evaluator
    source: Source
    limits: Limits = Limits()
    store: Store = ReadOnlyStore()
    options: tuple[tuple[str, str], ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: Path | None = None
    output_limit: int = DEFAULT_OUTPUT_LIMIT


@dataclass(frozen=True)
class Frame:
    """One entry of a message's `trace` array."""

    raw_msg: str
    file: str | None
    line: int | None
    column: int | None


@dataclass(frozen=True)
class Message:
    """One level 0 `msg` line from `--log-format internal-json`.

    `raw_msg` and `msg` keep the ANSI codes the evaluator writes.
    """

    msg: str
    raw_msg: str | None
    file: str | None
    line: int | None
    column: int | None
    trace: tuple[Frame, ...]


@dataclass(frozen=True)
class RawResult:
    """What one evaluator process did.

    Exactly one of `exit_code` and `signal` is set. A harness timeout shows
    as `timed_out` with `signal` set to SIGKILL. `peak_rss` is in bytes and
    covers the evaluator process and any children it waited for.
    `strays` is true when processes were still in the group after the
    evaluator exited. The harness kills them before it returns.
    """

    evaluator: str
    argv: tuple[str, ...]
    exit_code: int | None
    signal: int | None
    timed_out: bool
    elapsed: float
    peak_rss: int
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    messages: tuple[Message, ...]
    strays: bool
    pgid: int

    @property
    def error(self) -> Message | None:
        """The last level 0 message, which holds the evaluation error."""
        return self.messages[-1] if self.messages else None

    @property
    def raw_msg(self) -> str | None:
        return self.error.raw_msg if self.error else None

    @property
    def trace(self) -> tuple[Frame, ...]:
        return self.error.trace if self.error else ()


def build_argv(evaluation: Evaluation) -> list[str]:
    argv = [
        *evaluation.limits.wrapper,
        str(evaluation.evaluator.bin_dir() / "nix-instantiate"),
        "--eval",
        "--strict",
        "--log-format",
        "internal-json",
        "--show-trace",
    ]
    match evaluation.store:
        case ReadOnlyStore():
            pass
        case LocalStore(root):
            argv += ["--read-write-mode", "--store", f"local?root={root}"]
    for name, value in evaluation.options:
        argv += ["--option", name, value]
    match evaluation.source:
        case Expression(text):
            argv += ["-E", text]
        case File(path):
            argv += ["--", str(path)]
    return argv


def build_env(evaluation: Evaluation) -> dict[str, str]:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": os.environ.get("HOME", "/homeless-shelter"),
        "NIX_PATH": "",
        "NO_COLOR": "1",
        "TERM": "dumb",
    }
    # A build sandbox has no /nix/var, and the evaluators need a writable
    # state directory to start.
    if "NIX_STATE_DIR" in os.environ:
        env["NIX_STATE_DIR"] = os.environ["NIX_STATE_DIR"]
    if evaluation.limits.heap is not None:
        env["GC_MAXIMUM_HEAP_SIZE"] = evaluation.limits.heap
    env.update(evaluation.env)
    return env


def parse_messages(stderr: str) -> tuple[Message, ...]:
    """Level 0 `msg` lines from `--log-format internal-json` output."""
    messages = []
    for line in stderr.splitlines():
        if not line.startswith("@nix "):
            continue
        try:
            obj = json.loads(line[5:])
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj.get("action") != "msg" or obj.get("level") != 0:
            continue
        frames = tuple(
            Frame(
                raw_msg=str(f.get("raw_msg", "")),
                file=f.get("file"),
                line=f.get("line"),
                column=f.get("column"),
            )
            for f in obj.get("trace") or ()
            if isinstance(f, dict)
        )
        messages.append(
            Message(
                msg=str(obj.get("msg", "")),
                raw_msg=obj.get("raw_msg"),
                file=obj.get("file"),
                line=obj.get("line"),
                column=obj.get("column"),
                trace=frames,
            )
        )
    return tuple(messages)


class _Capture:
    """Drains a pipe on a thread and keeps at most `limit` bytes."""

    def __init__(self, stream, limit: int):
        self.chunks: list[bytes] = []
        self.size = 0
        self.truncated = False
        self.limit = limit
        self.thread = threading.Thread(target=self._drain, args=(stream,), daemon=True)
        self.thread.start()

    def _drain(self, stream) -> None:
        with stream:
            for data in iter(lambda: stream.read(65536), b""):
                room = self.limit - self.size
                if room <= 0:
                    self.truncated = True
                    continue
                if len(data) > room:
                    data = data[:room]
                    self.truncated = True
                self.chunks.append(data)
                self.size += len(data)

    def text(self) -> str:
        self.thread.join()
        return b"".join(self.chunks).decode(errors="replace")


def _group_alive(pgid: int) -> bool | None:
    """True if the group has a member we can signal, False if it is gone.

    None means signalling the group fails with EPERM. Darwin returns that
    for a group whose members are all zombies waiting to be reaped.
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return None
    return True


def _kill_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _peak_rss_bytes(ru_maxrss: int) -> int:
    # Darwin reports bytes and Linux reports KiB.
    return ru_maxrss if sys.platform == "darwin" else ru_maxrss * 1024


def evaluate(evaluation: Evaluation) -> RawResult:
    """Run one evaluation and wait for its process group to be gone."""
    argv = build_argv(evaluation)
    start = time.monotonic()
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=build_env(evaluation),
        cwd=evaluation.cwd,
        start_new_session=True,
    )
    pgid = proc.pid
    stdout = _Capture(proc.stdout, evaluation.output_limit)
    stderr = _Capture(proc.stderr, evaluation.output_limit)

    timed_out = threading.Event()
    reaped = threading.Lock()
    leader_alive = True

    def on_timeout() -> None:
        # The leader is not reaped while this holds the lock, so its pid is
        # still the group id and the signal cannot reach a reused pid.
        # A leader that has exited and waits to be reaped did not time out.
        with reaped:
            if not leader_alive:
                return
            exited = os.waitid(os.P_PID, pgid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if exited is None:
                timed_out.set()
            _kill_group(pgid)

    timer = threading.Timer(evaluation.limits.timeout, on_timeout)
    timer.daemon = True
    timer.start()
    try:
        # Wait without reaping, then reap under the lock.
        os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT)
        with reaped:
            _, status, rusage = os.wait4(proc.pid, 0)
            leader_alive = False
    finally:
        timer.cancel()
    elapsed = time.monotonic() - start
    proc.returncode = os.waitstatus_to_exitcode(status)
    # A leader already inside exit when SIGKILL arrives keeps its own status,
    # and the result reports that status rather than a timeout.
    killed = (
        timed_out.is_set()
        and os.WIFSIGNALED(status)
        and os.WTERMSIG(status) == signal.SIGKILL
    )

    # Children can outlive the evaluator. They keep the group id, and the
    # kernel does not hand out a pid that is still a group id, so killpg is
    # safe after the leader is reaped.
    state = _group_alive(pgid)
    strays = state is True
    deadline = time.monotonic() + GROUP_EXIT_GRACE
    while state is not False:
        if state:
            _kill_group(pgid)
        if time.monotonic() > deadline:
            if state:
                raise RuntimeError(f"process group {pgid} survived SIGKILL")
            break
        time.sleep(0.01)
        state = _group_alive(pgid)

    err = stderr.text()
    return RawResult(
        evaluator=evaluation.evaluator.name,
        argv=tuple(argv),
        exit_code=os.WEXITSTATUS(status) if os.WIFEXITED(status) else None,
        signal=os.WTERMSIG(status) if os.WIFSIGNALED(status) else None,
        timed_out=killed,
        elapsed=elapsed,
        peak_rss=_peak_rss_bytes(rusage.ru_maxrss),
        stdout=stdout.text(),
        stderr=err,
        stdout_truncated=stdout.truncated,
        stderr_truncated=stderr.truncated,
        messages=parse_messages(err),
        strays=strays,
        pgid=pgid,
    )


def evaluate_many(evaluations: Iterable[Evaluation], jobs: int) -> Iterator[RawResult]:
    """Run evaluations `jobs` at a time and yield results in input order.

    At most `2 * jobs` evaluations are taken from the iterable ahead of the
    results consumed, so a generator of candidates is not drained up front.
    """
    if jobs < 1:
        raise ValueError("jobs must be at least 1")
    pending: deque[Future[RawResult]] = deque()
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        for evaluation in evaluations:
            pending.append(pool.submit(evaluate, evaluation))
            if len(pending) >= 2 * jobs:
                yield pending.popleft().result()
        while pending:
            yield pending.popleft().result()

