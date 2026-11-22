"""Static analysis of a patch against the coverage map: which tests (and which of their
fixture items) can observe the change."""
from __future__ import annotations

import ast
import gzip
from array import array
import json
import marshal
import os
import re
from pathlib import Path

from patchutil import (
    none_compare_equivalent,
    apply_hunks,
    changed_old_lines,
    compiles,
    fixture_lines,
    fixture_pairs,
    parse_patch,
    python_equivalent,
)

FULL = None  # plan value meaning "the whole test"


def merge_into(plan: dict, add: dict) -> None:
    for t, items in add.items():
        if t in plan:
            cur = plan[t]
            if cur is FULL or items is FULL:
                plan[t] = FULL
            else:
                plan[t] = cur | items
        else:
            plan[t] = FULL if items is FULL else set(items)


def full_plan(tests) -> dict:
    return {t: FULL for t in tests}


class _LazyFiles(dict):
    """{file: marshalled per-file map}, decoded on first access."""

    def get(self, key, default=None):
        v = super().get(key)
        if v is None:
            return default
        if isinstance(v, bytes):
            v = marshal.loads(v)
            self[key] = v
        return v

    def __getitem__(self, key):
        v = self.get(key)
        if v is None:
            raise KeyError(key)
        return v

    def values(self):
        return [self.get(k) for k in self.keys()]

    def items(self):
        return [(k, self.get(k)) for k in self.keys()]


class _LazySets(list):
    """Pool of id sets stored as uint32 byte strings, decoded on access."""

    def __getitem__(self, i):
        v = list.__getitem__(self, i)
        if isinstance(v, bytes):
            a = array("I")
            a.frombytes(v)
            v = a.tolist()
            list.__setitem__(self, i, v)
        return v


def compact_maps(src: str, out: str) -> None:
    """Build step: store the map so a run decodes only the files it looks at."""
    with gzip.open(src, "rt") as fh:
        m = json.load(fh)
    m["lines"] = {f: marshal.dumps(v) for f, v in m["lines"].items()}
    m["funcs"] = {f: marshal.dumps(v) for f, v in m["funcs"].items()}
    cmp_by_file: dict = {}
    for site, v in (m.get("cmp") or {}).items():
        cmp_by_file.setdefault(site.split(":")[0], {})[site] = v
    m["cmp_files"] = {f: marshal.dumps(v) for f, v in cmp_by_file.items()}
    m.pop("cmp", None)
    m["sets"] = [array("I", v).tobytes() for v in m["sets"]]
    m["format"] = "compact1"
    with open(out, "wb") as fh:
        marshal.dump(m, fh)


