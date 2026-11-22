"""Per-test and per-fixture-item coverage of sqlglot (v5), plus a comparison-outcome profile.

Contexts: "<test id>" for a test's own work; "<test id>#<k>" for its k-th fixture item, where
k counts the items yielded by tests.helpers' fixture loaders during the test (a pool
submission belongs to the item being processed, and the parent's work after
future.result() belongs to that future's item). Executor tests keep their own numbering
(n-th TPC query).

COV_LINES=1: lines, function starts, file reads, instantiated classes per context.
COV_CMP=1: for every single comparison in sqlglot (other than with None), the outcome classes
seen per context: identity sites (is / is not / == / !=) record when `is` and `==` disagree,
ordering sites (< <= > >=) record when the operands were equal.
"""
import ast
import builtins
import importlib.abc
import importlib.util
import json
import os
import sys
import threading

ROOT = os.environ["COV_ROOT"]
SRC = ROOT + "sqlglot/"
OUTDIR = os.environ["COV_OUTDIR"]
LINES = os.environ.get("COV_LINES") == "1"
CMP = os.environ.get("COV_CMP") == "1"
BOOL = os.environ.get("COV_BOOL") == "1"
MAIN = threading.main_thread().ident
_get_ident = threading.get_ident
mon = sys.monitoring
TOOL = 3

_cur = {"ctx": "<import>"}
_rec = {}
_cur_rec = None
_base_rec = None


def _new():
    return {"lines": set(), "funcs": set(), "reads": set(), "classes": set(), "cmp": set(), "bool": set()}


def _set_ctx(name):
    global _cur_rec, _base_rec
    _cur["ctx"] = name
    _cur_rec = _rec.setdefault(name, _new())
    _base_rec = _rec.setdefault(name.split("#")[0], _new())
    if LINES:
        mon.restart_events()


def _r():
    return _cur_rec if _get_ident() == MAIN else _base_rec


_set_ctx("<import>")


# ---------------------------------------------------------------- comparison profile
_tl = threading.local()
_IDENT = {"is", "in", "eq", "ne"}


def __brcmp__(site, l, r):
    k = site[-2:]
    if k == "is":
        res = l is r
    elif k == "in":
        res = l is not r
    elif k == "eq":
        res = l == r
    elif k == "ne":
        res = l != r
    elif k == "lt":
        res = l < r
    elif k == "le":
        res = l <= r
    elif k == "gt":
        res = l > r
    else:
        res = l >= r
    if getattr(_tl, "busy", False):
        return res
    _tl.busy = True
    try:
        cls = None
        if k in _IDENT:
            ident = l is r
            try:
                eq = l == r
                ne = l != r
                bad = type(eq) is not bool or type(ne) is not bool or eq == ne
            except BaseException:  # noqa: BLE001
                bad = True
            if bad:
                cls = "ERR"
            elif ident and not eq:
                cls = "ISNE"
            elif eq and not ident:
                cls = "EQNI"
            elif k in ("eq", "ne"):
                cls = "OK"  # evaluated: an == / != swap flips it
        else:
            try:
                lt = l < r
                eq = l == r
                gt = l > r
                le = l <= r
                ge = l >= r
                bad = any(type(v) is not bool for v in (lt, eq, gt, le, ge)) or (
                    le != (lt or eq)) or (ge != (gt or eq)) or (lt + eq + gt) != 1
            except BaseException:  # noqa: BLE001
                bad = True
            if bad:
                cls = "ERR"
            elif eq:
                cls = "EQ"
        if cls is not None:
            _r()["cmp"].add((site, cls))
    finally:
        _tl.busy = False
    return res


builtins.__brcmp__ = __brcmp__


# ---------------------------------------------------------------- boolean formula profile
# A formula is a tree of `and` / `or` / `not` over leaf expressions, used for its truth
# value (if / while / assert / conditional expression / comprehension filter). Its leaves
# are all side-effect free, so the ones the original short-circuits past are evaluated too:
# the vector of every leaf's truth value (1/0, or E if evaluating it raised) is recorded.
def __brbool__(site, shape, leaves):
    vals = [None] * len(leaves)

    def ev(node):
        kind = node[0]
        if kind == "L":
            i = node[1]
            vals[i] = "E"  # stays E if evaluating the leaf raises
            v = leaves[i]()
            vals[i] = 1 if v else 0
            return vals[i]
        if kind == "N":
            return 0 if ev(node[1]) else 1
        if kind == "A":
            for c in node[1]:
                if not ev(c):
                    return 0
            return 1
        for c in node[1]:
            if ev(c):
                return 1
        return 0

    try:
        res = ev(shape)  # original semantics, exceptions propagate (after recording)
    except BaseException:
        _record_bool(site, leaves, vals)
        raise
    _record_bool(site, leaves, vals)
    return bool(res)


