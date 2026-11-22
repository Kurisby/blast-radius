"""Run selected sqlglot tests against a patched tree, in forked children.

The parent imports the heavy third-party modules once; each commit runs in a fork that loads
sqlglot fresh with the patched files served from an overlay directory under /tmp.
"""
from __future__ import annotations

import json
import os
import select
import shutil
import signal
import sys
import time

APP = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(APP, "hook")
ROOT = os.environ.get("BR_SQLGLOT", "/sqlglot")
TMP = os.environ.get("BR_TMP", "/tmp")
GC_FREEZE = os.environ.get("BR_GC_FREEZE", "1") == "1"
GC_THRESHOLD = int(os.environ.get("BR_GC_THRESHOLD", "0"))

if HOOK not in sys.path:
    sys.path.insert(0, HOOK)


def log(*a):
    print("[runner]", *a, file=sys.stderr, flush=True)


def prepare_parent(heavy: bool) -> None:
    os.environ["SKIP_INTEGRATION"] = "0"
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    import unittest  # noqa: F401
    import concurrent.futures  # noqa: F401
    import multiprocessing  # noqa: F401
    if heavy:
        try:
            import duckdb  # noqa: F401
            import numpy  # noqa: F401
            import pandas  # noqa: F401
            import pandas.testing  # noqa: F401
        except Exception as e:  # pragma: no cover
            log("preimport failed", repr(e))
        cap_duckdb_threads()


def cap_duckdb_threads() -> None:
    """One DuckDB worker thread per connection: the container has two cores and every
    thread's CPU is billed."""
    try:
        import duckdb
    except Exception:  # noqa: BLE001
        return
    if getattr(duckdb.connect, "_br_capped", False):
        return
    orig = duckdb.connect

    def connect(database=":memory:", read_only=False, config=None, *a, **k):
        cfg = dict(config or {})
        cfg.setdefault("threads", 1)
        try:
            return orig(database, read_only, cfg, *a, **k)
        except Exception:  # noqa: BLE001
            return orig(database, read_only, config, *a, **k)

    connect._br_capped = True
    duckdb.connect = connect


# Modules a forked child can inherit already imported, when its patch touches none of them.
WARM_LIGHT = ("sqlglot", "sqlglot.optimizer", "sqlglot.executor", "tests.helpers")
WARM_HEAVY = WARM_LIGHT + ("tests.test_optimizer",)


def warm_import(heavy: bool) -> None:
    import importlib

    os.environ["SKIP_INTEGRATION"] = "0"
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    cwd = os.getcwd()
    os.chdir(ROOT)
    # nothing printed while importing may reach the answer stream
    sys.stdout.flush()
    saved_fd = os.dup(1)
    saved_out = sys.stdout
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    try:
        for m in (WARM_HEAVY if heavy else WARM_LIGHT):
            importlib.import_module(m)
    finally:
        try:
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass
        os.dup2(saved_fd, 1)
        os.close(saved_fd)
        sys.stdout = saved_out
        os.chdir(cwd)


def loaded_project_modules() -> set:
    return {m for m in sys.modules if m.split(".")[0] in ("sqlglot", "tests")}


def dump_warm_modules(path: str) -> None:
    """Build step: record which modules warm_import() loads (unpatched pinned commit)."""
    import json
    import subprocess

    res = {}
    for kind, heavy in (("light", False), ("heavy", True)):
        code = ("import sys, json; sys.path.insert(0, %r); import runner; runner.prepare_parent(heavy=%r); "
                "runner.warm_import(%r); print(json.dumps(sorted(runner.loaded_project_modules())))"
                % (APP, heavy, heavy))
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True)
        res[kind] = json.loads(out.stdout.strip().splitlines()[-1])
    with open(path, "w") as fh:
        json.dump(res, fh)


_PKG_LINES_CODE = r"""
import importlib, json, os, pkgutil, sys
root = os.getcwd() + "/"
src = root + "sqlglot/"
mon = sys.monitoring
seen = set()
def on_line(code, line):
    # function bodies only (CO_OPTIMIZED): a module or class body evaluating a lambda or a
    # def on some line does not run that function
    fn = code.co_filename
    if fn.startswith(src):
        if code.co_flags & 1:
            seen.add((fn[len(root):], line))
        return mon.DISABLE
mon.use_tool_id(3, "pkglines")
mon.register_callback(3, mon.events.LINE, on_line)
mon.set_events(3, mon.events.LINE)
import sqlglot
for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    if m.name.endswith("__main__"):
        continue
    try:
        importlib.import_module(m.name)
    except Exception:
        pass
mon.set_events(3, 0)
out = {}
for f, l in seen:
    out.setdefault(f, []).append(l)
json.dump({f: sorted(v) for f, v in out.items()}, open(sys.argv[1], "w"))
"""


_WARM_LINES_CODE = r"""
import json, os, sys
root = os.getcwd() + "/"
src = root + "sqlglot/"
mon = sys.monitoring
seen = set()
def on_line(code, line):
    fn = code.co_filename
    if fn.startswith(src):
        if code.co_flags & 1:
            seen.add((fn[len(root):], line))
        return mon.DISABLE
sys.path.insert(0, os.environ["BR_APP"])
import runner
mon.use_tool_id(3, "warmlines")
mon.register_callback(3, mon.events.LINE, on_line)
mon.set_events(3, mon.events.LINE)
runner.prepare_parent(heavy=True)
runner.warm_import(True)
runner.preload_executor_data()
mon.set_events(3, 0)
out = {}
for f, l in seen:
    out.setdefault(f, []).append(l)
json.dump({f: sorted(v) for f, v in out.items()}, open(sys.argv[1], "w"))
"""