class Maps:
    def __init__(self, path: Path, tests: list[dict]):
        if str(path).endswith(".marshal"):
            with open(path, "rb") as fh:
                m = marshal.load(fh)
            m["lines"] = _LazyFiles(m["lines"])
            m["funcs"] = _LazyFiles(m["funcs"])
            self._cmp_files = _LazyFiles(m.pop("cmp_files"))
            m["cmp"] = {}
            m["sets"] = _LazySets(m["sets"])
        else:
            with gzip.open(path, "rt") as fh:
                m = json.load(fh)
            self._cmp_files = None
        self.number_by_id = {t["id"]: t["number"] for t in tests}
        self.id_by_number = {t["number"]: t["id"] for t in tests}
        self.trans = [self.number_by_id.get(i) for i in m["ids"]]
        self.raw_sets = m["sets"]
        self._tsets = {}
        self.lines = m["lines"]
        self.funcs = m["funcs"]
        self.reads = m["reads"]
        self.classes = m["classes"]
        self.classes_glob = m.get("classes_glob")
        self.classes_items = m.get("classes_items") or {}
        self.cmp = m.get("cmp")
        self.cmp_nontest = set(m.get("cmp_nontest") or ())
        self.items_count = {self.trans[int(k)]: v for k, v in m["items"].items() if self.trans[int(k)] is not None}
        self.import_lines = {f: set(v) for f, v in m["import_lines"].items()}
        self.dialect_files = m["dialect_files"]
        self.all_numbers = frozenset(self.number_by_id.values())
        self.by_test_file: dict[str, set[int]] = {}
        for t in tests:
            self.by_test_file.setdefault(t["file"], set()).add(t["number"])
        mapped = set(m["ids"])
        self.unmapped = frozenset(n for i, n in self.number_by_id.items() if i not in mapped)

    def tests_of(self, sid: int) -> frozenset:
        s = self._tsets.get(sid)
        if s is None:
            s = frozenset(self.trans[i] for i in self.raw_sets[sid] if self.trans[i] is not None)
            self._tsets[sid] = s
        return s

    def items_of(self, sid: int) -> set:
        return set(self.raw_sets[sid])

    def _add_entry(self, plan: dict, e) -> None:
        """Merge one [all, glob, {test: items}] map entry into a plan."""
        glob = self.tests_of(e[1])
        itemmap = e[2] if len(e) > 2 else {}
        for t in self.tests_of(e[0]):
            if t in glob:
                plan[t] = FULL
        for tidx, sid in itemmap.items():
            t = self.trans[int(tidx)]
            if t is None or plan.get(t, 0) is FULL:
                continue
            plan.setdefault(t, set()).update(self.items_of(sid))
        # tests in "all" but neither glob nor item-resolved (should not happen): full
        for t in self.tests_of(e[0]):
            if t not in plan:
                plan[t] = FULL

    def cover_lines(self, path: str, lines) -> dict:
        """Plan of tests that executed any of the lines (with item subsets where known)."""
        plan: dict = {}
        fl = self.lines.get(path)
        if not fl:
            return plan
        for l in lines:
            e = fl.get(str(l))
            if e:
                self._add_entry(plan, e)
        return plan

    def cmp_plan(self, site: str, classes) -> dict | None:
        """Plan of tests/items in which the comparison at site saw one of the outcome classes
        (None when the profile cannot answer for this site)."""
        if self.cmp is None or site in self.cmp_nontest:
            return None
        plan: dict = {}
        if self._cmp_files is not None:
            entries = (self._cmp_files.get(site.split(":")[0]) or {}).get(site) or {}
        else:
            entries = self.cmp.get(site) or {}
        for c in classes:
            e = entries.get(c)
            if e:
                self._add_entry(plan, e)
        return plan

    def class_plan(self, key: str) -> dict | None:
        """Tests (and fixture items) that instantiated the class."""
        sid = self.classes.get(key)
        if sid is None:
            return None
        if self.classes_glob is None or key.startswith("sqlglot.expressions"):
            # expression nodes also appear through pickling/copying: whole tests only
            return full_plan(self.tests_of(sid))
        plan = full_plan(self.tests_of(self.classes_glob[key])) if key in self.classes_glob else {}
        for tidx, isid in self.classes_items.get(key, {}).items():
            t = self.trans[int(tidx)]
            if t is None or plan.get(t, 0) is FULL:
                continue
            plan.setdefault(t, set()).update(self.items_of(isid))
        for t in self.tests_of(sid):
            if t not in plan:
                plan[t] = FULL
        return plan

    def executed_at_runtime(self, path: str, line: int) -> bool:
        fl = self.lines.get(path)
        return bool(fl and str(line) in fl)

    def file_tests(self, path: str) -> set:
        fl = self.lines.get(path) or {}
        res = set()
        seen = set()
        for e in fl.values():
            if e[0] not in seen:
                seen.add(e[0])
                res |= self.tests_of(e[0])
        return res

    def class_tests(self, key: str):
        sid = self.classes.get(key)
        return None if sid is None else self.tests_of(sid)

    def read_tests(self, path: str) -> set:
        sid = self.reads.get(path)
        return set() if sid is None else set(self.tests_of(sid))


# ---------------------------------------------------------------- source index