def _record_bool(site, leaves, vals):
    if not getattr(_tl, "busy", False):
        _tl.busy = True
        try:
            vec = []
            for i, v in enumerate(vals):
                if v is None:
                    try:
                        v = 1 if leaves[i]() else 0
                    except BaseException:  # noqa: BLE001
                        v = "E"
                vec.append(str(v))
            _r()["bool"].add((site, "".join(vec)))
        finally:
            _tl.busy = False


builtins.__brbool__ = __brbool__

_PURE_FUNCS = {"isinstance", "len", "type", "bool", "callable", "hasattr", "issubclass", "id", "getattr",
               "str", "int", "tuple", "frozenset", "any", "all"}
_PURE_METHODS = {"get", "startswith", "endswith", "isdigit", "isalpha", "isalnum", "isspace", "isupper",
                 "islower", "upper", "lower", "strip", "is_type", "is_int", "is_number", "is_string",
                 "is_star", "find", "find_ancestor", "text", "name", "keys", "values", "items",
                 "is_leaf", "isidentifier", "count", "index"}


def _pure(n):
    if isinstance(n, (ast.Name, ast.Constant)):
        return True
    if isinstance(n, ast.Attribute):
        return _pure(n.value)
    if isinstance(n, ast.Subscript):
        return _pure(n.value) and _pure(n.slice)
    if isinstance(n, ast.Slice):
        return all(x is None or _pure(x) for x in (n.lower, n.upper, n.step))
    if isinstance(n, (ast.Tuple, ast.List, ast.Set)):
        return all(_pure(e) for e in n.elts)
    if isinstance(n, ast.Compare):
        return _pure(n.left) and all(_pure(c) for c in n.comparators)
    if isinstance(n, ast.BoolOp):
        return all(_pure(v) for v in n.values)
    if isinstance(n, ast.UnaryOp):
        return _pure(n.operand)
    if isinstance(n, ast.BinOp):
        return _pure(n.left) and _pure(n.right)
    if isinstance(n, ast.Call):
        if n.keywords and not all(_pure(k.value) for k in n.keywords):
            return False
        if not all(_pure(a) and not isinstance(a, ast.Starred) for a in n.args):
            return False
        if isinstance(n.func, ast.Name):
            return n.func.id in _PURE_FUNCS
        if isinstance(n.func, ast.Attribute):
            return n.func.attr in _PURE_METHODS and _pure(n.func.value)
        return False
    return False


def _formula(node, leaves):
    if isinstance(node, ast.BoolOp):
        return ("A" if isinstance(node.op, ast.And) else "O", tuple(_formula(v, leaves) for v in node.values))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return ("N", _formula(node.operand, leaves))
    leaves.append(node)
    return ("L", len(leaves) - 1)


class _Bool(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel
        self.depth = 0

    def visit_FunctionDef(self, node):
        self.depth += 1
        self.generic_visit(node)
        self.depth -= 1
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def _wrap(self, test):
        if self.depth == 0 or test is None:
            return test
        root = test
        if isinstance(root, ast.UnaryOp) and isinstance(root.op, ast.Not):
            inner = root.operand
            if not isinstance(inner, ast.BoolOp):
                return test
        elif not isinstance(root, ast.BoolOp):
            return test
        leaves = []
        shape = _formula(root, leaves)
        if not all(_pure(l) for l in leaves) or len(leaves) > 12:
            return test
        site = f"{self.rel}:{root.lineno}:{root.col_offset}"
        lams = [ast.Lambda(args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[],
                                              defaults=[]), body=l) for l in leaves]
        call = ast.Call(func=ast.Name(id="__brbool__", ctx=ast.Load()),
                        args=[ast.Constant(site), ast.Constant(shape), ast.List(elts=lams, ctx=ast.Load())],
                        keywords=[])
        return ast.copy_location(call, root)

    def visit_If(self, node):
        self.generic_visit(node)
        node.test = self._wrap(node.test)
        return node

    visit_While = visit_If

    def visit_IfExp(self, node):
        self.generic_visit(node)
        node.test = self._wrap(node.test)
        return node

    def visit_Assert(self, node):
        self.generic_visit(node)
        node.test = self._wrap(node.test)
        return node

    def visit_comprehension(self, node):
        self.generic_visit(node)
        node.ifs = [self._wrap(t) for t in node.ifs]
        return node

_OPS = {ast.Is: "is", ast.IsNot: "in", ast.Eq: "eq", ast.NotEq: "ne", ast.Lt: "lt", ast.LtE: "le",
        ast.Gt: "gt", ast.GtE: "ge"}


