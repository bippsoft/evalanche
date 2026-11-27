"""Throwaway prototype for the evaluation harness question.

Compares a cold evaluator process per evaluation with a warm `nix repl`
worker that is reused only after a successful evaluation. Run it inside
`nix develop` so that EVALANCHE_CPPNIX_BIN and EVALANCHE_LIX_BIN point at
the pinned evaluators.

    python3 prototype/harness/harness.py cost
    python3 prototype/harness/harness.py failures
    python3 prototype/harness/harness.py groupkill
    python3 prototype/harness/harness.py store
    python3 prototype/harness/harness.py warm-memory
"""

import json
import os
import queue
import re
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
FIXTURE = HERE / "fixture" / "module.nix"
FILES = HERE / "fixture" / "files"

EVALUATORS = {
    "cppnix": os.environ.get("EVALANCHE_CPPNIX_BIN"),
    "lix": os.environ.get("EVALANCHE_LIX_BIN"),
}

HEAP = "256M"
TIMEOUT = 10.0
WORK = Path(tempfile.mkdtemp(prefix="evalanche-h12-")).resolve()


def nixpkgs_path():
    out = subprocess.run(
        [EVALUATORS["cppnix"] + "/nix", "flake", "archive", "--json", "--dry-run"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(out)["inputs"]["nixpkgs"]["path"]


NIXPKGS = None


def base_env(heap=HEAP):
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": os.environ["HOME"],
        "NIX_PATH": "",
        "NO_COLOR": "1",
        "TERM": "dumb",
    }
    if heap:
        env["GC_MAXIMUM_HEAP_SIZE"] = heap
    return env


# ---------------------------------------------------------------- candidates

def candidate_text(definitions, fixture=FIXTURE):
    """One evaluation: the fixture plus one generated module."""
    return f"""
let
  lib = import {NIXPKGS}/lib;
  cfg = (lib.evalModules {{
    modules = [ {fixture} ({definitions}) ];
  }}).config;
in
removeAttrs cfg [ "_module" ]
"""


def ok_definitions(i):
    return (
        "{ ... }: {"
        f' name = "svc{i}"; port = {1000 + i % 60000};'
        f' tags = [ "a" "b{i}" ];'
        f" users.u{i}.uid = {i}; users.root.uid = 0;"
        "}"
    )


DEEP = "builtins.foldl' (acc: _: [ acc ]) [ ] (builtins.genList (x: x) 1000000)"
SPIN = (
    "builtins.foldl' (a: i: builtins.foldl' (b: j: b + j) a"
    " (builtins.genList (x: x) 100000)) 0 (builtins.genList (x: x) 100000)"
)

FAILURE_CASES = {
    "success": ("{ ... }: { name = \"ok\"; }", {}),
    "throw": ("{ ... }: { name = throw \"planted\"; }", {}),
    "abort": ("{ ... }: { name = abort \"planted\"; }", {}),
    "type-check": ("{ ... }: { port = \"nope\"; }", {}),
    "infinite-recursion": ("{ config, ... }: { name = config.name; }", {}),
    "max-call-depth": (
        "{ ... }: { tags = let f = n: if n == 0 then [ ] else [ \"a\" ] ++ f (n - 1); in f 100000; }",
        {},
    ),
    "native-stack": (
        "{ ... }: { tags = let f = n: if n == 0 then [ ] else [ \"a\" ] ++ f (n - 1); in f 10000000; }",
        {"options": ["--option", "max-call-depth", "100000000"]},
    ),
    "deep-data": (
        f"{{ ... }}: {{ name = builtins.seq (builtins.deepSeq ({DEEP}) null) \"x\"; }}",
        {},
    ),
    "oom-one-allocation": (
        "{ ... }: { name = toString (builtins.length (builtins.genList (x: x) 200000000)); }",
        {},
    ),
    "oom-incremental": (
        "{ ... }: { name = builtins.seq (builtins.foldl' (acc: i: { inherit acc i; }) { }"
        " (builtins.genList (x: x) 10000000)) \"x\"; }",
        {},
    ),
    "timeout": (f"{{ ... }}: {{ port = {SPIN}; }}", {"timeout": 5.0}),
}


def write_candidate(name, definitions):
    path = WORK / f"{name}.nix"
    path.write_text(candidate_text(definitions))
    return path


# ---------------------------------------------------------------- outcomes

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def error_messages(stderr):
    """Level 0 messages from --log-format internal-json, ANSI removed."""
    out = []
    for line in stderr.splitlines():
        if not line.startswith("@nix "):
            continue
        try:
            obj = json.loads(line[5:])
        except ValueError:
            continue
        text = obj.get("raw_msg") or obj.get("msg", "")
        if obj.get("action") == "msg" and obj.get("level") == 0 and not text.startswith("trace: SENT-"):
            out.append(ANSI.sub("", obj.get("raw_msg") or obj.get("msg", "")))
    return out


def classify(timed_out, status, stderr):
    if timed_out:
        return "timeout"
    if os.WIFSIGNALED(status):
        sig = os.WTERMSIG(status)
        if sig == signal.SIGABRT and "crashed" in stderr:
            return "crash:SIGABRT"
        return "signal:" + signal.Signals(sig).name
    code = os.WEXITSTATUS(status)
    if code == 0:
        return "success"
    msgs = error_messages(stderr)
    text = msgs[-1] if msgs else stderr
    return "error:" + message_class(text)


def message_class(text):
    rules = [
        ("evaluation aborted", "abort"),
        ("infinite recursion encountered", "infinite-recursion"),
        ("max-call-depth exceeded", "max-call-depth"),
        ("out of memory", "out-of-memory"),
        ("is not of type", "type-check"),
        ("planted", "throw"),
        ("interrupted by the user", "interrupted"),
    ]
    for needle, cls in rules:
        if needle in text:
            return cls
    return "other:" + text.strip().splitlines()[-1][:80] if text.strip() else "other"


# ---------------------------------------------------------------- cold runner

class Result:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def run_cold(evaluator, args, env=None, timeout=TIMEOUT, kill_group=True):
    """Run one evaluator process in its own session and process group."""
    argv = [EVALUATORS[evaluator] + "/nix-instantiate", "--log-format", "internal-json"] + args
    env = env or base_env()
    t0 = time.perf_counter()
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=True,
    )
    pgid = proc.pid
    chunks = {"out": [], "err": []}

    def drain(stream, key):
        for data in iter(lambda: stream.read(65536), b""):
            chunks[key].append(data)

    readers = [
        threading.Thread(target=drain, args=(proc.stdout, "out")),
        threading.Thread(target=drain, args=(proc.stderr, "err")),
    ]
    for r in readers:
        r.start()
    timed_out = threading.Event()

    def on_timeout():
        timed_out.set()
        if kill_group:
            os.killpg(pgid, signal.SIGKILL)
        else:
            os.kill(proc.pid, signal.SIGKILL)

    timer = threading.Timer(timeout, on_timeout)
    timer.start()
    _, status, rusage = os.wait4(proc.pid, 0)
    wall = time.perf_counter() - t0
    timer.cancel()
    proc.returncode = 0
    survivors = group_alive(pgid)
    if survivors and kill_group:
        os.killpg(pgid, signal.SIGKILL)
    for r in readers:
        r.join(timeout=5)
    stderr = b"".join(chunks["err"]).decode(errors="replace")
    return Result(
        wall=wall,
        cpu=rusage.ru_utime + rusage.ru_stime,
        maxrss_mib=rusage.ru_maxrss / 2**20,  # bytes on darwin
        status=status,
        timed_out=timed_out.is_set(),
        cls=classify(timed_out.is_set(), status, stderr),
        stdout=b"".join(chunks["out"]).decode(errors="replace"),
        stderr=stderr,
        survivors=survivors,
        pgid=pgid,
    )