class SrcIndex:
    def __init__(self, src: str):
        self.tree = ast.parse(src)
        self.funcs = []    # (body_start, end, node)
        self.lambdas = []  # (start, end, node)
        self.classes = []  # (start, end, node, qualname)
        self.parent = {}

        def visit(node, qual):
            for child in ast.iter_child_nodes(node):
                self.parent[child] = node
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self.funcs.append((child.body[0].lineno, child.end_lineno, child))
                    visit(child, qual + [child.name, "<locals>"])
                elif isinstance(child, ast.ClassDef):
                    self.classes.append((child.lineno, child.end_lineno, child, ".".join(qual + [child.name])))
                    visit(child, qual + [child.name])
                elif isinstance(child, ast.Lambda):
                    self.lambdas.append((child.lineno, child.end_lineno, child))
                    visit(child, qual)
                else:
                    visit(child, qual)

        visit(self.tree, [])

    def innermost(self, items, line):
        best = None
        for it in items:
            if it[0] <= line <= it[1] and (best is None or it[0] >= best[0]):
                best = it
        return best

    def stmt_in(self, body, line):
        """Innermost statement within a list of statements containing line (recursive)."""
        for st in body:
            if st.lineno <= line <= st.end_lineno:
                # descend into compound bodies
                for field in ("body", "orelse", "finalbody", "handlers", "cases"):
                    sub = getattr(st, field, None)
                    if not sub:
                        continue
                    if field == "handlers":
                        for h in sub:
                            if h.body and h.body[0].lineno <= line <= h.end_lineno:
                                r = self.stmt_in(h.body, line)
                                if r is not None:
                                    return r
                    elif field == "cases":
                        for c in sub:
                            if c.body and c.body[0].lineno <= line <= c.end_lineno:
                                r = self.stmt_in(c.body, line)
                                if r is not None:
                                    return r
                    elif isinstance(sub, list) and sub and isinstance(sub[0], ast.stmt):
                        if sub[0].lineno <= line <= sub[-1].end_lineno:
                            r = self.stmt_in(sub, line)
                            if r is not None:
                                return r
                return st
        return None

    def locate(self, line: int) -> dict:
        lam = self.innermost(self.lambdas, line)
        fn = self.innermost(self.funcs, line)
        # a decorator/signature line belongs to the def statement, not its body
        if lam is not None and (fn is None or lam[0] >= fn[0]):
            return {"kind": "func", "lines": range(lam[0], lam[1] + 1)}
        if fn is not None:
            node = fn[2]
            st = self.stmt_in(node.body, line)
            if st is None:
                return {"kind": "none"}
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if line < (st.body[0].lineno if st.body else st.end_lineno + 1):
                    # nested def header: executed when the outer function runs
                    first = min([st.lineno] + [d.lineno for d in st.decorator_list])
                    return {"kind": "stmt", "lines": range(first, st.body[0].lineno)}
            return {"kind": "stmt", "lines": range(st.lineno, st.end_lineno + 1)}
        # not inside any function body: maybe a def header (decorators/signature)
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                first = min([node.lineno] + [d.lineno for d in node.decorator_list])
                if first <= line < node.body[0].lineno:
                    return {"kind": "funcdef", "lines": range(node.body[0].lineno, node.end_lineno + 1),
                            "first": first, "node": node}
        cls = self.innermost(self.classes, line)
        if cls is not None:
            node = cls[2]
            st = self.stmt_in(node.body, line) if node.body and node.body[0].lineno <= line else None
            return {"kind": "class", "qualname": cls[3], "stmt": st, "node": node}
        st = self.stmt_in(self.tree.body, line)
        return {"kind": "module", "stmt": st}


def assigned_names(st) -> list[str]:
    names = []
    if isinstance(st, ast.Assign):
        targets = st.targets
    elif isinstance(st, (ast.AnnAssign, ast.AugAssign)):
        targets = [st.target]
    elif isinstance(st, (ast.Import, ast.ImportFrom)):
        return [(a.asname or a.name).split(".")[0] for a in st.names]
    else:
        return []
    for t in targets:
        for n in ast.walk(t):
            if isinstance(n, ast.Name):
                names.append(n.id)
            elif isinstance(n, ast.Attribute):
                names.append(n.attr)
    return names