class _Cmp(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1:
            return node
        op = _OPS.get(type(node.ops[0]))
        if op is None:
            return node
        right = node.comparators[0]
        if any(isinstance(n, ast.Constant) and n.value is None for n in (node.left, right)):
            return node
        site = f"{self.rel}:{node.lineno}:{node.col_offset}:{op}"
        call = ast.Call(func=ast.Name(id="__brcmp__", ctx=ast.Load()),
                        args=[ast.Constant(site), node.left, right], keywords=[])
        return ast.copy_location(call, node)


class _Loader(importlib.abc.SourceLoader):
    def __init__(self, fullname, path):
        self.fullname = fullname
        self.path = path

    def get_filename(self, fullname):
        return self.path

    def get_data(self, path):
        with open(path, "rb") as fh:
            return fh.read()

    def source_to_code(self, data, path, *, _optimize=-1):
        tree = ast.parse(data, filename=path)
        if CMP:
            tree = _Cmp(path[len(ROOT):]).visit(tree)
        if BOOL:
            tree = _Bool(path[len(ROOT):]).visit(tree)
        ast.fix_missing_locations(tree)
        return compile(tree, path, "exec", dont_inherit=True)

    # never use or write bytecode caches
    def path_stats(self, path):
        raise OSError

    def set_data(self, path, data, *, _mode=0o666):
        pass


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname != "sqlglot" and not fullname.startswith("sqlglot."):
            return None
        for entry in sys.meta_path:
            if entry is self or not hasattr(entry, "find_spec"):
                continue
            spec = entry.find_spec(fullname, path, target)
            if spec is not None:
                break
        else:
            return None
        origin = spec.origin or ""
        if not origin.endswith(".py") or not origin.startswith(SRC):
            return spec
        loader = _Loader(fullname, origin)
        return importlib.util.spec_from_file_location(
            fullname, origin, loader=loader, submodule_search_locations=spec.submodule_search_locations)


if (CMP or BOOL) and not any(isinstance(f, _Finder) for f in sys.meta_path):
    sys.meta_path.insert(0, _Finder())


# ---------------------------------------------------------------- line coverage
def _on_line(code, line):
    fn = code.co_filename
    if fn.startswith(SRC):
        _r()["lines"].add((fn[len(ROOT):], line))
    return mon.DISABLE


def _on_start(code, offset):
    fn = code.co_filename
    if fn.startswith(SRC):
        _r()["funcs"].add((fn[len(ROOT):], code.co_firstlineno))
    return mon.DISABLE


def _audit(event, args):
    if event == "open" and args and isinstance(args[0], (str, bytes)):
        ap = os.path.abspath(os.fsdecode(args[0]))
        if ap.startswith(ROOT) and "__pycache__" not in ap and not (
            ap.endswith(".py") and (ap.startswith(SRC) or ap.startswith(ROOT + "tests/"))
        ):
            _r()["reads"].add(ap[len(ROOT):])


def _hook_classes():
    seen_by_ctx = {}

    def make(orig):
        def __init__(self, *a, **k):
            t = type(self)
            r = _r()
            s = seen_by_ctx.setdefault(id(r), set())
            if t not in s:
                s.add(t)
                for c in t.__mro__:
                    m = getattr(c, "__module__", "") or ""
                    if m.startswith("sqlglot"):
                        r["classes"].add(f"{m}:{c.__qualname__}")
            return orig(self, *a, **k)
        return __init__

    from sqlglot.expressions import core
    from sqlglot import tokens, parser, generator
    from sqlglot.dialects import dialect
    for cls in (core.Expr, tokens.Tokenizer, parser.Parser, generator.Generator, dialect.Dialect):
        if "__init__" in cls.__dict__:
            cls.__init__ = make(cls.__dict__["__init__"])
    try:
        from sqlglot import schema
        if "__init__" in schema.MappingSchema.__dict__:
            schema.MappingSchema.__init__ = make(schema.MappingSchema.__dict__["__init__"])
    except Exception:
        pass


_active = False


def _activate():
    global _active
    if _active:
        return
    _active = True
    if LINES:
        _hook_classes()
        mon.use_tool_id(TOOL, "covplugin")
        mon.register_callback(TOOL, mon.events.LINE, _on_line)
        mon.register_callback(TOOL, mon.events.PY_START, _on_start)
        mon.set_events(TOOL, mon.events.LINE | mon.events.PY_START)
        sys.addaudithook(_audit)


def _ser(rec):
    out = {}
    for k in ("lines", "funcs"):
        d = {}
        for f, l in rec[k]:
            d.setdefault(f, []).append(l)
        out[k] = d
    out["reads"] = sorted(rec["reads"])
    out["classes"] = sorted(rec["classes"])
    out["cmp"] = sorted(f"{s}|{c}" for s, c in rec["cmp"])
    out["bool"] = sorted(f"{s}|{c}" for s, c in rec["bool"])
    return out


# ---------------------------------------------------------------- worker side
_wn = [0]


def _cov_wrapper(ctx, fn, *args, **kwargs):
    _activate()
    _set_ctx(ctx)
    try:
        res = fn(*args, **kwargs)
        import pickle
        pickle.dumps(res)
        return res
    finally:
        _wn[0] += 1
        rec = _rec.pop(ctx)
        base = ctx.split("#")[0]
        if base != ctx and base in _rec:
            b = _rec.pop(base)
            for k in rec:
                rec[k] |= b[k]
        with open(os.path.join(OUTDIR, f"w{os.getpid()}_{_wn[0]}.json"), "w") as fh:
            json.dump({ctx: _ser(rec)}, fh)
        _set_ctx("<worker-idle>")


# ---------------------------------------------------------------- pytest side
_items = {}


def _base():
    return _cur["ctx"].split("#")[0]


def _wrap_loader(orig):
    def loader(*a, **k):
        base = _base()
        if base.startswith("<") or base.startswith("tests/test_executor.py::"):
            yield from orig(*a, **k)
            return
        try:
            for item in orig(*a, **k):
                n = _items.get(base, 0)
                _items[base] = n + 1
                _set_ctx(f"{base}#{n}")
                yield item
        finally:
            if _base() == base:
                _set_ctx(base)
    loader._br_wrapped = True
    return loader


def pytest_configure(config):
    _activate()
    import concurrent.futures as cf
    import concurrent.futures.process as cfp
    orig_submit = cfp.ProcessPoolExecutor.submit

    def submit(self, fn, /, *args, **kwargs):
        ctx = _cur["ctx"]
        if "#" not in ctx:
            n = _items.get(ctx, 0)
            _items[ctx] = n + 1
            ctx = f"{ctx}#{n}"
        fut = orig_submit(self, _cov_wrapper, ctx, fn, *args, **kwargs)
        fut._br_ctx = ctx
        return fut

    cfp.ProcessPoolExecutor.submit = submit

    orig_result = cf.Future.result

    def result(self, timeout=None):
        r = orig_result(self, timeout)
        c = getattr(self, "_br_ctx", None)
        if c is not None and _get_ident() == MAIN and c.split("#")[0] == _base():
            _set_ctx(c)
        return r

    cf.Future.result = result


def _patch_tests():
    import tests.helpers as helpers
    origs = {}
    for name in ("load_sql_fixture_pairs", "load_sql_fixtures"):
        f = getattr(helpers, name)
        if not getattr(f, "_br_wrapped", False):
            origs[f] = _wrap_loader(f)
            setattr(helpers, name, origs[f])
    for modname, mod in list(sys.modules.items()):
        if not modname.startswith("tests") or mod is None:
            continue
        for name in ("load_sql_fixture_pairs", "load_sql_fixtures"):
            f = getattr(mod, name, None)
            if f in origs:
                setattr(mod, name, origs[f])
    try:
        import tests.test_optimizer as to
        orig_check = to.TestOptimizer.check_file

        def check_file(self, *a, **k):
            base = _base()
            try:
                return orig_check(self, *a, **k)
            finally:
                if _base() == base:
                    _set_ctx(base)

        to.TestOptimizer.check_file = check_file
    except Exception as e:
        print("no test_optimizer", e, file=sys.stderr)
    try:
        import tests.test_executor as te
    except Exception as e:
        print("no test_executor", e, file=sys.stderr)
        return
    from concurrent.futures import ProcessPoolExecutor
    from multiprocessing import get_context

    def _mp_execute(self, schema, tables, sqls, tpch):
        test = _cur["ctx"]
        with ProcessPoolExecutor(
            mp_context=get_context("spawn"), initializer=te.initializer, initargs=(schema, tables)
        ) as pool:
            futures = []
            for n, (args, sql, _) in enumerate(sqls):
                _set_ctx(f"{test}#{n}")
                e = te.parse_one(sql)
                futures.append(pool.submit(te.mp_execute, e, args))
            _set_ctx(test)
            for i, future in enumerate(futures):
                table = future.result()
                _set_ctx(test)
                if table is not None:
                    _set_ctx(f"{test}#{i}")
                    self.subtestHelper(i, table, tpch=tpch)
                    _set_ctx(test)
            _set_ctx(test)

    te.TestExecutor._mp_execute = _mp_execute


def pytest_collection_finish(session):
    _patch_tests()
    _set_ctx("<between>")


def pytest_runtest_setup(item):
    _set_ctx(item.nodeid)


def pytest_runtest_logfinish(nodeid, location):
    _set_ctx("<between>")


def pytest_sessionfinish(session, exitstatus):
    out = {ctx: _ser(rec) for ctx, rec in _rec.items()}
    with open(os.path.join(OUTDIR, f"main{os.getpid()}.json"), "w") as fh:
        json.dump(out, fh)