def group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return []
    except PermissionError:
        pass
    out = subprocess.run(["pgrep", "-g", str(pgid)], capture_output=True, text=True).stdout
    return [int(p) for p in out.split()]


# ---------------------------------------------------------------- warm runner

class WarmRepl:
    """A `nix repl` with lib loaded, driven over stdin.

    Each candidate is sent as `builtins.deepSeq (import FILE) "DONE-n"`,
    followed by a trace and a string sentinel so that both pipes can be
    read up to a known point.
    """

    def __init__(self, evaluator, heap=HEAP, options=()):
        self.evaluator = evaluator
        self.heap = heap
        self.options = list(options)
        self.n = 0
        self.start()

    def start(self):
        t0 = time.perf_counter()
        self.proc = subprocess.Popen(
            [EVALUATORS[self.evaluator] + "/nix", "repl", "--log-format", "internal-json"] + self.options,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=base_env(self.heap),
            start_new_session=True,
        )
        self.out = queue.Queue()
        self.err = queue.Queue()
        for stream, q in ((self.proc.stdout, self.out), (self.proc.stderr, self.err)):
            threading.Thread(target=self._pump, args=(stream, q), daemon=True).start()
        res = self.send(f"builtins.seq (import {NIXPKGS}/lib).evalModules \"DONE-{{n}}\"", TIMEOUT * 3)
        if res.cls != "success":
            raise RuntimeError(f"warm start failed: {res.cls} {res.stderr[-500:]}")
        self.startup = time.perf_counter() - t0

    @staticmethod
    def _pump(stream, q):
        for line in iter(stream.readline, b""):
            q.put(ANSI.sub("", line.decode(errors="replace")))
        q.put(None)

    def _wait_for(self, q, needle, deadline, sink):
        while True:
            left = deadline - time.perf_counter()
            if left <= 0:
                return "timeout"
            try:
                line = q.get(timeout=left)
            except queue.Empty:
                return "timeout"
            if line is None:
                return "eof"
            sink.append(line)
            if needle in line:
                return "found"

    def send(self, expr_template, timeout):
        self.n += 1
        n = self.n
        expr = expr_template.replace("{n}", str(n))
        payload = f'{expr}\nbuiltins.trace "SENT-{n}" null\n"SENT-{n}"\n'
        t0 = time.perf_counter()
        deadline = t0 + timeout
        out, err = [], []
        try:
            self.proc.stdin.write(payload.encode())
            self.proc.stdin.flush()
        except BrokenPipeError:
            pass
        r1 = self._wait_for(self.out, f"SENT-{n}", deadline, out)
        r2 = self._wait_for(self.err, f"SENT-{n}", deadline, err) if r1 == "found" else r1
        wall = time.perf_counter() - t0
        stdout, stderr = "".join(out), "".join(err)
        if r1 == "found" and r2 == "found":
            if f"DONE-{n}" in stdout:
                cls = "success"
            else:
                msgs = [m for m in error_messages(stderr)]
                cls = "error:" + message_class(msgs[-1] if msgs else stderr)
            return Result(wall=wall, cls=cls, stdout=stdout, stderr=stderr, alive=True)
        if r1 == "timeout" or r2 == "timeout":
            self.kill()
            return Result(wall=wall, cls="timeout", stdout=stdout, stderr=stderr, alive=False)
        status = self.reap()
        while True:
            try:
                line = self.err.get(timeout=2)
            except queue.Empty:
                break
            if line is None:
                break
            err.append(line)
        stderr = "".join(err)
        self.exit_status = status
        return Result(wall=wall, cls=classify(False, status, stderr), stdout=stdout, stderr=stderr, alive=False)

    def eval_file(self, path, timeout=TIMEOUT):
        return self.send(f'builtins.deepSeq (import {path}) "DONE-{{n}}"', timeout)

    def rss_mib(self):
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(self.proc.pid)], capture_output=True, text=True).stdout
        return int(out.strip()) / 1024 if out.strip() else 0.0

    def kill(self):
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.reap()

    def reap(self):
        _, status = os.waitpid(self.proc.pid, 0)
        self.proc.returncode = 0
        return status

    def close(self):
        if self.proc.returncode is None:
            self.kill()


