"""Harness tests against the pinned evaluators.

Run inside `nix develop` with `python3 -m unittest discover -s tests`, or
through `nix flake check`. Every test runs once per evaluator.
"""

import os
import platform
import re
import signal
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from evalanche.evaluators import CPPNIX, LIX
from evalanche.harness import (
    Evaluation,
    Expression,
    File,
    Limits,
    LocalStore,
    ReadOnlyStore,
    address_space_wrapper,
    build_argv,
    build_env,
    cgroup_wrapper,
    evaluate,
    evaluate_many,
)

MiB = 2**20
UNSAFE_EXEC = (("allow-unsafe-native-code-during-evaluation", "true"),)
# `builtins.exec` cases run `sleep`, which a build sandbox has only on PATH.
HOST_PATH = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}

# A native stack overflow with the call depth limit raised out of the way.
DEEP_CALLS = 'let f = n: if n == 0 then [ ] else [ "a" ] ++ f (n - 1); in f 10000000'
CHAINED_ATTRSETS = (
    "builtins.seq (builtins.foldl' (acc: i: { inherit acc i; }) { }"
    " (builtins.genList (x: x) 10000000)) null"
)

# Both evaluators recognise a stack overflow only on x86_64, where they read
# the stack pointer in their SIGSEGV handler.
RECOGNISES_STACK_OVERFLOW = platform.machine() in ("x86_64", "AMD64")


