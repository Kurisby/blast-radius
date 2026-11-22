"""Record DuckDB results of the executor's TPC queries at the pinned commit (keyed by SQL)."""
import os, sys, pickle, gzip, unittest
os.environ["SKIP_INTEGRATION"] = "0"
root = sys.argv[1]; out = sys.argv[2]
sys.path.insert(0, root); os.chdir(root)
import tests.test_executor as te
from concurrent.futures import Future
import concurrent.futures as cf

class SyncPool:
    def __init__(self, *a, initializer=None, initargs=(), **k):
        if initializer: initializer(*initargs)
    def submit(self, fn, *a, **k):
        f = Future(); f.set_result(fn(*a, **k)); return f
    def __enter__(self): return self
    def __exit__(self, *e): return False
te.ProcessPoolExecutor = SyncPool

frames = {"tpch": {}, "tpcds": {}}
class Rec:
    def __init__(self, conn, store): self.conn, self.store = conn, store
    def execute(self, q, *a, **k):
        r = self.conn.execute(q, *a, **k)
        store = self.store
        class R:
            def fetchdf(self_inner):
                df = r.fetchdf()
                store[q] = df.copy()
                return df
        return R()
    def __getattr__(self, n): return getattr(self.conn, n)

orig = te.TestExecutor.setUpClass.__func__
@classmethod
def setUpClass(cls):
    orig(cls)
    cls.tpch_conn = Rec(cls.tpch_conn, frames["tpch"])
    cls.tpcds_conn = Rec(cls.tpcds_conn, frames["tpcds"])
te.TestExecutor.setUpClass = setUpClass
suite = unittest.TestSuite()
for m in ("test_execute_tpcds", "test_execute_tpch", "test_optimized_tpch"):
    suite.addTest(te.TestExecutor(m))
res = unittest.TextTestRunner(verbosity=1).run(suite)
print({k: len(v) for k, v in frames.items()}, file=sys.stderr)
with gzip.open(out, "wb") as fh:
    pickle.dump(frames, fh, protocol=pickle.HIGHEST_PROTOCOL)
print(os.path.getsize(out), file=sys.stderr)