# ---------------------------------------------------------------- commands

def summary(xs):
    xs = sorted(xs)
    p95 = xs[min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))]
    return f"mean {statistics.mean(xs)*1000:7.1f} ms  p50 {statistics.median(xs)*1000:7.1f}  p95 {p95*1000:7.1f}"


def cmd_cost(n=40):
    cands = [write_candidate(f"ok{i}", ok_definitions(i)) for i in range(n)]
    null_file = WORK / "null.nix"
    null_file.write_text("null")
    lib_file = WORK / "lib.nix"
    lib_file.write_text(f"builtins.seq (import {NIXPKGS}/lib).evalModules null")
    stores = {"daemon": [], "readonly": ["--readonly-mode"], "dummy": ["--store", "dummy://"]}
    print(f"host: {os.uname().machine}, n={n} per row, GC_MAXIMUM_HEAP_SIZE={HEAP}\n")
    print("cold, one nix-instantiate --eval --strict per evaluation")
    for ev in EVALUATORS:
        for sname, sargs in stores.items():
            for label, files in (("null", [null_file] * n), ("lib", [lib_file] * n), ("fixture", cands)):
                rs = [run_cold(ev, ["--eval", "--strict"] + sargs + [str(f)]) for f in files]
                bad = [r.cls for r in rs if r.cls != "success"]
                cpu = statistics.mean(r.cpu for r in rs) * 1000
                rss = max(r.maxrss_mib for r in rs)
                print(
                    f"  {ev:6} {sname:8} {label:8} {summary([r.wall for r in rs])}  cpu {cpu:6.1f} ms"
                    f"  maxrss {rss:5.0f} MiB" + (f"  FAIL {bad[:2]}" if bad else "")
                )
    print("\nwarm nix repl, lib loaded once, reused after success")
    for ev in EVALUATORS:
        w = WarmRepl(ev)
        rs = [w.eval_file(f) for f in cands]
        bad = [r.cls for r in rs if r.cls != "success"]
        print(f"  {ev:6} startup {w.startup*1000:6.1f} ms  per eval {summary([r.wall for r in rs])}"
              f"  rss after {w.rss_mib():5.0f} MiB" + (f"  FAIL {bad[:2]}" if bad else ""))
        w.close()
    print("\ncold throughput with parallel processes (daemon store, fixture)")
    for ev in EVALUATORS:
        for jobs in (1, 4, 8, 12):
            many = cands * 3
            t0 = time.perf_counter()
            with ThreadPoolExecutor(jobs) as ex:
                rs = list(ex.map(lambda f: run_cold(ev, ["--eval", "--strict", str(f)]), many))
            dt = time.perf_counter() - t0
            bad = sum(r.cls != "success" for r in rs)
            print(f"  {ev:6} jobs {jobs:2}  {len(many)/dt:6.1f} evals/s  {summary([r.wall for r in rs])}"
                  + (f"  FAIL {bad}" if bad else ""))