class Analyzer:
    def __init__(self, maps: Maps, root: Path):
        self.maps = maps
        self.root = root
        self._src = {}
        self._idx = {}
        self._all_sources = None
        self.touched = []      # [(rel, lines)] statement-level changes of the last analyze()
        self.nonlocal_change = False

    def source(self, rel: str) -> str:
        if rel not in self._src:
            p = self.root / rel
            self._src[rel] = p.read_text(encoding="utf-8") if p.exists() else ""
        return self._src[rel]

    def index(self, rel: str):
        if rel not in self._idx:
            try:
                self._idx[rel] = SrcIndex(self.source(rel))
            except SyntaxError:
                self._idx[rel] = None
        return self._idx[rel]

    def all_sources(self):
        if self._all_sources is None:
            res = []
            for p in sorted((self.root / "sqlglot").rglob("*.py")):
                rel = str(p.relative_to(self.root))
                res.append((rel, self.source(rel)))
            self._all_sources = res
        return self._all_sources

    # ---- name references ---------------------------------------------------
    def name_refs(self, names, def_path, def_lines):
        """Plan for runtime references to the names, or None when some reference runs at
        import time (class bodies, metaclasses, module level) and refinement is unsafe."""
        plan: dict = {}
        pat = re.compile(r"\b(" + "|".join(re.escape(n) for n in set(names)) + r")\b")
        def_lines = set(def_lines)
        for rel, src in self.all_sources():
            if not pat.search(src):
                continue
            imp = self.maps.import_lines.get(rel, set())
            for i, text in enumerate(src.split("\n"), start=1):
                if rel == def_path and i in def_lines:
                    continue
                if not pat.search(text):
                    continue
                if text.lstrip().startswith("#"):
                    continue
                idx = self.index(rel)
                if idx is None:
                    return None
                loc = idx.locate(i)
                if loc["kind"] == "class" and _is_plain_override(loc.get("stmt"), names):
                    continue
                if i in imp:
                    return None
                if loc["kind"] in ("class", "module", "funcdef"):
                    return None
                if loc["kind"] == "none":
                    continue
                merge_into(plan, self.maps.cover_lines(rel, loc["lines"]))
        return plan

    def dialect_users(self, rel: str) -> dict:
        plan = {}
        mod = rel[:-3].replace("/", ".")
        for key, sid in self.maps.classes.items():
            if key.startswith(mod + ":"):
                merge_into(plan, full_plan(self.maps.tests_of(sid)))
        return plan

    def module_fallback(self, rel: str) -> dict:
        plan = full_plan(self.maps.file_tests(rel))
        merge_into(plan, self.dialect_users(rel))
        return plan

    # ---- per change -----------------------------------------------------------
    def py_change(self, rel: str, original: str, patched: str | None, hunks) -> tuple[dict, list]:
        notes = []
        added = [l[1:] for h in hunks for l in h["lines"] if l[:1] == "+"]
        removed = [l[1:] for h in hunks for l in h["lines"] if l[:1] == "-"]
        # whole-file checks are costly on big modules: only run them when they could apply
        maybe_comment = all("#" in l or not l.strip() for l in added)
        maybe_none = any("None" in l for l in added + removed)
        if patched is not None and maybe_comment and python_equivalent(original, patched):
            return {}, [f"{rel}: comment-only"]
        if (patched is not None and maybe_none and rel != "sqlglot/executor/env.py"
                and none_compare_equivalent(original, patched)):
            return {}, [f"{rel}: None-comparison swap (equivalent)"]
        if patched is None or not compiles(patched, rel):
            self.nonlocal_change = True
            notes.append(f"{rel}: does not apply/compile")
            plan = self.module_fallback(rel)
            if not any(seg in rel for seg in ("/dialects/", "/generators/", "/parsers/", "/tokenizers/", "/typing/")):
                plan = full_plan(self.maps.all_numbers)
            return plan, notes
        idx = self.index(rel)
        if idx is None:
            return self.module_fallback(rel), [f"{rel}: original unparsable"]
        swap = None
        if len(added) == 1 and len(removed) == 1 and _OP_RE.search(removed[0]) and _OP_RE.search(added[0]):
            swap = cmp_swap(original, patched, idx.tree)
        if swap is not None:
            site, line, classes = swap
            loc = idx.locate(line)
            if loc["kind"] in ("func", "stmt", "funcdef"):
                plan = self.maps.cmp_plan(f"{rel}:{site}", classes)
                if plan is not None:
                    self.touched.append((rel, [line]))
                    return plan, [f"{rel}:{line} comparison swap {site} -> {len(plan)} infected"]
        plan: dict = {}
        imp = self.maps.import_lines.get(rel, set())
        for tag, line in changed_old_lines(hunks):
            locs = [idx.locate(line)]
            if tag == "+":
                locs.append(idx.locate(line - 1))
            for loc in locs:
                kind = loc["kind"]
                if kind in ("func", "stmt", "funcdef"):
                    merge_into(plan, self.maps.cover_lines(rel, loc["lines"]))
                    self.touched.append((rel, list(loc["lines"])))
                    notes.append(f"{rel}:{line} {kind}")
                    if imp and any(l in imp for l in loc["lines"]):
                        # also runs while modules are imported: whatever it computes there
                        # (type tables, tries, registries) is shared by every test
                        self.nonlocal_change = True
                        merge_into(plan, full_plan(self.maps.all_numbers))
                        notes.append(f"{rel}:{line} runs at import time")
                elif kind == "none":
                    continue
                elif kind == "class":
                    self.nonlocal_change = True
                    merge_into(plan, self.class_change(rel, loc, line))
                    notes.append(f"{rel}:{line} class {loc['qualname']}")
                else:
                    self.nonlocal_change = True
                    merge_into(plan, self.module_change(rel, loc, line))
                    notes.append(f"{rel}:{line} module")
        return plan, notes

    def class_change(self, rel, loc, line) -> dict:
        mod = rel[:-3].replace("/", ".")
        key = f"{mod}:{loc['qualname']}"
        inst = self.maps.class_plan(key)
        if inst is None:
            return self.module_fallback(rel)
        st = loc["stmt"]
        names = assigned_names(st) if st is not None else []
        if names:
            refs = self.name_refs(names, rel, range(st.lineno, st.end_lineno + 1))
            if refs is not None:
                # an item observes the change only if it both uses the class and runs a reference
                res = {}
                for t, v in refs.items():
                    if t not in inst:
                        continue
                    w = inst[t]
                    if w is FULL:
                        res[t] = v
                    elif v is FULL:
                        res[t] = set(w)
                    elif v & w:
                        res[t] = v & w
                return res
        return inst

    def module_change(self, rel, loc, line) -> dict:
        st = loc["stmt"]
        names = assigned_names(st) if st is not None else []
        if names:
            refs = self.name_refs(names, rel, range(st.lineno, st.end_lineno + 1))
            if refs is not None:
                return refs
        if st is None:
            return {}
        return self.module_fallback(rel)

    def fixture_change(self, rel, original, patched) -> tuple[dict, list]:
        readers = self.maps.read_tests(rel)
        notes = []
        if patched is None:
            return full_plan(readers | self.tpc_readers(rel)), [f"{rel}: fixture patch failed"]
        if rel.endswith(".sql"):
            try:
                a_pairs, b_pairs = fixture_pairs(original), fixture_pairs(patched)
                a_lines, b_lines = fixture_lines(original), fixture_lines(patched)
            except Exception:
                return full_plan(readers | self.tpc_readers(rel)), [f"{rel}: fixture parse failed"]

            def strip(pairs):
                return [({k: v for k, v in m.items() if k != "title"}, s, e) for m, s, e in pairs]

            changed_pairs = diff_indices(strip(a_pairs), strip(b_pairs))
            changed_titles = diff_indices(a_pairs, b_pairs)
            changed_lines = diff_indices(a_lines, b_lines)
            if not changed_pairs and not changed_lines:
                return {}, [f"{rel}: fixture no-op"]
            plan = {}
            idx_pairs = set(changed_pairs) | set(changed_titles)
            for t in readers | self.tpc_readers(rel):
                tid = self.maps.id_by_number[t]
                # which loader the test uses decides the indexing; record both
                plan[t] = {"pairs": idx_pairs, "lines": set(changed_lines), "file": rel}
            notes.append(f"{rel}: fixture items pairs={sorted(idx_pairs)[:5]} lines={sorted(changed_lines)[:5]}")
            return plan, notes
        return full_plan(readers | self.tpc_readers(rel)), [f"{rel}: data file"]

    def tpc_readers(self, rel) -> set:
        res = set()
        n = self.maps.number_by_id.get
        if "tpc-ds" in rel:
            if rel.endswith(".sql"):
                res |= {n("tests/test_optimizer.py::TestOptimizer::test_tpcds"),
                        n("tests/test_executor.py::TestExecutor::test_execute_tpcds")}
            else:
                res |= self.maps.by_test_file.get("tests/test_executor.py", set())
        elif "tpc-h" in rel:
            if rel.endswith(".sql"):
                res |= {n("tests/test_optimizer.py::TestOptimizer::test_tpch"),
                        n("tests/test_executor.py::TestExecutor::test_execute_tpch"),
                        n("tests/test_executor.py::TestExecutor::test_optimized_tpch")}
            else:
                res |= self.maps.by_test_file.get("tests/test_executor.py", set())
        res.discard(None)
        return res

    def test_code_change(self, rel, original, hunks) -> tuple[dict, list]:
        file_tests = set(self.maps.by_test_file.get(rel, set()))
        if rel == "tests/helpers.py" or not file_tests:
            return full_plan(self.maps.all_numbers if rel.endswith("helpers.py") else file_tests), [f"{rel}: test support"]
        hit = set()
        try:
            tree = ast.parse(original)
            methods = []
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    for item in node.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            methods.append((item, node.name))
            for tag, line in changed_old_lines(hunks):
                owner = None
                for item, cname in methods:
                    first = min([item.lineno] + [d.lineno for d in item.decorator_list])
                    if first <= line <= item.end_lineno:
                        owner = (item, cname)
                if owner and owner[0].name.startswith("test"):
                    tid = f"{rel}::{owner[1]}::{owner[0].name}"
                    if tid in self.maps.number_by_id:
                        hit.add(self.maps.number_by_id[tid])
                        continue
                hit = file_tests
                break
        except SyntaxError:
            hit = file_tests
        return full_plan(hit), [f"{rel}: test code {len(hit)}"]

    def analyze(self, patch_text: str) -> tuple[dict, list, dict]:
        self.touched = []
        self.nonlocal_change = False
        plan: dict = {}
        notes: list = []
        files = {}
        ok_overlay = True
        for f in parse_patch(patch_text):
            rel = f["path"]
            old = f["old"]
            original = self.source(old) if old else ""
            patched = apply_hunks(original, f["hunks"]) if f["hunks"] else original
            if f["old"] != f["new"] or patched is None:
                ok_overlay = False
            else:
                files[rel] = patched
            if rel.startswith("sqlglot/") and rel.endswith(".py"):
                p, n = self.py_change(rel, original, patched, f["hunks"])
            elif rel.startswith("tests/fixtures/"):
                p, n = self.fixture_change(rel, original, patched)
                if p:
                    self.nonlocal_change = True
            elif rel.startswith("tests/") and rel.endswith(".py"):
                self.nonlocal_change = True
                p, n = self.test_code_change(rel, original, f["hunks"])
            else:
                self.nonlocal_change = True
                p, n = full_plan(self.maps.read_tests(rel)), [f"{rel}: other file"]
            merge_into_plan_any(plan, p)
            notes += n
        return plan, notes, (files if ok_overlay else None)


