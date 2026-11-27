"""nix-eval-jobs as a batch evaluator: one attribute per candidate.

Each candidate is wrapped in a stub derivation whose name forces the
candidate deeply, since nix-eval-jobs reports only derivations.

    nix develop -c python3 -I prototype/harness/evaljobs.py
"""

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as h  # noqa: E402

PKGS = {
    "cppnix": "nix-eval-jobs",
    "lix": "lixPackageSets.latest.nix-eval-jobs",
}


def binary(attr):
    out = subprocess.run(
        ["nix", "build", "--no-link", "--print-out-paths", "--impure", "--expr",
         f"(import {h.NIXPKGS} {{ }}).{attr}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return out + "/bin/nix-eval-jobs"


def batch_expr(paths):
    attrs = "\n".join(
        f'  "{name}" = derivation {{ name = builtins.deepSeq (import {p}) "ok"; '
        f'system = builtins.currentSystem; builder = "/bin/sh"; }};'
        for name, p in paths
    )
    return "{\n" + attrs + "\n}"


def run(ev, paths, workers, timeout=120):
    expr_file = h.WORK / f"batch-{ev}.nix"
    expr_file.write_text(batch_expr(paths))
    argv = [binary(PKGS[ev]), "--no-instantiate", "--workers", str(workers),
            "--max-memory-size", "512", str(expr_file)]
    t0 = time.perf_counter()
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           env=h.base_env(), start_new_session=True)
        out, rc = p.stdout, p.returncode
    except subprocess.TimeoutExpired as e:
        out, rc = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or ""), "timeout"
    wall = time.perf_counter() - t0
    rows = {}
    for line in out.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        rows[obj.get("attr")] = obj
    return wall, rc, rows


def main():
    h.NIXPKGS = h.nixpkgs_path()
    ok = [(f"ok{i}", h.write_candidate(f"ej{i}", h.ok_definitions(i))) for i in range(600)]
    for ev in PKGS:
        print(f"{ev} nix-eval-jobs {binary(PKGS[ev]).split('-nix-eval-jobs-')[1].split('/')[0]}")
        for workers in (1, 12):
            wall, rc, rows = run(ev, ok, workers)
            good = sum(1 for r in rows.values() if "error" not in r)
            print(f"  {len(ok)} fixture candidates, workers {workers:2}: {wall*1000:7.0f} ms total, "
                  f"{wall*1000/len(ok):5.2f} ms each, {good} ok, rc {rc}")
        for case, (defs, opts) in h.FAILURE_CASES.items():
            if opts.get("options"):
                continue
            batch = [("before", ok[0][1]), (case, h.write_candidate("ej-" + case, defs)), ("after", ok[1][1])]
            wall, rc, rows = run(ev, batch, 1, timeout=30)
            def show(name):
                r = rows.get(name)
                if r is None:
                    return "missing"
                if "error" in r:
                    return "error:" + h.message_class(h.ANSI.sub("", r["error"]))
                return "success"
            print(f"  {case:20} {show(case):28} before={show('before')} after={show('after')} rc={rc} {wall:.2f}s")


if __name__ == "__main__":
    main()