def cmd_failures(reps=1):
    print(f"GC_MAXIMUM_HEAP_SIZE={HEAP}, default timeout {TIMEOUT}s\n")
    print(f"{'case':20} {'evaluator':7} {'cold':30} {'warm repl':30}")
    for case, (defs, opts) in FAILURE_CASES.items():
        path = write_candidate(case, defs)
        timeout = opts.get("timeout", TIMEOUT)
        for ev in EVALUATORS:
            for _ in range(reps):
                c = run_cold(ev, ["--eval", "--strict"] + opts.get("options", []) + [str(path)], timeout=timeout)
                cold = f"{c.cls} {c.wall:.2f}s {c.maxrss_mib:.0f}MiB"
                if c.survivors:
                    cold += f" survivors={c.survivors}"
                w = WarmRepl(ev, options=opts.get("options", []))
                r = w.eval_file(path, timeout=timeout)
                warm = f"{r.cls} {r.wall:.2f}s alive={r.alive}"
                if not r.alive and r.cls != "timeout":
                    st = w.exit_status
                    code = f"sig{os.WTERMSIG(st)}" if os.WIFSIGNALED(st) else f"exit{os.WEXITSTATUS(st)}"
                    msgs = error_messages(r.stderr)
                    warm += f" {code} msg={(msgs[-1].strip().splitlines()[-1] if msgs else r.stderr.strip()[-80:])[:70]!r}"
                if r.alive:
                    after = w.eval_file(write_candidate("after", ok_definitions(1)))
                    warm += f" next={after.cls}"
                w.close()
                print(f"{case:20} {ev:7} {cold:30} {warm}")
                if c.cls.startswith("error:other") or c.cls.startswith("signal") or c.cls.startswith("crash"):
                    print(f"{'':20} {'':7} stderr tail: {c.stderr.strip()[-160:]!r}")


