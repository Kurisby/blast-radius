"""Patch parsing and static classification of changes (no sqlglot import needed)."""
from __future__ import annotations

import ast
import io
import re
import tokenize

HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_patch(text: str) -> list[dict]:
    """Return [{'old': path|None, 'new': path|None, 'hunks': [...]}] for a unified diff."""
    files: list[dict] = []
    cur = None
    remaining_old = remaining_new = 0
    for line in text.split("\n"):
        if cur is not None and cur["hunks"] and (remaining_old > 0 or remaining_new > 0):
            tag = line[:1]
            if line == "":
                line, tag = " ", " "  # a blank context line that lost its leading space
            if tag == " ":
                remaining_old -= 1
                remaining_new -= 1
            elif tag == "-":
                remaining_old -= 1
            elif tag == "+":
                remaining_new -= 1
            elif tag == "\\":
                pass
            else:
                remaining_old = remaining_new = 0
                tag = None
            if tag is not None:
                cur["hunks"][-1]["lines"].append(line)
                continue
        if line.startswith("\\") and cur is not None and cur["hunks"]:
            cur["hunks"][-1]["lines"].append(line)
        elif line.startswith("diff --git "):
            cur = {"old": None, "new": None, "hunks": [], "header": line}
            files.append(cur)
        elif line.startswith("--- ") and cur is not None and not cur["hunks"]:
            p = line[4:].split("\t")[0]
            cur["old"] = None if p == "/dev/null" else (p[2:] if p.startswith("a/") else p)
        elif line.startswith("+++ ") and cur is not None and not cur["hunks"]:
            p = line[4:].split("\t")[0]
            cur["new"] = None if p == "/dev/null" else (p[2:] if p.startswith("b/") else p)
        elif line.startswith("@@") and cur is not None:
            m = HUNK.match(line)
            if m:
                remaining_old = int(m.group(2)) if m.group(2) is not None else 1
                remaining_new = int(m.group(4)) if m.group(4) is not None else 1
                cur["hunks"].append(
                    {"old_start": int(m.group(1)), "new_start": int(m.group(3)), "lines": []}
                )
    for f in files:
        if f["old"] is None and f["new"] is None:
            m = re.match(r"diff --git a/(\S+) b/(\S+)", f["header"])
            if m:
                f["old"], f["new"] = m.group(1), m.group(2)
        f["path"] = f["new"] or f["old"]
    return files


def apply_hunks(original: str, hunks: list[dict]) -> str | None:
    """Apply hunks to the original text. Returns None if context does not match."""
    src = original.split("\n")
    out: list[str] = []
    pos = 0  # index into src (0-based)
    for h in hunks:
        start = h["old_start"] - 1
        if h["old_start"] == 0:
            start = 0
        if start < pos:
            return None
        out.extend(src[pos:start])
        pos = start
        for l in h["lines"]:
            if l.startswith("\\"):
                continue
            tag, body = l[:1], l[1:]
            if tag == " ":
                if pos >= len(src) or src[pos] != body:
                    return None
                out.append(body)
                pos += 1
            elif tag == "-":
                if pos >= len(src) or src[pos] != body:
                    return None
                pos += 1
            elif tag == "+":
                out.append(body)
    out.extend(src[pos:])
    return "\n".join(out)


def changed_old_lines(hunks: list[dict]) -> list[tuple[str, int]]:
    """Old-file lines touched: ('-', n) for removed/replaced lines, ('+', n) for a pure
    insertion before old line n. Added lines right after removed ones replace them."""
    res = []
    for h in hunks:
        o = h["old_start"]
        prev_removed = False
        for l in h["lines"]:
            tag = l[:1]
            if tag == " ":
                o += 1
                prev_removed = False
            elif tag == "-":
                res.append(("-", o))
                o += 1
                prev_removed = True
            elif tag == "+":
                if not prev_removed:
                    res.append(("+", o))
    return res