def dump_warm_lines(path: str) -> None:
    """Build step: sqlglot function lines that run in the warm parent before it forks (the
    warm import, heavy flavour, plus the executor data preload)."""
    import subprocess

    subprocess.run([sys.executable, "-c", _WARM_LINES_CODE, os.path.abspath(path)], cwd=ROOT, check=True,
                   env=dict(os.environ, BR_APP=APP), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# -- hot patching ---------------------------------------------------------------------------
# A patch that only changes function bodies, on lines that never ran while the warm parent
# imported sqlglot, leaves the import-time state exactly as the pinned commit's. A child forked
# from the warm parent then only needs the changed functions' code swapped in, instead of
# importing sqlglot again.

_CO_OPTIMIZED = 0x0001
_BODY_ATTRS = ("co_code", "co_names", "co_varnames", "co_freevars", "co_cellvars", "co_flags", "co_argcount",
               "co_posonlyargcount", "co_kwonlyargcount", "co_name", "co_qualname", "co_exceptiontable",
               "co_nlocals", "co_stacksize")


def _code_list(code, out):
    import types

    out.append(code)
    for c in code.co_consts:
        if isinstance(c, types.CodeType):
            _code_list(c, out)
    return out


def _const_key(c):
    import types

    if isinstance(c, types.CodeType):
        return ("<code>",)
    if isinstance(c, tuple):
        return (tuple, tuple(_const_key(x) for x in c))
    if isinstance(c, frozenset):
        return (frozenset, tuple(sorted(repr(_const_key(x)) for x in c)))
    if isinstance(c, (float, complex)):
        return (type(c).__name__, repr(c))
    return (type(c).__name__, c)


def _same_body(a, b) -> bool:
    """Same behaviour apart from nested code objects and line numbers."""
    return all(getattr(a, k) == getattr(b, k) for k in _BODY_ATTRS) and _const_key(a.co_consts) == _const_key(b.co_consts)


def _changed_old_lines(old_src: str, new_src: str) -> set:
    """Old line numbers a patch replaces or deletes; an insertion counts both neighbours."""
    import difflib

    a = old_src.splitlines()
    b = new_src.splitlines()
    pre = 0
    while pre < len(a) and pre < len(b) and a[pre] == b[pre]:
        pre += 1
    suf = 0
    while suf < len(a) - pre and suf < len(b) - pre and a[len(a) - 1 - suf] == b[len(b) - 1 - suf]:
        suf += 1
    sm = difflib.SequenceMatcher(None, a[pre:len(a) - suf], b[pre:len(b) - suf], autojunk=False)
    res = set()
    for tag, i1, i2, _, _ in sm.get_opcodes():
        if tag == "equal":
            continue
        if i2 > i1:
            res.update(range(pre + i1 + 1, pre + i2 + 1))
        else:
            res.update((pre + i1, pre + i1 + 1))
    return res


_OLD_CODES: dict = {}


def hotpatch_prepare(path: str, old_src: str, new_src: str, import_lines: set):
    """[(qualname, firstlineno, old_code, new_code)] that turn the imported module at `path`
    into the patched one, or None when the patch changes anything but function bodies or a
    changed line ran during the warm import."""
    if _changed_old_lines(old_src, new_src) & set(import_lines or ()):
        return None
    try:
        old = _OLD_CODES.get(path)
        if old is None or old[0] is not old_src:
            old = _OLD_CODES[path] = (old_src, _code_list(compile(old_src, path, "exec", dont_inherit=True), []))
        old = old[1]
        new = _code_list(compile(new_src, path, "exec", dont_inherit=True), [])
    except (SyntaxError, ValueError):
        return None
    if len(old) != len(new):
        return None
    swaps = []
    for i, (a, b) in enumerate(zip(old, new)):
        if a.co_qualname != b.co_qualname or a.co_freevars != b.co_freevars:
            return None
        if not _same_body(a, b) and (i == 0 or not (a.co_flags & _CO_OPTIMIZED)):
            return None  # module or class body
        if a != b:
            swaps.append((a.co_qualname, a.co_firstlineno, a, b))
    return swaps


def apply_hotpatch(patches: dict) -> int:
    """Swap the patched code into the live functions; raises when a changed function that
    must exist at import time (not nested in another function) was not found."""
    import gc
    import types

    want = {}
    need = set()
    for path, swaps in patches.items():
        for qn, fl, a, b in swaps:
            want.setdefault((path, qn, fl), []).append((a, b))
            # a function that is not nested in another one was created at import time: it
            # must be found (a change nested deeper also changes its enclosing function)
            if "<locals>" not in qn and a.co_flags & _CO_OPTIMIZED:
                need.add((path, qn, fl))
    done = set()
    n = 0
    frozen = gc.get_freeze_count()
    if frozen:
        gc.unfreeze()  # get_objects() does not list frozen objects
    try:
        objs = gc.get_objects()
    finally:
        if frozen:
            gc.freeze()
    for f in objs:
        if type(f) is not types.FunctionType:
            continue
        c = f.__code__
        k = (c.co_filename, c.co_qualname, c.co_firstlineno)
        lst = want.get(k)
        if not lst:
            continue
        for a, b in lst:
            if c == a:
                f.__code__ = b
                done.add(k)
                n += 1
                break
    del objs
    missing = need - done
    if missing:
        raise RuntimeError(f"hotpatch: no live function for {sorted(missing)[:3]}")
    return n


def dump_pkg_import_lines(path: str) -> None:
    """Build step: lines of sqlglot that run while every sqlglot module is imported (pinned
    commit). Profiled coverage misses most of them: the package was imported before line
    tracing started, and dialect modules are imported lazily inside whichever test came first."""
    import subprocess

    subprocess.run([sys.executable, "-c", _PKG_LINES_CODE, path], cwd=ROOT, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


_STATEFP_CODE = r"""
import os, sys
sys.path.insert(0, os.environ["BR_HOOK"])
import br_overlay
if os.environ.get("BR_OVERLAY"):
    br_overlay.install(os.environ["BR_ROOT"], os.environ["BR_OVERLAY"])
sys.path.insert(0, os.environ["BR_ROOT"])
sys.path.insert(0, os.environ["BR_APP"])
sys.argv = ["statefp"] + sys.argv[1:]
import statefp
statefp.main()
"""


def _statefp_env(overlay):
    env = dict(os.environ, PYTHONHASHSEED="0", BR_HOOK=HOOK, BR_ROOT=ROOT, BR_APP=APP, SKIP_INTEGRATION="0")
    env.pop("PYTHONPATH", None)
    if overlay:
        env["BR_OVERLAY"] = overlay
    else:
        env.pop("BR_OVERLAY", None)
    return env


def dump_state_fp(path: str) -> None:
    """Build step: import-time state fingerprint of the pinned commit, taken twice so that
    entries that differ between runs are known."""
    import json
    import subprocess

    path = os.path.abspath(path)
    outs = []
    for i in range(2):
        tmp = f"{path}.{i}"
        subprocess.run([sys.executable, "-c", _STATEFP_CODE, tmp], cwd=ROOT, env=_statefp_env(None),
                       check=True, stdout=subprocess.DEVNULL)
        with open(tmp) as fh:
            outs.append(json.load(fh))
        os.unlink(tmp)
    a, b = outs[0]["fp"], outs[1]["fp"]
    unstable = sorted(p for p in set(a) | set(b) if a.get(p) != b.get(p))
    with open(path, "w") as fh:
        json.dump({"errors": outs[0]["errors"], "fp": a, "unstable": unstable}, fh, separators=(",", ":"))


_STATEFP_SERVER_CODE = r"""
import os, sys
sys.path.insert(0, os.environ["BR_HOOK"])
sys.path.insert(0, os.environ["BR_ROOT"])
sys.path.insert(0, os.environ["BR_APP"])
import statefp
statefp.serve(sys.argv[1], os.environ["BR_ROOT"])
"""

FP_SERVER = os.environ.get("BR_FPSERVER", "1") == "1"
FP_CHECK = os.environ.get("BR_STATEFP_CHECK") == "1"  # local test: compare with a fresh process
_FP = [None]  # the fork server process, or False once it is unusable


def fp_server_stop(kill: bool = False) -> None:
    s = _FP[0]
    if not s:
        return
    _FP[0] = False
    try:
        if kill:
            os.killpg(s.pid, signal.SIGKILL)
        else:
            s.stdin.close()
        s.wait(timeout=5)
    except Exception:  # noqa: BLE001
        try:
            os.killpg(s.pid, signal.SIGKILL)
        except Exception:  # noqa: BLE001
            pass


def _fp_server_diff(d: str, out: str, orig: str, timeout: float):
    """State diff through the fork server: the result dict, or None (then a fresh process)."""
    import json
    import subprocess

    if _FP[0] is False:
        return None
    if _FP[0] is None:
        try:
            _FP[0] = subprocess.Popen([sys.executable, "-c", _STATEFP_SERVER_CODE, orig], cwd=ROOT,
                                      env=_statefp_env(None), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, text=True, start_new_session=True)
            import atexit

            atexit.register(fp_server_stop)
        except Exception as e:  # noqa: BLE001
            log("fp server failed to start", repr(e))
            _FP[0] = False
            return None
    s = _FP[0]
    try:
        s.stdin.write(json.dumps({"overlay": d, "out": out}) + "\n")
        s.stdin.flush()
        r, _, _ = select.select([s.stdout], [], [], timeout + 2.0)
        reply = s.stdout.readline().strip() if r else "timeout"
    except Exception as e:  # noqa: BLE001
        reply = f"error {e!r}"
    if reply != "ok":
        log("fp server", reply)
        if not reply.startswith("fail"):
            fp_server_stop(kill=True)
            _FP[0] = False
        return None
    with open(out) as fh:
        return json.load(fh)


def state_diff(key: str, files: dict, timeout: float = 8.0):
    """Import-time state of the tree with `files` patched in, compared with the pinned
    commit's: {"diff": [(path, kind, info)], "errors": [...], ...} or None."""
    import json
    import subprocess

    orig = os.path.join(APP, "state_fp.json")
    if not os.path.exists(orig):
        return None
    d = write_overlay("fp-" + key, files)
    out = os.path.join(TMP, "ov", f"fp-{key}.json")
    try:
        res = _fp_server_diff(d, out, orig, timeout) if FP_SERVER else None
        if res is not None and not FP_CHECK:
            return res
        subprocess.run([sys.executable, "-c", _STATEFP_CODE, out, orig], cwd=ROOT, env=_statefp_env(d),
                       timeout=timeout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        with open(out) as fh:
            ref = json.load(fh)
        if FP_CHECK:
            same = res is not None and res["diff"] == ref["diff"] and sorted(res["errors"]) == sorted(ref["errors"])
            log("fp check", key, "same" if same else "DIFFERENT" if res is not None else "no server result",
                "" if same or res is None else (res["diff"][:3], ref["diff"][:3], res["errors"][:2], ref["errors"][:2]))
        return ref
    except Exception as e:  # noqa: BLE001
        log("state diff failed", key, repr(e))
        return None
    finally:
        shutil.rmtree(d, ignore_errors=True)
        try:
            os.unlink(out)
        except OSError:
            pass


class _Timeout(BaseException):
    pass


class _Abort(BaseException):
    """Raised to stop a test at its first failure: its outcome is already known."""


LAZY_WAIT = []
POOL_SWAP = []  # [original ProcessPoolExecutor, SyncPool]


def install_sync_pool():
    """Replace ProcessPoolExecutor with an in-process executor that still pickles arguments
    and results, so tests behave as with a pool minus the process start-up cost."""
    import concurrent.futures as cf
    import pickle

    class LazyFuture(cf.Future):
        """Computed when its result is first asked for, so a test that stops at its first
        failure never pays for the remaining items."""

        def __init__(self, thunk):
            super().__init__()
            self._thunk = thunk

        def _compute(self):
            if self._thunk is None:
                return
            thunk, self._thunk = self._thunk, None
            try:
                r = thunk()
            except _Timeout:
                raise
            except _Abort:
                raise
            except BaseException as e:  # noqa: BLE001
                self.set_exception(e)
            else:
                self.set_result(r)

        def result(self, timeout=None):
            self._compute()
            return super().result(timeout)

        def exception(self, timeout=None):
            self._compute()
            return super().exception(timeout)

        def done(self):
            self._compute()
            return super().done()

    class SyncPool:
        def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(),
                     **kwargs):
            if initializer is not None:
                initializer(*initargs)

        def submit(self, fn, /, *args, **kwargs):
            payload = pickle.dumps((args, kwargs))

            def thunk():
                a, k = pickle.loads(payload)
                return pickle.loads(pickle.dumps(fn(*a, **k)))

            return LazyFuture(thunk)

        def map(self, fn, *iterables, timeout=None, chunksize=1):
            return [self.submit(fn, *a).result() for a in zip(*iterables)]

        def shutdown(self, wait=True, *, cancel_futures=False):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.shutdown()
            return False

    def wait(fs, timeout=None, return_when=cf.ALL_COMPLETED):
        return cf._base.DoneAndNotDoneFutures(set(fs), set())

    def as_completed(fs, timeout=None):
        return iter(list(fs))

    cf.wait = wait
    cf.as_completed = as_completed
    LAZY_WAIT[:] = [wait, as_completed]

    POOL_SWAP[:] = [cf.ProcessPoolExecutor, SyncPool]
    cf.ProcessPoolExecutor = SyncPool
    import concurrent.futures.process as cfp
    cfp.ProcessPoolExecutor = SyncPool


def _child(wfd: int, overlay: str, tests: list[tuple[str, float]], extra: dict) -> None:
    # nothing a test prints may reach the real stdout (it would corrupt the answer lines)
    try:
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    out = os.fdopen(wfd, "w", buffering=1)

    def emit(kind, tid):
        out.write(f"{kind} {tid}\n")
        t = time.process_time()
        out.write(f"NOTE t {tid} {t - _last[0]:.3f}\n")
        _last[0] = t

    _last = [time.process_time()]

    try:
        if GC_THRESHOLD > 0:
            import gc

            gc.set_threshold(GC_THRESHOLD, *gc.get_threshold()[1:])
        os.setpgid(0, 0)
        try:
            with open("/proc/self/oom_score_adj", "w") as fh:
                fh.write("1000")  # if memory runs out, this child goes first, not the parent
        except OSError:
            pass
        os.environ["BR_OVERLAY"] = overlay
        os.environ["BR_ROOT"] = ROOT
        pp = os.environ.get("PYTHONPATH", "")
        os.environ["PYTHONPATH"] = HOOK + os.pathsep + ROOT + (os.pathsep + pp if pp else "")
        os.chdir(ROOT)
        import br_overlay

        br_overlay.install(ROOT, overlay)
        if extra.get("hotpatch"):
            try:
                out.write(f"NOTE hotpatch {apply_hotpatch(extra['hotpatch'])}\n")
            except BaseException as e:  # noqa: BLE001
                # never run tests on a half-patched tree: report every test failing
                out.write(f"NOTE hotpatch-failed {e!r}\n")
                for tid, *_ in tests:
                    emit("FAIL", tid)
                out.write("END\n")
                out.flush()
                os._exit(0)

        install_sync_pool()

        import unittest

        by_file: dict[str, list[tuple[str, str, str, float]]] = {}
        spec_of = {}
        for tid, limit, spec in tests:
            path, cls, meth = tid.split("::")
            by_file.setdefault(path, []).append((tid, cls, meth, limit))
            spec_of[tid] = spec or {}
        hooks = TestHooks(spec_of, emit, out)
        hooks.install_loader_filters()

        def order(path):
            return (path == "tests/test_executor.py", path)

        by_file = {k: sorted(v, key=lambda it: (bool((spec_of[it[0]] or {}).get("tpcds") is not None or (spec_of[it[0]] or {}).get("gate")), it[0]))
                   for k, v in sorted(by_file.items(), key=lambda kv: order(kv[0]))}
        suite = unittest.TestSuite()
        id_of = {}
        limit_of = {}
        for path, items in by_file.items():
            modname = path[:-3].replace("/", ".")
            try:
                mod = __import__(modname, fromlist=["_"])
            except BaseException as e:  # import error fails the whole file
                for tid, *_ in items:
                    emit("FAIL", tid)
                out.write(f"NOTE import {modname} {e!r}\n")
                continue
            hooks.after_import(modname, mod)
            for tid, cls, meth, limit in items:
                try:
                    if spec_of[tid].get("replica") == "max_nodes":
                        klass = getattr(mod, cls)
                        klass.test_max_nodes_replica = _max_nodes_replica
                        meth = "test_max_nodes_replica"
                    case = getattr(mod, cls)(meth)
                except BaseException as e:
                    emit("FAIL", tid)
                    out.write(f"NOTE build {tid} {e!r}\n")
                    continue
                id_of[id(case)] = tid
                limit_of[tid] = limit
                suite.addTest(case)

        done = set()
        failed = set()

        timed_out = []

        def on_alarm(signum, frame):
            raise _Timeout("test timeout")

        # per-test limits are in CPU seconds of this process (all threads)
        signal.signal(signal.SIGPROF, on_alarm)

        class Result(unittest.TestResult):
            def startTest(self, test):
                super().startTest(test)
                tid = id_of.get(id(test))
                hooks.start(tid, test)
                if tid:
                    lim = limit_of[tid]
                    if timed_out:
                        # the patch already hung one test: give the rest little room
                        lim = min(lim, 0.3 + 1.5 * float(spec_of[tid].get("_est", lim)))
                    signal.setitimer(signal.ITIMER_PROF, max(0.05, lim))

            def stopTest(self, test):
                signal.setitimer(signal.ITIMER_PROF, 0)
                super().stopTest(test)
                tid = id_of.get(id(test))
                if tid and not hooks.stop(tid):
                    failed.add(tid)
                if tid and tid not in done:
                    done.add(tid)
                    emit("FAIL" if tid in failed else "PASS", tid)

            def _fail(self, test):
                tid = id_of.get(id(test))
                if tid:
                    failed.add(tid)
                    hooks.failed.add(tid)
                else:
                    # class/module fixture errors: _ErrorHolder; report its description
                    out.write(f"NOTE fixture-error {getattr(test, 'description', test)}\n")

            def addError(self, test, err):
                super().addError(test, err)
                if err and isinstance(err[1], _Timeout):
                    timed_out.append(id_of.get(id(test)))
                self._fail(test)

            def addFailure(self, test, err):
                super().addFailure(test, err)
                self._fail(test)

            def addSubTest(self, test, subtest, err):
                super().addSubTest(test, subtest, err)
                if err is not None:
                    self._fail(test)
                    if not issubclass(err[0], _Abort):
                        raise _Abort("first failure")

            def addUnexpectedSuccess(self, test):
                super().addUnexpectedSuccess(test)
                self._fail(test)

        res = Result()
        suite.run(res)
        signal.setitimer(signal.ITIMER_PROF, 0)
        for tid in limit_of:
            if tid not in done:
                emit("FAIL", tid)  # never ran: class setup failed
        out.write("END\n")
        out.flush()
    except BaseException as e:
        try:
            out.write(f"NOTE crash {e!r}\n")
            out.flush()
        except BaseException:
            pass
    finally:
        os._exit(0)


def _max_nodes_replica(self):
    """test_max_nodes at a smaller scale: same code paths, same assertions."""
    from sqlglot import exp, parse_one
    from sqlglot.errors import ParseError

    self.assertIsInstance(parse_one("SELECT 1", max_nodes=1_000_000), exp.Select)
    cols = [f"col{i}" for i in range(500)]
    sql = f"SELECT {','.join(cols)} FROM t"
    self.assertIsInstance(parse_one(sql, max_nodes=1_000_000), exp.Select)
    width = 200
    depth = 100
    expr = "x" + "=x" * depth
    sql = f"SELECT {','.join([expr for _ in range(width)])}"
    with self.assertRaises(ParseError) as ctx:
        parse_one(sql, max_nodes=10_000)
    self.assertIn("Maximum number of AST nodes", str(ctx.exception))
    with self.assertRaises(ParseError) as ctx:
        parse_one(sql, max_nodes=5_000)
    self.assertIn("(5000)", str(ctx.exception))
    with self.assertRaises(ParseError):
        parse_one("SELECT " + ",".join(f"x={i}" for i in range(100)), max_nodes=5)
    self.assertIsInstance(parse_one("SELECT 1, 2, 3"), exp.Select)


# Parent-side data shared with children through fork.
PRE_ROWS = None   # {(kind, table): rows}
TPCDS_HASHES = {}  # item -> sha1 of the optimizer output execute() works from (pinned commit)
FRAMES = None     # {"tpch": {sql: df}, "tpcds": {sql: df}}


def preload_executor_data():
    """Parse the TPC CSVs (pure Python, independent of sqlglot) and load the DuckDB results
    recorded at the pinned commit."""
    global PRE_ROWS, FRAMES
    import gzip
    import pickle

    global TPCDS_HASHES
    try:
        import json as _json
        with gzip.open(os.path.join(APP, "tpcds.json.gz"), "rt") as fh:
            TPCDS_HASHES = {int(k): v for k, v in _json.load(fh)["hashes"].items()}
    except Exception as e:  # noqa: BLE001
        log("tpcds hashes unavailable", repr(e))
    try:
        with gzip.open(os.path.join(APP, "frames.pkl.gz"), "rb") as fh:
            FRAMES = pickle.load(fh)
    except Exception as e:  # noqa: BLE001
        log("frames unavailable", repr(e))
        FRAMES = None
    try:
        with open(os.environ.get("BR_PREROWS", os.path.join(APP, "pre_rows.pkl")), "rb") as fh:
            PRE_ROWS = pickle.load(fh)
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        PRE_ROWS = parse_tpc_rows()
    except Exception as e:  # noqa: BLE001
        log("csv preload failed", repr(e))
        PRE_ROWS = None


def build_pre_rows(path: str) -> None:
    """Build step: parse the pinned commit's TPC CSVs once, as the executor tests do."""
    import pickle

    with open(path, "wb") as fh:
        pickle.dump(parse_tpc_rows(), fh, protocol=5)


def parse_tpc_rows():
    import ast
    import csv
    import gzip

    rows_by = {}
    fixtures = os.path.join(ROOT, "tests", "fixtures", "optimizer")
    for kind, sub in (("tpch", "tpc-h"), ("tpcds", "tpc-ds")):
        d = os.path.join(fixtures, sub)
        for name in sorted(os.listdir(d)):
            if not name.endswith(".csv.gz"):
                continue
            table = name[: -len(".csv.gz")]
            with open(os.path.join(d, name), "rb") as fh:
                gz = fh.read(2) == b"\x1f\x8b"
            f = gzip.open(os.path.join(d, name), "rt", newline="") if gz else open(
                os.path.join(d, name), encoding="utf-8", newline="")
            with f:
                reader = csv.reader(f, delimiter="|")
                next(reader)
                rows = []
                ctypes = []
                for row in reader:
                    if not ctypes:
                        for v in row:
                            try:
                                ctypes.append(type(ast.literal_eval(v)))
                            except (ValueError, SyntaxError):
                                ctypes.append(str)
                    rows.append(tuple(None if (t is not str and v == "") else t(v)
                                      for t, v in zip(ctypes, row)))
            rows_by[(kind, table)] = rows
    return rows_by


class _ConnProxy:
    """DuckDB connection that answers queries recorded at the pinned commit from cache."""

    def __init__(self, conn, frames):
        self._conn = conn
        self._frames = frames or {}

    def execute(self, q, *a, **k):
        if not a and not k and isinstance(q, str) and q in self._frames:
            df = self._frames[q]

            class _R:
                def fetchdf(self_inner):
                    return df.copy()

                df_ = fetchdf

            return _R()
        return self._conn.execute(q, *a, **k)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class TestHooks:
    def __init__(self, spec_of, emit, out):
        self.spec_of = spec_of
        self.emit = emit
        self.out = out
        self.cur = None
        self.counter = 0
        self.mismatch = False
        self.failed = set()

    # -- fixture loaders -----------------------------------------------------
    def install_loader_filters(self):
        if not any(s.get("pool") is not None or s.get("pairs") is not None or s.get("lines") is not None
                   for s in self.spec_of.values()):
            self.filtering = False
            return
        self.filtering = True
        import tests.helpers as helpers

        orig_pairs = helpers.load_sql_fixture_pairs
        orig_lines = helpers.load_sql_fixtures
        hooks = self

        def load_sql_fixture_pairs(filename):
            spec = hooks.spec_of.get(hooks.cur) or {}
            pool = spec.get("pool")
            pairs = None
            if spec.get("file"):
                if not spec["file"].endswith(filename):
                    return  # fixture-only plan: this file did not change
                pairs = spec.get("pairs")
            for pos, item in enumerate(orig_pairs(filename)):
                c = hooks.counter
                if c is not None:
                    hooks.counter += 1
                if pool is not None and c not in pool:
                    continue
                if pairs is not None and pos not in pairs:
                    continue
                yield item

        def load_sql_fixtures(filename):
            spec = hooks.spec_of.get(hooks.cur) or {}
            lines = None
            if spec.get("file"):
                if not spec["file"].endswith(filename):
                    return
                lines = spec.get("lines")
            pool = spec.get("pool")
            for pos, item in enumerate(orig_lines(filename)):
                # items are numbered across both loaders, as in the coverage maps
                c = hooks.counter
                if c is not None:
                    hooks.counter += 1
                if pool is not None and c not in pool:
                    continue
                if lines is not None and pos not in lines:
                    continue
                yield item

        helpers.load_sql_fixture_pairs = load_sql_fixture_pairs
        helpers.load_sql_fixtures = load_sql_fixtures
        self._pairs = load_sql_fixture_pairs
        self._lines = load_sql_fixtures

    def after_import(self, modname, mod):
        if POOL_SWAP and getattr(mod, "ProcessPoolExecutor", None) is POOL_SWAP[0]:
            mod.ProcessPoolExecutor = POOL_SWAP[1]  # module imported before the pool swap
        if LAZY_WAIT:
            if getattr(mod, "wait", None) is not None and getattr(mod.wait, "__module__", "") == "concurrent.futures._base":
                mod.wait = LAZY_WAIT[0]
            if getattr(mod, "as_completed", None) is not None and getattr(mod.as_completed, "__module__", "") == "concurrent.futures._base":
                mod.as_completed = LAZY_WAIT[1]
        if getattr(self, "filtering", False):
            if hasattr(mod, "load_sql_fixture_pairs"):
                mod.load_sql_fixture_pairs = self._pairs
            if hasattr(mod, "load_sql_fixtures"):
                mod.load_sql_fixtures = self._lines
        if modname == "tests.test_executor":
            self._patch_executor(mod)
        if modname == "tests.test_generator":
            self.gen_mod = mod

    def _patch_executor(self, te):
        if PRE_ROWS is None:
            if FRAMES is not None:
                orig = te.TestExecutor.setUpClass.__func__

                @classmethod
                def setUpClass(cls):
                    orig(cls)
                    cls.tpch_conn = _ConnProxy(cls.tpch_conn, FRAMES.get("tpch"))
                    cls.tpcds_conn = _ConnProxy(cls.tpcds_conn, FRAMES.get("tpcds"))

                te.TestExecutor.setUpClass = setUpClass
            return
        import duckdb

        @classmethod
        def setUpClass(cls):
            cls.tpch_conn = duckdb.connect()
            cls.tpcds_conn = duckdb.connect()
            cls.tpch_tables = {}
            cls.tpcds_tables = {}

            def setup(conn, directory, table, columns, tables, kind):
                file_name = f"{directory}{table}.csv.gz"
                conn.execute(
                    f"""
                CREATE VIEW {table} AS
                SELECT *
                FROM READ_CSV('{file_name}', delim='|', header=True, columns={columns})
                """
                )
                rows = list(PRE_ROWS[(kind, table)])
                tables[table] = te.Table(columns=columns, rows=rows)

            for table, columns in te.TPCH_SCHEMA.items():
                setup(cls.tpch_conn, te.DIR_TPCH, table, columns, cls.tpch_tables, "tpch")
            for table, columns in te.TPCDS_SCHEMA.items():
                setup(cls.tpcds_conn, te.DIR_TPCDS, table, columns, cls.tpcds_tables, "tpcds")
            cls.cache = {}
            cls.tpch_sqls = list(te.load_sql_fixture_pairs("optimizer/tpc-h/tpc-h.sql"))
            cls.tpcds_sqls = list(te.load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
            if FRAMES is not None:
                cls.tpch_conn = _ConnProxy(cls.tpch_conn, FRAMES.get("tpch"))
                cls.tpcds_conn = _ConnProxy(cls.tpcds_conn, FRAMES.get("tpcds"))

        te.TestExecutor.setUpClass = setUpClass

    # -- per test ------------------------------------------------------------
    def _tpcds_decide(self, test, spec):
        """Settle the TPC-DS executor test cheaply where possible. Returns 'fail', 'pass'
        or None (run it, on the item list left on the instance)."""
        import hashlib
        import json as _json
        import pickle

        if any(g in self.failed for g in spec.get("gate", ())):
            return "fail"
        plan = spec["tpcds"]
        from sqlglot import parse_one, transpile
        from sqlglot.optimizer import optimize
        from sqlglot.schema import ensure_schema
        from sqlglot.serde import dump
        import tests.helpers as helpers

        sqls = type(test).tpcds_sqls
        run = set(plan.get("run", ()))
        try:
            for i in plan.get("parse", ()):
                pickle.loads(pickle.dumps(parse_one(sqls[i][1])))
        except Exception:  # noqa: BLE001
            return "fail"
        for i in plan.get("proxy", ()):
            if i in run:
                continue
            try:
                e = pickle.loads(pickle.dumps(parse_one(sqls[i][1])))
                o = optimize(e, ensure_schema(helpers.TPCDS_SCHEMA), leave_tables_isolated=True, dialect=None)
                h = hashlib.sha1(_json.dumps(dump(o), sort_keys=True, default=str).encode()).hexdigest()
            except Exception:  # noqa: BLE001
                run.add(i)
                continue
            if h != TPCDS_HASHES.get(i):
                run.add(i)
        frames = (FRAMES or {}).get("tpcds", {})
        for i in plan.get("duck", ()):
            if i in run:
                continue
            try:
                q = transpile(sqls[i][1], write="duckdb")[0]
            except Exception:  # noqa: BLE001
                run.add(i)
                continue
            if q not in frames:
                run.add(i)
        if not run:
            return "pass"
        test.tpcds_sqls = [x for j, x in enumerate(sqls) if j in run]
        self.out.write(f"NOTE tpcds running {len(run)} items\n")
        return None

    def start(self, tid, test):
        self.cur = tid
        self.counter = 0
        spec = self.spec_of.get(tid) or {}
        if spec.get("tpcds") is not None:
            try:
                verdict = self._tpcds_decide(test, spec)
            except Exception as e:  # noqa: BLE001
                self.out.write(f"NOTE tpcds-decide-error {e!r}\n")
                verdict = None
                test.tpcds_sqls = type(test).tpcds_sqls
            meth = test._testMethodName
            if verdict == "pass":
                setattr(test, meth, lambda: None)
            elif verdict == "fail":
                def _gated():
                    raise AssertionError("gated: a related test failed")
                setattr(test, meth, _gated)
            self.counter = None
            return
        if tid and tid.startswith("tests/test_executor.py::") and spec:
            # executor TPC lists are loaded once per class: filter per test on the instance
            for attr, fname in (("tpch_sqls", "tpc-h.sql"), ("tpcds_sqls", "tpc-ds.sql")):
                full = getattr(type(test), attr, None)
                if full is None:
                    continue
                keep = None
                if spec.get("pool") is not None:
                    keep = spec["pool"]
                if spec.get("pairs") is not None:
                    keep = spec["pairs"] if spec.get("file", "").endswith(fname) else keep
                if keep is not None:
                    setattr(test, attr, [x for i, x in enumerate(full) if i in keep])
            self.counter = None  # executor items are not counted through the loader
        if spec.get("dialects") is not None and getattr(self, "gen_mod", None) is not None:
            D = getattr(self, "_all_dialects", None) or list(self.gen_mod.Dialects)
            self._all_dialects = D
            self.gen_mod.Dialects = [d for d in D if d.name in spec["dialects"]]

    def stop(self, tid) -> bool:
        """False when the filtered run cannot be trusted (item counts differ from the map)."""
        spec = self.spec_of.get(tid) or {}
        ok = True
        exp = spec.get("expect_items")
        if exp is not None and self.counter is not None and self.counter != exp:
            self.out.write(f"NOTE item-mismatch {tid} {self.counter} != {exp}\n")
            ok = False
        self.cur = None
        return ok


class Job:
    def __init__(self, key, pid, rfd, tests, started, hard_limit, overlay):
        self.key = key
        self.pid = pid
        self.rfd = rfd
        self.tests = tests
        self.started = started
        self.hard_limit = hard_limit
        self.buf = b""
        self.passed: set[str] = set()
        self.failed: set[str] = set()
        self.notes: list[str] = []
        self.ended = False
        self.overlay = overlay


def write_overlay(key: str, files: dict[str, str]) -> str:
    d = os.path.join(TMP, "ov", key)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    for rel, text in files.items():
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
    return d


_TICK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def proc_cpu(pid: int) -> float | None:
    """CPU seconds (user+sys, including reaped children) used so far by a process."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read()
        fields = data[data.rindex(b")") + 2:].split()
        return (int(fields[11]) + int(fields[12]) + int(fields[13]) + int(fields[14])) / _TICK
    except (OSError, ValueError, IndexError):
        return None


def run_all(jobs_in: list[tuple[str, dict, list[tuple[str, float]]]], parallel: int = 2,
            deadline: float | None = None, warm: dict | None = None) -> dict[str, dict]:
    """jobs_in: [(key, overlay_files, [(test_id, timeout_s)])]. Returns key -> result dict.

    A result has 'failed', 'passed', 'complete' (every test reported).
    """
    pending = list(jobs_in)
    running: dict[int, Job] = {}
    results: dict[str, dict] = {}
    # Jobs whose patch touches none of the warm modules start from a parent that already
    # imported them; the others are forked first, from the clean parent.
    hot_keys = set()
    if warm:
        wm = set(warm.get("modules") or ())
        cold = [j for j in pending if len(j) < 5 or set(j[4]) & wm]
        hot = [j for j in pending if not (len(j) < 5 or set(j[4]) & wm)]
        if len(hot) >= 2:
            pending = cold + hot
            hot_keys = {j[0] for j in hot}
    warmed = [False]

    def start(item):
        key, files, tests = item[:3]
        budget = item[3] if len(item) > 3 else None
        extra = {"hotpatch": item[5]} if len(item) > 5 and item[5] else {}
        overlay = write_overlay(key, files)
        r, w = os.pipe()
        sys.stdout.flush()
        sys.stderr.flush()
        if GC_FREEZE:
            # what the parent holds (imports, TPC rows, frames) is never garbage: keep the
            # children's collections from scanning it again and again
            import gc

            gc.freeze()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            try:
                _child(w, overlay, tests, extra)
            finally:
                os._exit(0)
        os.close(w)
        hard = sum(t[1] for t in tests) + 10
        job = Job(key, pid, r, tests, time.time(), hard, overlay)
        job.budget = budget
        running[r] = job

    def finish(job: Job, killed=False):
        try:
            os.close(job.rfd)
        except OSError:
            pass
        cpu = None
        try:
            _, _, ru = os.wait4(job.pid, 0)
            cpu = ru.ru_utime + ru.ru_stime
        except ChildProcessError:
            pass
        shutil.rmtree(job.overlay, ignore_errors=True)
        all_ids = {t[0] for t in job.tests}
        complete = job.ended and (job.passed | job.failed) >= all_ids
        results[job.key] = {
            "failed": set(job.failed),
            "passed": set(job.passed),
            "complete": complete,
            "notes": job.notes,
            "killed": killed,
            "seconds": time.time() - job.started,
            "cpu": cpu,
        }

    while pending or running:
        while pending and len(running) < parallel:
            if deadline is not None and time.time() > deadline:
                for key in [j[0] for j in pending]:
                    results[key] = {"failed": set(), "passed": set(), "complete": False,
                                    "notes": ["deadline"], "killed": False, "seconds": 0}
                pending = []
                break
            if pending[0][0] in hot_keys and not warmed[0]:
                warmed[0] = True
                try:
                    warm_import(bool(warm.get("heavy")))
                except BaseException as e:  # noqa: BLE001
                    log("warm import failed", repr(e))
                loaded = loaded_project_modules()
                for j in list(pending):
                    if set(j[4]) & loaded:  # should not happen: never run a patch on stale modules
                        pending.remove(j)
                        results[j[0]] = {"failed": set(), "passed": set(), "complete": False,
                                         "notes": ["stale-warm"], "killed": False, "seconds": 0}
                if not pending:
                    break
            start(pending.pop(0))
        if not running:
            break
        ready, _, _ = select.select(list(running), [], [], 0.05)
        for fd in ready:
            job = running[fd]
            chunk = os.read(fd, 65536)
            if not chunk:
                del running[fd]
                finish(job)
                continue
            job.buf += chunk
            *lines, job.buf = job.buf.split(b"\n")
            for line in lines:
                s = line.decode("utf-8", "replace")
                if s.startswith("PASS "):
                    job.passed.add(s[5:])
                elif s.startswith("FAIL "):
                    job.failed.add(s[5:])
                elif s == "END":
                    job.ended = True
                else:
                    job.notes.append(s)
        now = time.time()
        for fd, job in list(running.items()):
            over = False
            if job.budget is not None:
                c = proc_cpu(job.pid)
                if c is not None and c > job.budget:
                    over = True
                    job.notes.append(f"NOTE budget {c:.2f} > {job.budget:.2f}")
            if over or now - job.started > job.hard_limit or (deadline is not None and now > deadline + 20):
                for kill in (lambda: os.killpg(job.pid, signal.SIGKILL), lambda: os.kill(job.pid, signal.SIGKILL)):
                    try:
                        kill()
                    except (ProcessLookupError, PermissionError, OSError):
                        pass
                del running[fd]
                finish(job, killed=True)
    return results