def cmd_groupkill():
    """fetchGit over ssh with a hanging ssh command gives nix -> git -> sleep."""
    path = WORK / "hang.nix"
    path.write_text('builtins.fetchGit { url = "ssh://git@example.invalid/x.git"; }')
    env = base_env()
    fake_ssh = WORK / "fake-ssh"
    fake_ssh.write_text("#!/bin/sh\nsleep 600\n")
    fake_ssh.chmod(0o755)
    env["GIT_SSH"] = str(fake_ssh)
    env["PATH"] = "/usr/bin:/bin:" + subprocess.run(
        ["sh", "-c", "dirname $(command -v git)"], capture_output=True, text=True
    ).stdout.strip()
    for ev in EVALUATORS:
        for kill_group in (False, True):
            r = run_cold(ev, ["--eval", "--strict", str(path)], env=env, timeout=3.0, kill_group=kill_group)
            tree = subprocess.run(["ps", "-o", "pid,ppid,pgid,comm", "-g", str(r.pgid)],
                                  capture_output=True, text=True).stdout.strip().splitlines()[1:]
            mode = "killpg" if kill_group else "kill leader"
            print(f"{ev:6} {mode:11} {r.cls:10} survivors {len(r.survivors)} {[' '.join(t.split()[3:]) for t in tree]}")
            if r.survivors:
                os.killpg(r.pgid, signal.SIGKILL)
            if r.cls != "timeout":
                print("   stderr:", r.stderr[-300:])


def cmd_store():
    """Each row gets a fixture file with unique content and a fresh local root,
    so no row can see a store path that an earlier row added."""
    import uuid
    forms = {
        "path value": "{{ ... }}: {{ source = {f}; }}",
        "interpolated": '{{ ... }}: {{ name = "${{{f}}}"; }}',
        "readFile interpolated": '{{ ... }}: {{ name = builtins.readFile "${{{f}}}"; }}',
        "builtins.path": '{{ ... }}: {{ name = builtins.path {{ path = {f}; name = "a"; }}; }}',
    }
    stores = {
        "daemon": lambda root: [],
        "daemon rw": lambda root: ["--read-write-mode"],
        "dummy": lambda root: ["--store", "dummy://"],
        "dummy rw": lambda root: ["--read-write-mode", "--store", "dummy://?read-only=false"],
        "local rw": lambda root: ["--read-write-mode", "--store", f"local?root={root}"],
    }
    for label, form in forms.items():
        print(label)
        for ev in EVALUATORS:
            for sname, sargs in stores.items():
                tag = uuid.uuid4().hex[:12]
                fdir = WORK / f"files-{tag}"
                fdir.mkdir()
                f = fdir / "a.txt"
                f.write_text(f"fixture {tag}\n")
                root = WORK / f"root-{tag}"
                path = write_candidate("store", form.format(f=f))
                r = run_cold(ev, ["--eval", "--strict"] + sargs(root) + [str(path)])
                shown = r.stdout.strip().replace("\n", " ")
                m = re.search(r"/nix/store/[a-z0-9]{32}-[^\" ;]+", shown)
                where = ""
                if m:
                    sp = m.group(0)
                    where = f" in-host-store={os.path.exists(sp)} in-local-root={(root / sp.lstrip('/')).exists()}"
                    shown = "store path"
                elif r.cls == "success":
                    shown = "value printed, no store path"
                else:
                    msgs = error_messages(r.stderr)
                    shown = (msgs[-1].strip().splitlines()[-1] if msgs else r.stderr[-120:])[:100]
                print(f"  {ev:6} {sname:10} {r.cls.split(':')[0]:8} {shown}{where}")


def cmd_warm_memory(n=1000):
    for ev in EVALUATORS:
        w = WarmRepl(ev, heap=None)
        print(f"{ev:6} after startup rss {w.rss_mib():6.0f} MiB")
        times = []
        for i in range(n):
            r = w.eval_file(write_candidate(f"m{i}", ok_definitions(i)))
            times.append(r.wall)
            if r.cls != "success":
                print("  failed", r.cls)
                break
            if (i + 1) % 250 == 0:
                print(f"{ev:6} after {i+1:4} evals rss {w.rss_mib():6.0f} MiB  last 250 {summary(times[-250:])}")
        w.close()

EDGE_STRINGS = ["", " ", "\n", "\t", "${", "''", "\\", '"', "é", "\U0001F600", "-x", "true", "null", "0", "a" * 300]
EDGE_INTS = [0, 1, 65535, 80, 443]


