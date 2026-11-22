"""Blast Radius selector.

For each patch: find the tests (and fixture items) that can observe the change from coverage
maps of the pinned sqlglot commit, run them against the patched tree inside this container,
and print only the tests that fail. When running would cost more than just listing the
candidates, list them instead.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# every thread's CPU is billed: keep numeric libraries single-threaded
for _v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_v, "1")

APP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP)

from analysis import FULL, Analyzer, Maps  # noqa: E402

DATA = Path(os.environ.get("BR_DATA", "/data"))
SQLGLOT = Path(os.environ.get("BR_SQLGLOT", "/sqlglot"))
_COMPACT = os.path.join(APP, "maps.marshal")  # written at build time from maps.json.gz
MAPS = Path(os.environ.get("BR_MAPS") or (_COMPACT if os.path.exists(_COMPACT) else os.path.join(APP, "maps.json.gz")))
MODE = os.environ.get("BR_MODE", "run")
DEADLINE_S = float(os.environ.get("BR_DEADLINE", "140"))
PARALLEL = int(os.environ.get("BR_PARALLEL", "2"))
T0 = time.time()

TPCDS = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
TPCH_EXEC = "tests/test_executor.py::TestExecutor::test_execute_tpch"
TPCH_OPT = "tests/test_executor.py::TestExecutor::test_optimized_tpch"
MAX_NODES = "tests/test_parser.py::TestParser::test_max_nodes"
GENMEM = "tests/test_generator.py::TestGenerator::test_generator_memory"

# in-container CPU estimates (seconds) for tests whose cost differs from the recorded time
EST_OVERRIDE = {MAX_NODES: 0.35}
EXECUTOR_CLASS_SETUP = 0.35
PER_JOB_OVERHEAD = 0.3
# Estimates come from an unpatched run; the scoring machine and patched code can be slower.
SAFETY = float(os.environ.get("BR_SAFETY", "1.4"))
P_FAIL_LIKELY = float(os.environ.get("BR_P_LIKELY", "0.95"))  # changed optimizer fixture item: its reader almost always fails
P_FAIL = float(os.environ.get("BR_P_FAIL", "0.05"))
BUDGET_MULT = float(os.environ.get("BR_BUDGET_MULT", "2.5"))  # 0: budget is the listing cost alone


def log(*a):
    print(f"[{time.time() - T0:6.2f}]", *a, file=sys.stderr, flush=True)


def split(numbers, duration_ms: dict[int, int]) -> list[list[int]]:
    ms = sorted(numbers, key=lambda n: -duration_ms[n])
    if not ms:
        return []
    best = None
    for k in range(1, 9):
        loads = [0] * k
        workers = [[] for _ in range(k)]
        for n in ms:
            i = loads.index(min(loads))
            loads[i] += duration_ms[n]
            workers[i].append(n)
        cost = max(loads) + 2000 * k
        if best is None or cost < best[0]:
            best = (cost, workers)
    return [w for w in best[1] if w]


def output_cost(numbers, duration_ms) -> float:
    ws = split(numbers, duration_ms)
    if not ws:
        return 0.0
    return (max(sum(duration_ms[n] for n in w) for w in ws) + 2000 * len(ws)) / 1000


def load_tpcds():
    import gzip
    try:
        with gzip.open(os.path.join(APP, "tpcds.json.gz"), "rt") as fh:
            d = json.load(fh)
        return {
            "phases": d["phases"],
            "times": {int(k): v for k, v in d["times"].items()},
            "executed": {int(k) for k in d["hashes"]},
        }
    except Exception as e:  # noqa: BLE001
        log("tpcds data unavailable", repr(e))
        return None


def tpcds_spec(td, touched, plan_items):
    """Split TPC-DS items by the phase that executes the changed statements."""
    ph = td["phases"]

    def items(phase):
        res = set()
        for rel, lines in touched:
            fl = ph.get(phase, {}).get(rel)
            if not fl:
                continue
            for l in lines:
                res.update(fl.get(str(l), ()))
        return res

    executed = td["executed"]
    allowed = set(plan_items)  # items the plan says can observe the change
    run = items("exec") & allowed
    parse = items("parse") & allowed
    opt = items("optimize") & allowed
    duck = items("duck") & allowed
    known = run | parse | opt | duck
    run |= (set(plan_items) - known) & executed
    parse_only = (set(plan_items) - known) - executed
    spec = {
        "run": sorted(run),
        "proxy": sorted(((parse | opt) & executed) - run),
        "parse": sorted((parse - executed) | parse_only),
        "duck": sorted(duck - run),
    }
    t = td["times"]
    est = (sum(t.get(i, {}).get("exec", 0.3) for i in spec["run"]) + 0.04 * len(spec["proxy"])
           + 0.012 * len(spec["duck"]) + 0.005 * len(spec["parse"]) + 0.05)
    return spec, est


STRONG_GATES = ("tests/test_executor.py::TestExecutor::test_execute_tpch",
                "tests/test_executor.py::TestExecutor::test_optimized_tpch")


def tpcds_gate(gate, est):
    """Tests whose failure settles the TPC-DS executor test as failing without running it.
    Listing it costs ~21 s whenever it would have passed, so a gate only pays off when the
    run is costly and the gate test is a close proxy (the other TPC executor tests)."""
    if est <= 3.0:
        return []
    if est <= 10.0:
        return [g for g in gate if g in STRONG_GATES]
    return gate


def dialects_for(files: dict, dialect_files: dict) -> list[str] | None:
    """Dialects whose class hierarchy includes a changed module (None = all)."""
    res = set()
    for rel in files:
        if not rel.startswith("sqlglot/"):
            continue
        hit = [d for d, fs in dialect_files.items() if rel in fs]
        if len(hit) == len(dialect_files) or not hit:
            return None
        res |= set(hit)
    return sorted(res)


def module_names(files) -> list[str]:
    res = []
    for rel in files or {}:
        if rel.endswith(".py"):
            m = rel[:-3].replace("/", ".")
            if m.endswith(".__init__"):
                m = m[: -len(".__init__")]
            res.append(m)
    return res


def load_warm(heavy: bool):
    try:
        with open(os.environ.get("BR_WARM", os.path.join(APP, "warm_modules.json"))) as fh:
            d = json.load(fh)
        return {"modules": set(d["heavy" if heavy else "light"]), "heavy": heavy}
    except Exception as e:  # noqa: BLE001
        log("no warm module list", repr(e))
        return None


def choose_split(cand, ests, seconds, duration_ms, maps, likely):
    """Pick which candidates to run (the rest are listed without running) to minimise
    expected CPU + listed cost. Tests likely to fail, and tests whose run cost is high
    relative to their recorded time, are listed first."""
    order = sorted(cand, key=lambda t: (-(1.0 if t in likely else 0.0),
                                        -(ests[t] + 0.01) / (seconds[t] + 0.01)))
    n = len(order)
    ks = sorted({0, n} | {min(n, 2 ** i) for i in range(0, 12)} | {k for k in range(0, min(n, 12) + 1)}
                | {max(0, n - 2 ** i) for i in range(0, 12)} | {max(0, n - k) for k in range(0, 13)})
    best = None
    for k in ks:
        blind = set(order[:k])
        run = order[k:]
        c = output_cost(blind, duration_ms)
        if run:
            p_none = 1.0
            for t in run:
                p = P_FAIL_LIKELY if t in likely else P_FAIL
                c += SAFETY * ests[t] + p * seconds[t]
                p_none *= 1.0 - p
            c += PER_JOB_OVERHEAD
            if not blind:
                c += 2.0 * (1.0 - p_none)  # a failing run test opens a worker
            if any(maps.id_by_number[t].startswith("tests/test_executor.py::") for t in run):
                c += EXECUTOR_CLASS_SETUP
        if best is None or c < best[0] - 1e-9:
            best = (c, k, blind)
    c, k, blind = best
    if k == n:
        return "blind", set(cand), 0.0
    mode = "run" if k == 0 else "mixed"
    run = [t for t in cand if t not in blind]
    run_est = sum(ests[t] for t in run) + PER_JOB_OVERHEAD + (
        EXECUTOR_CLASS_SETUP if any(maps.id_by_number[t].startswith("tests/test_executor.py::") for t in run) else 0.0)
    return mode, blind, run_est


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    duration_ms = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
    seconds = {t["number"]: t["seconds"] for t in tests}
    maps = Maps(MAPS, tests)
    analyzer = Analyzer(maps, SQLGLOT)
    td = load_tpcds()
    try:
        with open(os.path.join(APP, "cpu_est.json")) as fh:
            cpu_est = json.load(fh)
    except Exception:  # noqa: BLE001
        cpu_est = {}
    log("maps loaded")
    patches = sorted((DATA / "commits").glob("*.patch"))
    plans = {}
    for p in patches:
        commit = p.name[: -len(".patch")]
        text = p.read_text(encoding="utf-8", errors="replace")
        try:
            plan, notes, files = analyzer.analyze(text)
        except Exception as e:  # never fail the run: fall back to everything
            log(commit, "analysis failed", repr(e))
            plan, notes, files = {n: FULL for n in maps.all_numbers}, ["failed"], None
        plan = {t: v for t, v in plan.items() if t in maps.all_numbers}
        cand = set(plan)
        blind_cost = output_cost(cand, duration_ms)
        likely = {t for t, v in plan.items() if isinstance(v, dict) and v.get("pairs")
                  and v.get("file", "").startswith("tests/fixtures/optimizer/")
                  and "tpc-" not in v.get("file", "")}
        specs = {}
        ests = {}
        run_est = 0.0
        has_exec = False
        for t, v in plan.items():
            tid = maps.id_by_number[t]
            spec = {}
            frac = 1.0
            if isinstance(v, set):
                spec["pool"] = sorted(v)
                n_items = maps.items_count.get(t)
                if n_items:
                    frac = min(1.0, len(v) / n_items)
                    if not tid.startswith("tests/test_executor.py::"):
                        spec["expect_items"] = n_items
            elif isinstance(v, dict):
                spec["pairs"] = sorted(v["pairs"])
                spec["lines"] = sorted(v["lines"])
                spec["file"] = v["file"]
                frac = 0.2
            est = cpu_est.get(tid, seconds[t]) * frac + 0.002
            if tid in EST_OVERRIDE:
                est = EST_OVERRIDE[tid]
            if tid == MAX_NODES:
                spec["replica"] = "max_nodes"
            if tid == GENMEM:
                ds = dialects_for(files or {}, maps.dialect_files)
                if ds is not None:
                    spec["dialects"] = ds
                est = 2.2 * (len(ds) / len(maps.dialect_files) if ds is not None else 1.0)
            if tid == TPCDS:
                gate = [maps.id_by_number[o] for o in plan if o != t and (
                    maps.id_by_number[o].startswith("tests/test_executor.py::")
                    or maps.id_by_number[o].split("::")[-1] in ("test_tpcds", "test_tpch", "test_optimize"))]
                if td is not None and isinstance(v, set) and not analyzer.nonlocal_change:
                    tspec, est = tpcds_spec(td, analyzer.touched, v)
                    spec = {"tpcds": tspec, "gate": tpcds_gate(gate, est)}
                else:
                    idxs = spec.get("pool")
                    if spec.get("pairs") is not None:
                        idxs = spec["pairs"]
                    if idxs is None and td is not None:
                        idxs = sorted(td["executed"])
                    times = td["times"] if td is not None else {}
                    est = sum(times.get(i, {}).get("exec", 0.3) + times.get(i, {}).get("parse", 0.005)
                              for i in (idxs or [])) + 0.1 if idxs is not None else 12.5
                    spec["gate"] = tpcds_gate(gate, est)
            if tid.startswith("tests/test_executor.py::"):
                has_exec = True
            spec["_est"] = round(est, 4)
            spec["_full"] = round(cpu_est.get(tid, seconds[t]), 4)
            specs[tid] = spec
            ests[t] = est
            run_est += est
        if has_exec:
            run_est += EXECUTOR_CLASS_SETUP
        mode = "blind"
        blind_part = set(cand)
        if MODE == "run" and files is not None and cand:
            mode, blind_part, run_est = choose_split(cand, ests, seconds, duration_ms, maps, likely)
            for t in blind_part:
                specs.pop(maps.id_by_number[t], None)
        plans[commit] = {"cand": cand, "specs": specs, "files": files, "mode": mode,
                         "run_est": run_est, "blind_cost": blind_cost, "blind_part": blind_part}
        log(commit, mode, f"cand={len(cand)} est={run_est:.2f} blind={blind_cost:.2f}", notes[:3])

    jobs = []
    for c, pl in plans.items():
        if pl["mode"] not in ("run", "mixed"):
            continue
        items = []
        for tid, spec in sorted(pl["specs"].items()):
            # CPU seconds; a timeout only lists the test, it can never miss a failure
            est = spec.get("_est", 0.3)
            limit = 0.8 + 3.0 * max(est, min(spec.get("_full", est), 2 * est + 0.5))
            if spec.get("replica"):
                limit = 3.0
            items.append((tid, limit, spec))
        ran = {maps.number_by_id[tid] for tid in pl["specs"]}
        # running can never be worth more CPU than listing what it runs
        cap = BUDGET_MULT * pl["run_est"] + 0.8 if BUDGET_MULT > 0 else float("inf")
        budget = max(1.0, min(output_cost(ran, duration_ms), cap))
        pl["budget"] = budget
        jobs.append((c, pl["files"], items, budget, module_names(pl["files"])))
    results = {}
    if jobs:
        import runner

        heavy = any(tid.startswith(("tests/test_optimizer.py::", "tests/test_executor.py::"))
                    for _, _, items, _, _ in jobs for tid, _, _ in items)
        runner.prepare_parent(heavy=heavy)
        if any(tid.startswith("tests/test_executor.py::") for _, _, items, _, _ in jobs for tid, _, _ in items):
            runner.preload_executor_data()
        log("parent ready", len(jobs), "jobs")
        # longest first keeps both cores busy
        jobs.sort(key=lambda j: -plans[j[0]]["run_est"])
        warm = load_warm(heavy) if os.environ.get("BR_WARM_OFF") != "1" else None
        results = runner.run_all(jobs, parallel=PARALLEL, deadline=T0 + DEADLINE_S, warm=warm)

    out = []
    total_est = 0.0
    for c, pl in plans.items():
        if pl["mode"] in ("run", "mixed"):
            r = results.get(c)
            ran = {maps.number_by_id[tid] for tid in pl["specs"]}
            if r is None:
                sel = set(pl["cand"])
            else:
                failed = {maps.number_by_id[t] for t in r["failed"] if t in maps.number_by_id}
                passed = {maps.number_by_id[t] for t in r["passed"] if t in maps.number_by_id}
                unknown = ran - passed - failed
                sel = failed | unknown | (pl["cand"] - ran)
                total_est += pl["run_est"]
                log(c, f"est {pl['run_est']:.2f} budget {pl.get('budget', 0):.2f} cpu {r.get('cpu') or 0:.2f} wall {r['seconds']:.2f} "
                       f"failed {len(failed)} unknown {len(unknown)}", [n for n in r["notes"] if not n.startswith("NOTE t ")][:3])
                if os.environ.get("BR_VERBOSE"):
                    tl = sorted(((float(n.split()[-1]), n.split()[2]) for n in r["notes"] if n.startswith("NOTE t ")), reverse=True)
                    log("   slowest", [(round(a, 2), b.split("::")[-1]) for a, b in tl[:6]])
        else:
            sel = set(pl["cand"])
        out.append(json.dumps({"commit": c, "workers": split(sel, duration_ms)}))
    data = "\n".join(out) + ("\n" if out else "")
    sys.stdout._br_written = True
    sys.stdout.write(data)
    sys.stdout.flush()
    log("done")


def fallback() -> None:
    """Last resort if anything above failed: every test on every commit."""
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    duration_ms = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
    ws = split([t["number"] for t in tests], duration_ms)
    lines = [json.dumps({"commit": p.name[: -len(".patch")], "workers": ws})
             for p in sorted((DATA / "commits").glob("*.patch"))]
    sys.stdout.write("\n".join(lines) + ("\n" if lines else ""))
    sys.stdout.flush()


if __name__ == "__main__":
    try:
        main()
    except BaseException as e:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        if not getattr(sys.stdout, "_br_written", False):
            fallback()
