"""Blast Radius selector.

For each patch, find the tests that can observe the change (coverage maps built from the
pinned sqlglot commit), and print them split across workers.
"""
from __future__ import annotations

import gzip
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from patchutil import (  # noqa: E402
    FuncIndex,
    apply_hunks,
    changed_old_lines,
    compiles,
    fixture_lines,
    fixture_pairs,
    parse_patch,
    python_equivalent,
)

DATA = Path(os.environ.get("BR_DATA", "/data"))
SQLGLOT = Path(os.environ.get("BR_SQLGLOT", "/sqlglot"))
MAPS = Path(os.environ.get("BR_MAPS", os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps.json.gz")))
T0 = time.time()


def log(*a):
    print(f"[{time.time() - T0:6.2f}]", *a, file=sys.stderr, flush=True)


class Maps:
    def __init__(self, path: Path, tests: list[dict]):
        with gzip.open(path, "rt") as fh:
            m = json.load(fh)
        number_by_id = {t["id"]: t["number"] for t in tests}
        # translate our test indices into this run's numbers
        trans = [number_by_id.get(i) for i in m["ids"]]
        self.sets = [frozenset(trans[i] for i in s if trans[i] is not None) for s in m["sets"]]
        self.funcs = m["funcs"]
        self.files = m["files"]
        self.reads = m["reads"]
        self.dialects = m["dialects"]
        self.lines = m["lines"]
        self.all_numbers = frozenset(t["number"] for t in tests)
        # tests per test file
        self.by_test_file: dict[str, set[int]] = {}
        for t in tests:
            self.by_test_file.setdefault(t["file"], set()).add(t["number"])
        self.id_by_number = {t["number"]: t["id"] for t in tests}
        self.number_by_id = number_by_id
        # file-level union by directory prefix
        self.unmapped = frozenset(n for n in self.all_numbers if self.id_by_number[n] not in set(m["ids"]))

    def get(self, table: dict, key) -> frozenset:
        s = table.get(key)
        return self.sets[s] if s is not None else frozenset()

    def file_tests(self, path: str) -> frozenset:
        return self.get(self.files, path)

    def func_tests(self, path: str, firstline: int) -> frozenset:
        return self.get(self.funcs, f"{path}:{firstline}")


# Dialect families: a module-level change in a dialect's files reaches its subclasses too.
def dialect_name_of(path: str) -> str | None:
    parts = path.split("/")
    if len(parts) == 3 and parts[0] == "sqlglot" and parts[1] in (
        "dialects", "generators", "parsers", "tokenizers", "typing"
    ):
        name = parts[2][:-3]
        if name in ("__init__", "dialect"):
            return None
        return name
    return None


def module_level_candidates(maps: Maps, path: str) -> set[int]:
    cand = set(maps.file_tests(path))
    name = dialect_name_of(path)
    if name:
        # every test that instantiated a dialect defined in a module whose name matches,
        # plus the files of the same dialect
        for d in ("dialects", "generators", "parsers", "tokenizers", "typing"):
            cand |= maps.file_tests(f"sqlglot/{d}/{name}.py")
        for key, sid in maps.dialects.items():
            if key.startswith(f"sqlglot.dialects.{name}."):
                cand |= maps.sets[sid]
        # subclasses: any dialect whose files import this one show up via file coverage of
        # the base module when they run, which file_tests already includes
    else:
        # a core module: anything that executed code in it
        pass
    return cand


def analyze_commit(maps: Maps, patch_text: str) -> dict:
    """Return {'cand': set, 'notes': [...], 'certain': set} for one patch."""
    cand: set[int] = set()
    certain: set[int] = set()
    notes: list[str] = []
    for f in parse_patch(patch_text):
        path = f["path"]
        old_path = f["old"]
        orig_file = SQLGLOT / old_path if old_path else None
        try:
            original = orig_file.read_text(encoding="utf-8") if orig_file and orig_file.exists() else ""
        except Exception:
            original = ""
        patched = apply_hunks(original, f["hunks"]) if f["hunks"] else original
        if path.startswith("sqlglot/") and path.endswith(".py"):
            if patched is not None and f["old"] == f["new"] and python_equivalent(original, patched):
                notes.append(f"{path}: comment-only")
                continue
            if patched is not None and not compiles(patched, path):
                notes.append(f"{path}: syntax error -> everything importing it")
                c = maps.file_tests(path) | module_level_candidates(maps, path)
                if not dialect_name_of(path):
                    c = set(maps.all_numbers)
                cand |= c
                certain |= c
                continue
            try:
                idx = FuncIndex(original)
            except SyntaxError:
                idx = None
            for tag, line in changed_old_lines(f["hunks"]):
                enc = idx.enclosing(line) if idx else None
                if enc is None and tag == "+" and idx:
                    # insertion at the start of a block: look at the line before
                    enc = idx.enclosing(line - 1)
                    if enc is not None:
                        # make sure line-1 is really inside the function body
                        pass
                if enc is not None:
                    cand |= maps.func_tests(path, enc[0])
                    notes.append(f"{path}:{line} func@{enc[0]}")
                else:
                    cand |= module_level_candidates(maps, path)
                    notes.append(f"{path}:{line} module-level")
        elif path.startswith("tests/fixtures/"):
            readers = set(maps.get(maps.reads, path))
            if path.startswith("tests/fixtures/optimizer/tpc-"):
                readers |= tpc_fixture_tests(maps, path, original, patched)
            if patched is not None and not path.endswith((".gz", ".json")):
                try:
                    same = fixture_pairs(original) == fixture_pairs(patched) and fixture_lines(original) == fixture_lines(patched)
                    # titles do not change outcomes
                    if not same:
                        a = [({k: v for k, v in m.items() if k != "title"}, s, e) for m, s, e in fixture_pairs(original)]
                        b = [({k: v for k, v in m.items() if k != "title"}, s, e) for m, s, e in fixture_pairs(patched)]
                        if a == b and fixture_lines(original) == fixture_lines(patched):
                            same = True
                except Exception:
                    same = False
                if same:
                    notes.append(f"{path}: fixture no-op")
                    continue
            notes.append(f"{path}: fixture readers {len(readers)}")
            cand |= readers
            if len(readers) <= 2:
                certain |= readers
        elif path.startswith("tests/") and path.endswith(".py"):
            # a change to test code: the tests of that file (all of them if module level)
            file_tests = set(maps.by_test_file.get(path, set()))
            hit = set()
            try:
                idx = FuncIndex(original)
                for tag, line in changed_old_lines(f["hunks"]):
                    enc = idx.enclosing(line)
                    if enc is None:
                        hit = file_tests
                        break
                    # find the test method containing it by def line
                    import ast as _ast
                    tree = _ast.parse(original)
                    for node in _ast.walk(tree):
                        if isinstance(node, _ast.ClassDef):
                            for item in node.body:
                                if isinstance(item, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and item.lineno <= line <= item.end_lineno:
                                    if item.name.startswith("test"):
                                        tid = f"{path}::{node.name}::{item.name}"
                                        if tid in maps.number_by_id:
                                            hit.add(maps.number_by_id[tid])
                                    else:
                                        hit = file_tests
                if not hit:
                    hit = file_tests
            except Exception:
                hit = file_tests
            if path == "tests/helpers.py" or not file_tests:
                hit = set(maps.all_numbers)
            notes.append(f"{path}: test code {len(hit)}")
            cand |= hit
        else:
            readers = set(maps.get(maps.reads, path))
            notes.append(f"{path}: other file readers {len(readers)}")
            cand |= readers
    return {"cand": cand, "certain": certain, "notes": notes}


def tpc_fixture_tests(maps: Maps, path: str, original: str, patched: str | None) -> set[int]:
    res = set()
    def num(i):
        return maps.number_by_id.get(i)
    if "tpc-ds" in path:
        if path.endswith(".sql"):
            res |= {num("tests/test_optimizer.py::TestOptimizer::test_tpcds")}
            res |= {num("tests/test_executor.py::TestExecutor::test_execute_tpcds")}
        else:
            res |= {n for n in maps.by_test_file.get("tests/test_executor.py", set())}
    else:
        if path.endswith(".sql"):
            res |= {num("tests/test_optimizer.py::TestOptimizer::test_tpch"),
                    num("tests/test_executor.py::TestExecutor::test_execute_tpch"),
                    num("tests/test_executor.py::TestExecutor::test_optimized_tpch")}
        else:
            res |= {n for n in maps.by_test_file.get("tests/test_executor.py", set())}
    res.discard(None)
    return res


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


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    duration_ms = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
    maps = Maps(MAPS, tests)
    log("maps loaded")
    patches = sorted((DATA / "commits").glob("*.patch"))
    out = []
    for p in patches:
        commit = p.name[: -len(".patch")]
        try:
            res = analyze_commit(maps, p.read_text(encoding="utf-8", errors="replace"))
            sel = res["cand"] | set(maps.unmapped & res["cand"])
            log(commit, len(sel), res["notes"][:4])
        except Exception as e:  # never fail the run: fall back to everything
            log(commit, "analysis failed", repr(e))
            sel = set(maps.all_numbers)
        sel &= maps.all_numbers
        out.append(json.dumps({"commit": commit, "workers": split(sel, duration_ms)}))
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))
    sys.stdout.flush()
    log("done")


if __name__ == "__main__":
    main()