def nix_string(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("${", "\\${").replace("\n", "\\n").replace("\t", "\\t") + '"'


def compare_candidates():
    out = []
    for i, s in enumerate(EDGE_STRINGS):
        for j, p in enumerate(EDGE_INTS):
            tags = " ".join(nix_string(t) for t in EDGE_STRINGS[: (i + j) % 6])
            out.append(
                f"{{ ... }}: {{ name = {nix_string(s)}; port = {p}; tags = [ {tags} ]; "
                f"users.{nix_string(s or 'e')}.uid = {i * 7 + j}; source = {FILES}/a.txt; }}"
            )
    return out


def cmd_compare():
    """Printed value text, cold `--eval --strict` against warm `:p`."""
    cands = [write_candidate(f"cmp{i}", d) for i, d in enumerate(compare_candidates())]
    for ev in EVALUATORS:
        w = WarmRepl(ev)
        same = differ = 0
        example = None
        for c in cands:
            cold = run_cold(ev, ["--eval", "--strict", str(c)]).stdout
            r = w.send(f":p import {c}", TIMEOUT)
            lines = [l for l in r.stdout.splitlines() if f"SENT-{w.n}" not in l and l.strip()]
            if lines and lines[-1].strip() == "null":  # value of the trace sentinel
                lines.pop()
            warm = "\n".join(lines)
            norm = lambda t: " ".join(t.split())
            if norm(cold) == norm(warm):
                same += 1
            else:
                differ += 1
                a, b = norm(cold), norm(warm)
                k = next((x for x in range(min(len(a), len(b))) if a[x] != b[x]), min(len(a), len(b)))
                example = example or (a[max(0, k - 60):k + 80], b[max(0, k - 60):k + 80])
        w.close()
        print(f"{ev:6} {len(cands)} candidates, whitespace-normalised text equal {same}, different {differ}")
        if example:
            print("  cold:", example[0])
            print("  warm:", example[1])


def nixos_text(i, force):
    return f"""
let
  cfg = (import {NIXPKGS}/nixos/lib/eval-config.nix {{
    system = "aarch64-linux";
    modules = [ {{
      boot.isContainer = true;
      system.stateVersion = "26.05";
      networking.hostName = "host{i}";
      networking.firewall.allowedTCPPorts = [ {1000 + i} ];
      services.openssh.enable = true;
    }} ];
  }}).config;
in
{force}
"""


NIXOS_FORCE = {
    "selected options": "{ inherit (cfg.networking) hostName; ports = cfg.networking.firewall.allowedTCPPorts; ssh = cfg.services.openssh.settings; }",
    "toplevel.drvPath": "cfg.system.build.toplevel.drvPath",
}


def cmd_nixos(n=8):
    """M2 scale: a minimal NixOS configuration, cold and warm."""
    for label, force in NIXOS_FORCE.items():
        cands = []
        for i in range(n):
            p = WORK / f"nixos-{label[:3]}-{i}.nix"
            p.write_text(nixos_text(i, force))
            cands.append(p)
        for ev in EVALUATORS:
            rs = [run_cold(ev, ["--eval", "--strict", str(c)], env=base_env(None), timeout=120) for c in cands]
            bad = [r.cls for r in rs if r.cls != "success"]
            print(f"{label:17} {ev:6} cold  {summary([r.wall for r in rs])}  maxrss {max(r.maxrss_mib for r in rs):5.0f} MiB"
                  + (f"  FAIL {bad[:1]} {rs[0].stderr[-300:]!r}" if bad else ""))
            w = WarmRepl(ev, heap=None)
            ws = [w.eval_file(c, timeout=120) for c in cands]
            bad = [r.cls for r in ws if r.cls != "success"]
            print(f"{label:17} {ev:6} warm  {summary([r.wall for r in ws])}  first {ws[0].wall*1000:.0f} ms"
                  f"  rss after {w.rss_mib():5.0f} MiB" + (f"  FAIL {bad[:1]}" if bad else ""))
            w.close()


if __name__ == "__main__":
    missing = [k for k, v in EVALUATORS.items() if not v]
    if missing:
        sys.exit(f"run inside `nix develop`, missing bin for {missing}")
    NIXPKGS = nixpkgs_path()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "cost"
    {"cost": cmd_cost, "failures": cmd_failures, "groupkill": cmd_groupkill,
     "store": cmd_store, "warm-memory": cmd_warm_memory, "compare": cmd_compare, "nixos": cmd_nixos}[cmd]()