def plain(text):
    """Text with ANSI colour codes removed, for assertions only."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text or "")


def group_exists(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def pid_exists(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class HarnessCases:
    evaluator = None

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="evalanche-test-")).resolve()

    def run_expr(self, text, **kwargs):
        result = evaluate(Evaluation(self.evaluator, Expression(text), **kwargs))
        self.assertFalse(group_exists(result.pgid), "process group outlived the result")
        return result

    def assertError(self, result, needle):
        self.assertEqual(result.exit_code, 1, result.stderr[-500:])
        self.assertIsNone(result.signal)
        self.assertFalse(result.timed_out)
        self.assertIsNotNone(result.error, result.stderr[-500:])
        self.assertIn(needle, plain(result.raw_msg or result.error.msg))

    def test_success(self):
        result = self.run_expr("{ a = 1 + 1; }")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout.strip(), "{ a = 2; }")
        self.assertEqual(result.messages, ())
        self.assertGreater(result.peak_rss, MiB)
        self.assertGreater(result.elapsed, 0)

    def test_file_source(self):
        path = self.tmp / "target.nix"
        path.write_text("let x = import ./value.nix; in x * 2\n")
        (self.tmp / "value.nix").write_text("21\n")
        result = evaluate(Evaluation(self.evaluator, File(path)))
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "42")

    def test_throw(self):
        result = self.run_expr('let f = x: throw "planted"; in f 1')
        self.assertError(result, "planted")
        self.assertEqual(result.raw_msg, "planted")
        self.assertGreaterEqual(len(result.trace), 2)
        self.assertTrue(all(frame.line == 1 for frame in result.trace))

    def test_abort(self):
        result = self.run_expr('abort "planted"')
        self.assertError(result, "evaluation aborted with the following error message: 'planted'")

    def test_infinite_recursion(self):
        result = self.run_expr("let x = x; in x")
        self.assertError(result, "infinite recursion encountered")

    def test_max_call_depth(self):
        result = self.run_expr(
            'let f = n: if n == 0 then [ ] else [ "a" ] ++ f (n - 1); in f 100000'
        )
        self.assertError(result, "max-call-depth exceeded")

    def test_native_stack_overflow(self):
        result = self.run_expr(DEEP_CALLS, options=(("max-call-depth", "100000000"),))
        if RECOGNISES_STACK_OVERFLOW:
            self.assertError(result, "stack overflow (possible infinite recursion)")
        else:
            self.assertIsNone(result.exit_code)
            self.assertEqual(result.signal, signal.SIGSEGV)
            self.assertFalse(result.timed_out)
            self.assertEqual(result.messages, ())

    def test_heap_cap_one_allocation(self):
        result = self.run_expr(
            "builtins.length (builtins.genList (x: x) 200000000)",
            limits=Limits(heap="64M"),
        )
        self.assertError(result, "out of memory")
        # The out of memory message carries `msg` and no `raw_msg`.
        self.assertIsNone(result.raw_msg)

    def test_heap_cap_incremental_growth(self):
        result = self.run_expr(CHAINED_ATTRSETS, limits=Limits(heap="128M"))
        if self.evaluator is CPPNIX:
            self.assertEqual(result.signal, signal.SIGABRT)
            self.assertIn("std::bad_alloc", result.stderr)
        else:
            self.assertError(result, "out of memory")
        self.assertFalse(result.timed_out)
        self.assertLess(result.peak_rss, 128 * MiB + 200 * MiB)

    @unittest.skipUnless(sys.platform == "linux", "macOS rejects ulimit -v")
    def test_address_space_limit(self):
        result = self.run_expr(
            CHAINED_ATTRSETS,
            limits=Limits(heap=None, wrapper=address_space_wrapper(2**30)),
        )
        if self.evaluator is CPPNIX:
            self.assertEqual(result.signal, signal.SIGABRT)
            self.assertIn("std::bad_alloc", result.stderr)
        else:
            self.assertError(result, "out of memory")
        self.assertLess(result.peak_rss, 2**30)

    def test_no_heap_cap(self):
        evaluation = Evaluation(self.evaluator, Expression("1"), limits=Limits(heap=None))
        self.assertNotIn("GC_MAXIMUM_HEAP_SIZE", build_env(evaluation))
        self.assertEqual(evaluate(evaluation).exit_code, 0)

    def test_timeout_kills_children(self):
        script = (
            f"echo $$ > {self.tmp}/sh.pid; "
            f"sleep 600 & echo $! > {self.tmp}/sleep.pid; wait"
        )
        start = time.monotonic()
        result = self.run_expr(
            f'builtins.exec [ "/bin/sh" "-c" "{script}" ]',
            limits=Limits(timeout=2.0),
            options=UNSAFE_EXEC,
            env=HOST_PATH,
        )
        self.assertTrue(result.timed_out)
        self.assertEqual(result.signal, signal.SIGKILL)
        self.assertIsNone(result.exit_code)
        self.assertGreaterEqual(result.elapsed, 2.0)
        self.assertLess(time.monotonic() - start, 10.0)
        for name in ("sh", "sleep"):
            pid = int((self.tmp / f"{name}.pid").read_text())
            self.assertFalse(pid_exists(pid), f"{name} {pid} survived the timeout")

    def test_stray_child_after_exit(self):
        result = self.run_expr(
            'builtins.exec [ "/bin/sh" "-c" "sleep 600 >/dev/null 2>&1 & echo 1" ]',
            options=UNSAFE_EXEC,
            env=HOST_PATH,
        )
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "1")
        self.assertTrue(result.strays)
        self.assertFalse(result.timed_out)

    def test_read_only_store_writes_nothing(self):
        fixture = self.tmp / f"fixture-{uuid.uuid4().hex}.txt"
        fixture.write_text(f"{uuid.uuid4()}\n")
        result = self.run_expr(f'"${{{fixture}}}"')
        self.assertEqual(result.exit_code, 0, result.stderr)
        store_path = result.stdout.strip().strip('"')
        self.assertTrue(store_path.startswith("/nix/store/"))
        self.assertFalse(os.path.exists(store_path))
        self.assertNotIn("--read-write-mode", result.argv)

        result = self.run_expr(f'builtins.readFile "${{{fixture}}}"')
        self.assertEqual(result.exit_code, 1)

    def test_local_store_writes_under_root(self):
        fixture = self.tmp / f"fixture-{uuid.uuid4().hex}.txt"
        content = str(uuid.uuid4())
        fixture.write_text(content)
        root = self.tmp / "store"
        store = LocalStore(root)
        result = self.run_expr(f'"${{{fixture}}}"', store=store)
        self.assertEqual(result.exit_code, 0, result.stderr)
        store_path = result.stdout.strip().strip('"')
        self.assertFalse(os.path.exists(store_path))
        self.assertTrue((root / store_path.lstrip("/")).exists())

        result = self.run_expr(f'builtins.readFile "${{{fixture}}}"', store=store)
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f'"{content}"')

    def test_output_limit(self):
        result = self.run_expr(
            'builtins.concatStringsSep "" (builtins.genList (x: "a") 100000)',
            output_limit=1000,
        )
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.stdout_truncated)
        self.assertEqual(len(result.stdout), 1000)

    def test_evaluate_many_keeps_order(self):
        jobs = [
            Evaluation(self.evaluator, Expression(f"{i} * 3"), limits=Limits(timeout=30))
            for i in range(24)
        ]
        jobs[5] = Evaluation(self.evaluator, Expression('throw "five"'))
        results = list(evaluate_many(iter(jobs), jobs=4))
        self.assertEqual(len(results), 24)
        for i, result in enumerate(results):
            if i == 5:
                self.assertEqual(result.raw_msg, "five")
            else:
                self.assertEqual(result.stdout.strip(), str(i * 3))

    def test_evaluate_many_runs_concurrently(self):
        jobs = [
            Evaluation(
                self.evaluator,
                Expression('builtins.exec [ "/bin/sh" "-c" "sleep 1; echo 1" ]'),
                options=UNSAFE_EXEC,
                env=HOST_PATH,
            )
            for _ in range(4)
        ]
        start = time.monotonic()
        results = list(evaluate_many(jobs, jobs=4))
        self.assertTrue(all(r.exit_code == 0 for r in results))
        self.assertLess(time.monotonic() - start, 3.0)


class CppNixHarness(HarnessCases, unittest.TestCase):
    evaluator = CPPNIX


class LixHarness(HarnessCases, unittest.TestCase):
    evaluator = LIX


class ArgvTests(unittest.TestCase):
    def test_argv_and_wrappers(self):
        argv = build_argv(
            Evaluation(
                CPPNIX,
                File(Path("/x.nix")),
                limits=Limits(wrapper=address_space_wrapper(2**30)),
                store=ReadOnlyStore(),
            )
        )
        self.assertEqual(argv[:4], ["/bin/sh", "-c", 'ulimit -v "$0" && exec "$@"', "1048576"])
        self.assertEqual(
            argv[5:],
            ["--eval", "--strict", "--log-format", "internal-json", "--show-trace", "--", "/x.nix"],
        )
        self.assertEqual(cgroup_wrapper("512M")[-1], "--")
        self.assertIn("MemoryMax=512M", cgroup_wrapper("512M"))

    def test_jobs_must_be_positive(self):
        with self.assertRaises(ValueError):
            list(evaluate_many([], jobs=0))


if __name__ == "__main__":
    sys.exit(unittest.main())