_SWAPS = {
    (ast.Is, ast.Eq): ("EQNI", "ISNE", "ERR"), (ast.Eq, ast.Is): ("EQNI", "ISNE", "ERR"),
    (ast.IsNot, ast.NotEq): ("EQNI", "ISNE", "ERR"), (ast.NotEq, ast.IsNot): ("EQNI", "ISNE", "ERR"),
    (ast.Lt, ast.LtE): ("EQ", "ERR"), (ast.LtE, ast.Lt): ("EQ", "ERR"),
    (ast.Gt, ast.GtE): ("EQ", "ERR"), (ast.GtE, ast.Gt): ("EQ", "ERR"),
}
_OP_RE = re.compile(r"==|!=|<|>|\bis\b")
_OPNAME = {ast.Is: "is", ast.IsNot: "in", ast.Eq: "eq", ast.NotEq: "ne", ast.Lt: "lt", ast.LtE: "le",
           ast.Gt: "gt", ast.GtE: "ge"}


def cmp_swap(original: str, patched: str, orig_tree=None):
    """If the patch only swaps the operator of one profiled comparison between two operators
    that differ on a single outcome class (is/==, is not/!=, </<=, >/>=), return
    ("line:col:op", line, outcome classes that tell them apart)."""
    a = original.split("\n")
    b = patched.split("\n")
    if len(a) != len(b):
        return None
    diff = [i for i in range(len(a)) if a[i] != b[i]]
    if len(diff) != 1:
        return None
    line = diff[0] + 1
    try:
        ta = orig_tree if orig_tree is not None else ast.parse(original)
        tb = ast.parse(patched)
    except SyntaxError:
        return None
    ca = {(n.lineno, n.col_offset): n for n in ast.walk(ta) if isinstance(n, ast.Compare) and n.lineno == line}
    cb = {(n.lineno, n.col_offset): n for n in ast.walk(tb) if isinstance(n, ast.Compare) and n.lineno == line}
    found = None
    for k, na in ca.items():
        nb = cb.get(k)
        if nb is None or len(na.ops) != 1 or len(nb.ops) != 1:
            continue
        pair = (type(na.ops[0]), type(nb.ops[0]))
        if pair[0] is pair[1] or pair not in _SWAPS:
            continue
        if any(isinstance(x, ast.Constant) and x.value is None for x in (na.left, na.comparators[0])):
            continue
        if found is not None:
            return None
        found = (k, na, nb, pair)
    if found is None:
        return None
    k, na, nb, pair = found
    # the rest of the line must be unchanged: same text before the operator and after it
    la, lb = a[line - 1].encode("utf-8"), b[line - 1].encode("utf-8")  # offsets are in bytes
    for n in (na.left, na.comparators[0], nb.left, nb.comparators[0]):
        if n.lineno != line or n.end_lineno != line:
            return None
    if (la[: na.left.end_col_offset] != lb[: nb.left.end_col_offset]
            or la[na.comparators[0].col_offset:] != lb[nb.comparators[0].col_offset:]
            or ast.dump(na.left) != ast.dump(nb.left)
            or ast.dump(na.comparators[0]) != ast.dump(nb.comparators[0])):
        return None
    return f"{k[0]}:{k[1]}:{_OPNAME[pair[0]]}", line, _SWAPS[pair]


