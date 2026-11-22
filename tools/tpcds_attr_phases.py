"""Per-query, per-phase profile of the class attributes and class-dict keys that the
executor's TPC-DS test reads at the pinned commit (the names the import-time state diff
reports), so a changed setting maps to the phase that can observe it.

usage: tpcds_attr_phases.py SQLGLOT_TREE OUT.json.gz   (tools/ on sys.path for covplugin5)
"""
import gzip
import json
import os
import pickle
import sys

os.environ["SKIP_INTEGRATION"] = "0"
os.environ["COV_KEYS"] = "1"
root = os.path.abspath(sys.argv[1]) + "/"
out = sys.argv[2]
os.environ["COV_ROOT"] = root
os.environ.setdefault("COV_OUTDIR", os.path.dirname(os.path.abspath(out)))
sys.path.insert(0, root)
os.chdir(root)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import covplugin5 as cp  # noqa: E402

cp._activate()  # imports every sqlglot module and wraps the class attributes
cp._set_ctx("<other>")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import sqlglot.executor as sx  # noqa: E402
import tests.test_executor as te  # noqa: E402
from sqlglot import parse_one, transpile  # noqa: E402

te.TestExecutor.setUpClass()
cls = te.TestExecutor
te.initializer(te.TPCDS_SCHEMA, cls.tpcds_tables)

cur = {"i": None}
orig_optimize = sx.optimize


def wrapped_optimize(*a, **k):
    prev = cp._cur["ctx"]
    cp._set_ctx(f"optimize#{cur['i']}")
    try:
        return orig_optimize(*a, **k)
    finally:
        cp._set_ctx(prev)


sx.optimize = wrapped_optimize


def record(phase, i, fn):
    cp._set_ctx(f"{phase}#{i}")
    try:
        return fn()
    finally:
        cp._set_ctx("<other>")


n_exec = 0
for i, (meta, sql, _) in enumerate(cls.tpcds_sqls):
    cur["i"] = i
    expr = record("parse", i, lambda: pickle.loads(pickle.dumps(parse_one(sql))))
    if not meta.get("execute"):
        continue
    n_exec += 1
    table = record("exec", i, lambda: pickle.loads(pickle.dumps(te.mp_execute(expr, meta))))
    record("duck", i, lambda: transpile(sql, write="duckdb")[0])
    record("exec", i, lambda: pd.DataFrame(((np.nan if c is None else c for c in r) for r in table.rows),
                                           columns=table.columns))

res = {"parse": {}, "optimize": {}, "exec": {}, "duck": {}}
other = set()
for ctx, rec in cp._rec.items():
    keys = {f"{o}|{k}" for o, k in rec["keys"]}
    phase, _, item = ctx.partition("#")
    if phase in res and item:
        for k in keys:
            res[phase].setdefault(k, []).append(int(item))
    elif not ctx.startswith("<") or ctx == "<other>":
        other |= keys
    # "<import>", "<wrapped>", "<mirror>" and phase totals ("parse") are bookkeeping
for d in res.values():
    for k in d:
        d[k].sort()
with gzip.open(out, "wt", compresslevel=9) as fh:
    json.dump({"phases": res, "other": sorted(other), "executed": n_exec}, fh, separators=(",", ":"))
print({k: len(v) for k, v in res.items()}, "other", len(other), "executed", n_exec, os.path.getsize(out))
