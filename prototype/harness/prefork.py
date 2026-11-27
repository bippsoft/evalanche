"""Prefork test through the CppNix C API. Lix has no C API.

The parent loads lib once and forks one child per evaluation. Each child
evaluates its candidate, writes the outcome and calls _exit, so no state
returns to the parent.

    nix develop -c python3 -I prototype/harness/prefork.py
"""

import ctypes
import os
import re
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as h  # noqa: E402


def c_api_libs():
    dev = subprocess.run(
        ["nix", "build", "--no-link", "--print-out-paths", ".#cppnix.dev"],
        cwd=h.ROOT, capture_output=True, text=True, check=True,
    ).stdout.strip()
    libs = {}
    for name in ("nix-util-c", "nix-store-c", "nix-expr-c"):
        pc = Path(dev, "lib", "pkgconfig", name + ".pc").read_text()
        prefix = re.search(r"^prefix=(.*)$", pc, re.M).group(1)
        libs[name] = ctypes.CDLL(f"{prefix}/lib/lib{name.replace('-', '')}.dylib", mode=ctypes.RTLD_GLOBAL)
    return libs


CB = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_uint, ctypes.c_void_p)


class Evaluator:
    def __init__(self, store_uri=b"dummy://"):
        libs = c_api_libs()
        u, s, e = libs["nix-util-c"], libs["nix-store-c"], libs["nix-expr-c"]
        for fn, res in (
            (u.nix_c_context_create, ctypes.c_void_p),
            (s.nix_store_open, ctypes.c_void_p),
            (e.nix_state_create, ctypes.c_void_p),
            (e.nix_alloc_value, ctypes.c_void_p),
        ):
            fn.restype = res
        s.nix_store_open.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p]
        e.nix_state_create.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        e.nix_alloc_value.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        e.nix_expr_eval_from_string.argtypes = [ctypes.c_void_p] * 2 + [ctypes.c_char_p] * 2 + [ctypes.c_void_p]
        e.nix_value_force_deep.argtypes = [ctypes.c_void_p] * 3
        u.nix_err_info_msg.argtypes = [ctypes.c_void_p, ctypes.c_void_p, CB, ctypes.c_void_p]
        u.nix_err_msg.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        u.nix_err_msg.restype = ctypes.c_char_p
        self.u, self.e = u, e
        self.ctx = u.nix_c_context_create()
        for init in (u.nix_libutil_init, s.nix_libstore_init, e.nix_libexpr_init):
            init.argtypes = [ctypes.c_void_p]
            self.check(init(self.ctx))
        self.store = s.nix_store_open(self.ctx, store_uri, None)
        self.state = e.nix_state_create(self.ctx, None, self.store)

    def check(self, err):
        if err != 0:
            raise RuntimeError(self.message())

    def message(self):
        return (self.u.nix_err_msg(None, self.ctx, None) or b"").decode(errors="replace")

    def eval(self, expr):
        v = self.e.nix_alloc_value(self.ctx, self.state)
        err = self.e.nix_expr_eval_from_string(self.ctx, self.state, expr.encode(), b"/", v)
        if err == 0:
            err = self.e.nix_value_force_deep(self.ctx, self.state, v)
        return err, (self.message() if err else "")


BIG_STACK = False


def forked(ev, expr, timeout=h.TIMEOUT):
    r, w = os.pipe()
    t0 = time.perf_counter()
    pid = os.fork()
    if pid == 0:
        os.setpgid(0, 0)
        os.close(r)
        if BIG_STACK:
            box = []
            h.threading.stack_size(512 * 2**20)
            t = h.threading.Thread(target=lambda: box.append(ev.eval(expr)))
            t.start()
            t.join()
            err, msg = box[0] if box else (1, "thread died")
        else:
            err, msg = ev.eval(expr)
        os.write(w, msg.encode()[:65536])
        os._exit(0 if err == 0 else 1)
    os.close(w)
    try:
        os.setpgid(pid, pid)
    except OSError:
        pass
    timer = h.threading.Timer(timeout, lambda: os.killpg(pid, signal.SIGKILL))
    timer.start()
    msg = b""
    for chunk in iter(lambda: os.read(r, 65536), b""):
        msg += chunk
    _, status, ru = os.wait4(pid, 0)
    timer.cancel()
    os.close(r)
    wall = time.perf_counter() - t0
    timed_out = os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
    if timed_out:
        cls = "timeout"
    elif os.WIFSIGNALED(status):
        cls = "signal:" + signal.Signals(os.WTERMSIG(status)).name
    elif os.WEXITSTATUS(status) == 0:
        cls = "success"
    else:
        cls = "error:" + h.message_class(msg.decode(errors="replace"))
    return h.Result(wall=wall, cls=cls, msg=msg.decode(errors="replace"), maxrss_mib=ru.ru_maxrss / 2**20)


def main():
    os.environ["GC_MAXIMUM_HEAP_SIZE"] = h.HEAP
    h.NIXPKGS = h.nixpkgs_path()
    global BIG_STACK
    BIG_STACK = "--big-stack" in sys.argv
    ev = Evaluator.__new__(Evaluator)
    libs = c_api_libs()
    t0 = time.perf_counter()
    ev = Evaluator()
    err, msg = ev.eval(f"builtins.seq (import {h.NIXPKGS}/lib).evalModules null")
    assert err == 0, msg
    print(f"cppnix C API, store dummy://, big stack child thread {BIG_STACK}, parent init with lib {1000 * (time.perf_counter() - t0):.1f} ms")
    cands = [h.write_candidate(f"pf{i}", h.ok_definitions(i)) for i in range(200)]
    rs = [forked(ev, f'builtins.deepSeq (import {c}) "ok"') for c in cands]
    bad = [r.cls for r in rs if r.cls != "success"]
    print(f"forked child per evaluation, n={len(rs)}  {h.summary([r.wall for r in rs])}" + (f"  FAIL {bad[:3]}" if bad else ""))
    for case, (defs, opts) in h.FAILURE_CASES.items():
        if opts.get("options"):
            print(f"  {case:20} skipped, needs max-call-depth raised in the parent")
            continue
        path = h.write_candidate("pf-" + case, defs)
        r = forked(ev, f'builtins.deepSeq (import {path}) "ok"', timeout=opts.get("timeout", h.TIMEOUT))
        print(f"  {case:20} {r.cls:28} {r.wall:.2f}s")
    after = forked(ev, f'builtins.deepSeq (import {cands[0]}) "ok"')
    print(f"  parent still usable after all cases: {after.cls}")


if __name__ == "__main__":
    main()