def _sig_tokens(src: str):
    toks = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.NL):
            continue
        if tok.type == tokenize.NEWLINE:
            toks.append((tok.type, ""))
            continue
        toks.append((tok.type, tok.string))
    return toks


def python_equivalent(old: str, new: str) -> bool:
    """True if the two sources differ only in comments / blank lines (same token stream)."""
    try:
        if _sig_tokens(old) != _sig_tokens(new):
            return False
        # a coding cookie in a comment could change decoding; treat as non-equivalent
        for src in (old, new):
            for l in src.split("\n")[:2]:
                if re.match(r"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)", l):
                    return old.split("\n")[:2] == new.split("\n")[:2]
        return True
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return False


def compiles(src: str, filename: str) -> bool:
    try:
        compile(src, filename, "exec")
        return True
    except (SyntaxError, ValueError):
        return False


class FuncIndex:
    """Map source lines to the innermost enclosing function (key = first line incl decorators)."""

    def __init__(self, src: str):
        self.fns = []
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                first = min([node.lineno] + [d.lineno for d in node.decorator_list])
                self.fns.append((node.body[0].lineno, node.end_lineno, first, node.lineno))
            elif isinstance(node, ast.Lambda):
                self.fns.append((node.lineno, node.end_lineno, node.lineno, node.lineno))
        # signature lines (def line .. body start) count as module/class level: defaults are
        # evaluated at definition time

    def enclosing(self, line: int):
        best = None
        for bs, end, first, defline in self.fns:
            if bs <= line <= end and (best is None or first > best[0]):
                best = (first, defline)
        return best


# ---------------- fixtures ----------------

def _filter_comments(s):
    return "\n".join([line for line in s.splitlines() if line and not line.startswith("--")])


def _extract_meta(sql):
    meta = {}
    sql_lines = sql.split("\n")
    i = 0
    while i < len(sql_lines) and sql_lines[i].startswith("#"):
        if ":" not in sql_lines[i]:
            raise ValueError("bad meta")
        key, val = sql_lines[i].split(":", maxsplit=1)
        meta[key.lstrip("#").strip()] = val.strip()
        i += 1
    return "\n".join(sql_lines[i:]), meta


def fixture_pairs(text: str):
    statements = _filter_comments(text).split(";")
    out = []
    for i in range(0, len(statements), 2):
        if i + 1 < len(statements):
            sql, meta = _extract_meta(statements[i].strip())
            out.append((meta, sql, statements[i + 1].strip()))
    return out


def fixture_lines(text: str):
    out = []
    lines = []
    for line in _filter_comments(text).splitlines():
        lines.append(line)
        if not line.startswith("#"):
            out.append("\n".join(lines))
            lines = []
    return out


class _NoneCmp(ast.NodeTransformer):
    """Rewrite `x == None` / `x != None` as `x is None` / `x is not None`."""

    def visit_Compare(self, node):
        self.generic_visit(node)
        ops = []
        operands = [node.left] + list(node.comparators)
        for i, op in enumerate(node.ops):
            a, b = operands[i], operands[i + 1]
            is_none = any(isinstance(x, ast.Constant) and x.value is None for x in (a, b))
            if is_none and isinstance(op, ast.Eq):
                op = ast.Is()
            elif is_none and isinstance(op, ast.NotEq):
                op = ast.IsNot()
            ops.append(op)
        node.ops = ops
        return node


def none_compare_equivalent(old: str, new: str) -> bool:
    """True if the sources differ only by `is None` <-> `== None` style swaps.

    In sqlglot no __eq__ returns True for None (Expr, Dialect and _Dialect compare by type),
    so the two spellings behave the same."""
    try:
        a = ast.parse(old)
        b = ast.parse(new)
    except SyntaxError:
        return False
    if ast.dump(a) == ast.dump(b):
        return False  # identical ASTs are handled as comment-only
    return ast.dump(_NoneCmp().visit(a)) == ast.dump(_NoneCmp().visit(b))
