"""Generate random sqlglot mutants (kit-like patches) and their ground truth via the real suite.

usage: mkmut.py TREE OUTDIR N SEED
"""
import ast
import difflib
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tokenize
from pathlib import Path

TREE = Path(sys.argv[1])
OUT = Path(sys.argv[2])
N = int(sys.argv[3])
SEED = int(sys.argv[4])
PY = sys.executable
S = Path(__file__).resolve().parent.parent
rng = random.Random(SEED)

(OUT / "commits").mkdir(parents=True, exist_ok=True)
shutil.copy(S / "kit/sample/tests.json", OUT / "tests.json")
tests = json.load(open(OUT / "tests.json"))["tests"]
num = {t["id"]: t["number"] for t in tests}

files = sorted(p for p in (TREE / "sqlglot").rglob("*.py") if "__pycache__" not in p.parts)
sizes = [max(1, p.stat().st_size) for p in files]

CATS = {"andor": 4, "cmp": 3, "is": 2, "bool": 3, "int": 3, "not": 1}


def candidates(src):
    toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    out = {k: [] for k in CATS}
    for i, t in enumerate(toks):
        if t.type == tokenize.NAME and t.string in ("and", "or"):
            out["andor"].append((t, "or" if t.string == "and" else "and", 1))
        elif t.type == tokenize.OP and t.string in ("==", "!=", "<", "<=", ">", ">="):
            new = {"==": "!=", "!=": "==", "<": "<=", "<=": "<", ">": ">=", ">=": ">"}[t.string]
            out["cmp"].append((t, new, 1))
        elif t.type == tokenize.NAME and t.string == "is":
            nxt = toks[i + 1] if i + 1 < len(toks) else None
            if nxt is not None and nxt.string == "not":
                out["is"].append((t, "!=", 2))
            else:
                out["is"].append((t, "==", 1))
        elif t.type == tokenize.NAME and t.string in ("True", "False"):
            out["bool"].append((t, "False" if t.string == "True" else "True", 1))
        elif t.type == tokenize.NUMBER and re.fullmatch(r"-?\d+", t.string):
            out["int"].append((t, str(int(t.string) + rng.choice((1, -1, 1))), 1))
        elif t.type == tokenize.NAME and t.string == "not":
            prev = toks[i - 1] if i else None
            nxt = toks[i + 1] if i + 1 < len(toks) else None
            if prev is not None and prev.string in ("is",):
                continue
            if nxt is not None and nxt.string == "in":
                continue
            out["not"].append((t, "", 1))
    return toks, out


def mutate(src):
    toks, cands = candidates(src)
    cats = [c for c in CATS if cands[c]]
    if not cats:
        return None
    cat = rng.choices(cats, [CATS[c] for c in cats])[0]
    t, new, span = rng.choice(cands[cat])
    lines = src.splitlines(keepends=True)
    row, col = t.start
    line = lines[row - 1]
    if span == 2:  # "is not" -> "!="
        m = re.compile(r"is\s+not").match(line, col)
        if not m:
            return None
        line2 = line[:col] + new + line[m.end():]
    elif cat == "not":
        m = re.compile(r"not\s+").match(line, col)
        if not m:
            return None
        line2 = line[:col] + line[m.end():]
    else:
        line2 = line[:col] + new + line[col + len(t.string):]
    if line2 == line:
        return None
    lines[row - 1] = line2
    new_src = "".join(lines)
    try:
        ast.parse(new_src)
    except SyntaxError:
        return None
    return cat, row, new_src


def run_suite():
    env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run([PY, "-m", "pytest", "-q", "-n", "4", "-p", "no:cacheprovider", "tests", "-rfE",
                        "--tb=no", "--timeout", "120"], cwd=TREE, env=env, capture_output=True, text=True)
    failing = set()
    for line in r.stdout.splitlines():
        if not re.match(r"^(FAILED|ERROR|SUBFAIL\w*)\b", line):
            continue
        m = re.search(r"(tests/\S+?\.py::\w+::\w+)", line)
        if m:
            failing.add(m.group(1))
            continue
        m = re.search(r"(tests/\S+?\.py)\b", line)
        if m:  # collection error: the whole file fails
            failing |= {t for t in num if t.startswith(m.group(1) + "::")}
    return failing, r.stdout.splitlines()[-1:] if r.stdout else []


answers_path = OUT / "answers.json"
answers = json.load(open(answers_path)) if answers_path.exists() else {"commits": {}}
made = len(answers["commits"])
while made < N:
    p = rng.choices(files, sizes)[0]
    rel = p.relative_to(TREE).as_posix()
    src = p.read_text()
    m = mutate(src)
    if m is None:
        continue
    cat, row, new_src = m
    cid = f"m{SEED:02d}{made:02d}"
    diff = difflib.unified_diff(src.splitlines(keepends=True), new_src.splitlines(keepends=True),
                                f"a/{rel}", f"b/{rel}", n=3)
    patch = f"diff --git a/{rel} b/{rel}\nindex 1111111..2222222 100644\n" + "".join(diff)
    pyc = p.parent / "__pycache__"
    try:
        p.write_text(new_src)
        if pyc.exists():
            for f in pyc.glob(p.stem + ".*.pyc"):
                f.unlink()
        failing, tail = run_suite()
    finally:
        p.write_text(src)
        if pyc.exists():
            for f in pyc.glob(p.stem + ".*.pyc"):
                f.unlink()
    (OUT / "commits" / f"{cid}.patch").write_text(patch)
    answers["commits"][cid] = {"failing": sorted(num[t] for t in failing if t in num), "kind": cat}
    json.dump(answers, open(answers_path, "w"))
    print(cid, cat, rel, row, len(failing), tail, flush=True)
    made += 1