def merge_into_plan_any(plan, add):
    """Merge plans whose item values may be sets (pool items) or dicts (fixture items)."""
    for t, v in add.items():
        cur = plan.get(t, "missing")
        if cur == "missing":
            plan[t] = v
        elif cur is FULL or v is FULL:
            plan[t] = FULL
        elif isinstance(cur, set) and isinstance(v, set):
            plan[t] = cur | v
        else:
            plan[t] = FULL  # mixing kinds: run the whole test


def diff_indices(a, b) -> list[int]:
    n = min(len(a), len(b))
    res = [i for i in range(n) if a[i] != b[i]]
    if len(a) != len(b):
        res += list(range(n, max(len(a), len(b))))
    return res


def _is_plain_override(st, names) -> bool:
    """A class-body assignment that rebinds one of the names without reading any of them."""
    if not isinstance(st, (ast.Assign, ast.AnnAssign)):
        return False
    targets = st.targets if isinstance(st, ast.Assign) else [st.target]
    tnames = {t.id for t in targets if isinstance(t, ast.Name)}
    if not tnames or not tnames & set(names):
        return False
    value = st.value
    if value is None:
        return True
    used = {n.id for n in ast.walk(value) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(value) if isinstance(n, ast.Attribute)}
    return not used & set(names)
