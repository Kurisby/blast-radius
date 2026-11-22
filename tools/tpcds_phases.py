"""Per-query, per-phase coverage of the executor's TPC-DS test, plus baseline hashes of the
optimizer output each query feeds to the executor (pinned commit)."""
import os, sys, json, pickle, time, hashlib, gzip
os.environ["SKIP_INTEGRATION"] = "0"
root = os.path.abspath(sys.argv[1]) + "/"; out = sys.argv[2]
sys.path.insert(0, root); os.chdir(root)
import tests.test_executor as te
import sqlglot.executor as sx
from sqlglot import parse_one, transpile
from sqlglot.serde import dump
from sqlglot.schema import ensure_schema

mon = sys.monitoring
TOOL = 4
SRC = root + "sqlglot/"
cur = {"set": None}
def on_line(code, line):
    fn = code.co_filename
    if fn.startswith(SRC) and cur["set"] is not None:
        cur["set"].add((fn[len(root):], line))
    return mon.DISABLE
mon.use_tool_id(TOOL, "phases")
mon.register_callback(TOOL, mon.events.LINE, on_line)

te.TestExecutor.setUpClass()
cls = te.TestExecutor
te.initializer(te.TPCDS_SCHEMA, cls.tpcds_tables)

phases = {}  # (phase) -> {(f,l): set(items)}
def record(phase, i, fn):
    s = set()
    cur["set"] = s
    mon.set_events(TOOL, mon.events.LINE)
    mon.restart_events()
    try:
        return fn()
    finally:
        mon.set_events(TOOL, 0)
        cur["set"] = None
        d = phases.setdefault(phase, {})
        for k in s:
            d.setdefault(k, set()).add(i)

orig_optimize = sx.optimize
hashes = {}
duckq = {}  # item -> the DuckDB SQL its expected frame came from (pinned commit)
phashes = {}  # item -> sha1 of the parsed query as the pool child receives it (pickle = serde dump/load)
def ahash(e):
    return hashlib.sha1(json.dumps(dump(e), sort_keys=True, default=str).encode()).hexdigest()
opt_holder = {}
def wrapped_optimize(*a, **k):
    # switch phase to "optimize" for the duration of the call
    prev = cur["set"]
    s = set()
    cur["set"] = s
    mon.restart_events()
    try:
        r = orig_optimize(*a, **k)
        opt_holder["r"] = r
        return r
    finally:
        cur["set"] = prev
        mon.restart_events()
        d = phases.setdefault("optimize", {})
        for key in s:
            d.setdefault(key, set()).add(opt_holder["i"])
sx.optimize = wrapped_optimize

# execute() logs expression.sql(pretty=True) at debug level: the string is built and thrown
# away, so a line that only this call runs can matter only by raising, or by changing the
# expression (the proxy generates it too and hashes after): its own phase, "pretty"
from sqlglot import exp as _exp
_orig_sql = _exp.Expression.sql
def wrapped_sql(self, *a, **k):
    if k.get("pretty") and opt_holder.get("r") is self and cur["set"] is not None:
        prev = cur["set"]
        s = set()
        cur["set"] = s
        mon.restart_events()
        try:
            return _orig_sql(self, *a, **k)
        finally:
            cur["set"] = prev
            mon.restart_events()
            d = phases.setdefault("pretty", {})
            for key in s:
                d.setdefault(key, set()).add(opt_holder["i"])
    return _orig_sql(self, *a, **k)
_exp.Expression.sql = wrapped_sql

times = {}
for i, (meta, sql, _) in enumerate(cls.tpcds_sqls):
    t0 = time.process_time()
    expr = record("parse", i, lambda: pickle.loads(pickle.dumps(parse_one(sql))))
    t1 = time.process_time()
    if not meta.get("execute"):
        times[i] = {"parse": round(t1 - t0, 4)}
        continue
    opt_holder["i"] = i
    phashes[i] = ahash(expr)
    table = record("exec", i, lambda: pickle.loads(pickle.dumps(te.mp_execute(expr, meta))))
    t2 = time.process_time()
    # baseline hash of what execute() optimizes, computed the way the proxy will
    e2 = pickle.loads(pickle.dumps(parse_one(sql)))
    o = orig_optimize(e2, ensure_schema(te.TPCDS_SCHEMA), leave_tables_isolated=True, dialect=None)
    hashes[i] = ahash(o)
    _orig_sql(o, pretty=True)
    assert ahash(o) == hashes[i], i  # generating SQL leaves the expression as it was
    t3 = time.process_time()
    duck_sql = record("duck", i, lambda: transpile(sql, write="duckdb")[0])
    duckq[i] = duck_sql
    # comparison side: DataFrame from table rows
    import numpy as np, pandas as pd
    record("exec", i, lambda: pd.DataFrame(((np.nan if c is None else c for c in r) for r in table.rows), columns=table.columns))
    times[i] = {"parse": round(t1 - t0, 4), "exec": round(t2 - t1, 4), "proxy": round(t3 - t2, 4)}

res = {"phases": {}, "hashes": hashes, "phashes": phashes, "duckq": duckq, "times": times}
for ph, d in phases.items():
    byfile = {}
    for (f, l), items in d.items():
        byfile.setdefault(f, {})[str(l)] = sorted(items)
    res["phases"][ph] = byfile
with gzip.open(out, "wt") as fh:
    json.dump(res, fh, separators=(",", ":"))
print("phases", {k: len(v) for k, v in phases.items()}, "hashes", len(hashes),
      "exec total", round(sum(v.get("exec", 0) for v in times.values()), 2),
      "proxy total", round(sum(v.get("proxy", 0) for v in times.values()), 2), os.path.getsize(out))
