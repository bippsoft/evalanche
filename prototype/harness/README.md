# Evaluation harness prototype

Throwaway code for #12. It measures the cost of one evaluation and checks
which isolation mechanism catches and tells apart each failure class. The
decision is a process per evaluation, run as `nix-instantiate --eval --strict
--log-format internal-json` in its own session with `GC_MAXIMUM_HEAP_SIZE` set
and a timeout that kills the process group.

All numbers come from CppNix `2.36.0` and Lix `2.95.4` at nixpkgs `2786a2a` on
aarch64-darwin (Apple M6, 12 cores, 24 GiB), with `GC_MAXIMUM_HEAP_SIZE=256M`
unless stated.

## Commands

```
nix develop -c python3 -I prototype/harness/harness.py cost
nix develop -c python3 -I prototype/harness/harness.py failures
nix develop -c python3 -I prototype/harness/harness.py groupkill
nix develop -c python3 -I prototype/harness/harness.py store
nix develop -c python3 -I prototype/harness/harness.py warm-memory
nix develop -c python3 -I prototype/harness/harness.py compare
nix develop -c python3 -I prototype/harness/harness.py nixos
nix develop -c python3 -I prototype/harness/prefork.py
nix develop -c python3 -I prototype/harness/evaljobs.py
```

`fixture/module.nix` is a small `lib.evalModules` target with `str`, `port`,
`listOf`, an `attrsOf` submodule and `nullOr path` options. Each evaluation
adds one generated module and forces every option value except `_module`.

## Mechanisms

- **Cold**: one `nix-instantiate` process per evaluation.
- **Warm repl**: one `nix repl` per worker with `lib` loaded, fed
  `builtins.deepSeq (import FILE) "DONE-n"` over stdin and restarted after any
  outcome other than success.
- **Prefork**: the CppNix C API loads `lib` in a parent, and a forked child runs
  each evaluation. Lix `2.95.4` ships C++ headers only and has no C API.
- **nix-eval-jobs**: one attribute per candidate, each wrapped in a stub
  derivation, run with `--no-instantiate`. The nixpkgs build for CppNix is
  `2.35.4`, not the pinned `2.36.0`.

## Cost on the fixture

| Run | CppNix | Lix |
|---|---|---|
| `nix-instantiate --version` | 29.4 ms | 24.7 ms |
| cold, `null` | 30.8 ms | 27.3 ms |
| cold, fixture | 33.8 ms | 28.5 ms |
| cold, fixture, 12 jobs | 192 evals/s | 214 evals/s |
| warm repl, fixture | 0.3 ms | 0.3 ms |
| warm repl, 12 workers | 14,829 evals/s | 13,642 evals/s |
| prefork child, fixture | 3.4 ms | no C API |
| nix-eval-jobs, 1 worker | 0.31 ms | 0.40 ms |

Cold cost is process startup, since `--version` costs as much as the whole
evaluation. Read-only mode, `dummy://` and the daemon store differ by under
2 ms.

## Cost at NixOS scale

A minimal `aarch64-linux` configuration through `nixos/lib/eval-config.nix`,
8 runs each, no heap cap.

| Forced value | CppNix cold | CppNix warm | Lix cold | Lix warm |
|---|---|---|---|---|
| three selected options | 576 ms | 292 ms | 485 ms | 361 ms |
| `system.build.toplevel.drvPath` | 1,101 ms | 984 ms | 976 ms | 1,084 ms |

The warm repl grew to 518 to 534 MiB after 8 selected-option evaluations and to
about 1,200 MiB after 8 `toplevel` evaluations. At this scale the warm gain is
under 2 times for selected options and none for `toplevel`.

## Failure classes

| Case | Cold CppNix | Cold Lix | Warm repl | Prefork (CppNix) | nix-eval-jobs |
|---|---|---|---|---|---|
| `throw`, `abort`, type check, infinite recursion | exit 1, class from `raw_msg` | same | caught, worker survives | caught | error per attribute |
| call depth past 10000 | exit 1 `max-call-depth` | exit 1 `max-call-depth` | caught | SIGSEGV | error per attribute |
| native stack, depth limit raised | SIGSEGV, empty stderr | SIGSEGV, empty stderr | worker dies | not run | not run |
| `deepSeq` on 1M-deep list | exit 1 `max-call-depth` | SIGSEGV | CppNix caught, Lix dies | `max-call-depth` | CppNix error, Lix worker SIGSEGV |
| 200M-element list | exit 1 `out of memory` | exit 1 `out of memory` | repl exits 1 | `std::bad_alloc` | `std::bad_alloc` |
| 10M chained attrsets | SIGABRT `Nix crashed`, `std::bad_alloc` | exit 1 `out of memory` | CppNix SIGABRT, Lix exits 1 | SIGABRT | CppNix worker SIGABRT, Lix `std::bad_alloc` |
| 5 s spin loop | killed at 5.0 to 5.15 s | same | killed, restarted | killed | no time limit, see below |

Cold runs separate every class by exit status or signal plus `raw_msg`.

- **Group kill.** `builtins.fetchGit` over ssh with a hanging ssh command gives
  `nix-instantiate`, `git`, `/bin/sh` and `sleep`. Killing the leader alone left
  three survivors in both evaluators, and `killpg` left none.
- **Warm repl.** RSS grows by about 125 KiB per fixture evaluation, from 29 to
  154 MiB (CppNix) and 32 to 166 MiB (Lix) over 1,000. `import` caches by path,
  so a rewritten file at the same path returned the old value. Repl errors carry
  `msg` but no `raw_msg`, and stdout keeps ANSI colour under `NO_COLOR=1`. The
  printed value text matched cold output for 75 of 75 edge-value candidates
  once whitespace was normalised.
- **Prefork.** Python reports the parent as multi-threaded at `fork()`. The
  call-depth case died with SIGSEGV in the child where the CLI exits 1, and the
  cause is not confirmed. Evaluating on a new thread with a larger stack failed
  with `Collecting from unknown thread`, since Boehm GC does not know the thread.
- **nix-eval-jobs.** It has no per-job timeout. The spin loop ran out of memory
  under the heap cap, while without it CppNix reached 4,449 MiB and was killed
  by the 4,096 MiB memory budget, and Lix still ran at 20 s. A
  `subprocess.run` timeout killed only the leader and left the `--worker`
  process running.

## Fixture paths and the store

Each row used a file with unique content and a fresh store root.

| Form | default (read-only) | `--read-write-mode` | `dummy://` | `--read-write-mode` + `dummy://?read-only=false` | `--read-write-mode` + `local?root=DIR` |
|---|---|---|---|---|---|
| path value | printed, no copy | same | same | same | same |
| `"${./a.txt}"`, `builtins.path` | store path computed, nothing written | written to host store | computed | CppNix in memory, Lix `addToStoreFromDump` unsupported | written under `DIR` only |
| `readFile "${./a.txt}"` | fails unless the path already exists in the host store | reads | fails | CppNix reads, Lix fails | reads |

Both evaluators compute the same store path. A per-run local store costs about
3 ms per evaluation and about 10 ms and 8 MB to create. Its root must not sit
under a symlink, and macOS `/var` is one.

## Not run

No x86_64 or Linux host was run, and no cgroup memory limit was tried.
