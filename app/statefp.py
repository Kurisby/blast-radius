"""Fingerprint of sqlglot's import-time state.

After every sqlglot module is imported, hash what the import left behind: module globals
and the own attributes of every sqlglot class (dicts per key). Comparing a patched tree's
fingerprint with the pinned commit's says which settings and table entries a change to
import-time code (class bodies, module level, metaclasses) really altered.

Function code is not hashed (code changes are found through line coverage); functions are
identified by name plus their defaults and closure contents.

Run with a fixed PYTHONHASHSEED: cached hashes and orderings otherwise differ per process.
"""
import enum
import functools
import hashlib
import importlib
import json
import pkgutil
import re
import sys
import types


def keyname(k):
    """Canonical dict-key name, shared with the build-time read profile."""
    if isinstance(k, type):
        return "T:" + k.__qualname__.split(".")[-1]
    if isinstance(k, enum.Enum):
        return "E:" + k.name
    if isinstance(k, str):
        return "S:" + k
    if k is None or type(k) in (int, bool, float):
        return "V:" + repr(k)
    return "*"


_ATOMS = (int, float, complex, str, bytes, bool, type(None))


class _Hasher:
    def __init__(self):
        self.memo = {}
        self.stack = set()
        self.types = []

    def h(self, v) -> str:
        t = type(v)
        if t in _ATOMS:
            return t.__name__[0] + repr(v)
        i = id(v)
        r = self.memo.get(i)
        if r is not None:
            return r[1]
        if i in self.stack:
            return "@"
        self.stack.add(i)
        try:
            s = self._canon(v, t)
        finally:
            self.stack.discard(i)
        r = hashlib.blake2b(s.encode("utf-8", "surrogatepass"), digest_size=8).hexdigest()
        self.memo[i] = (v, r)  # keeps v alive: its id must not be reused
        return r

    def _canon(self, v, t) -> str:
        h = self.h
        if isinstance(v, enum.Enum):
            return "E" + type(v).__module__ + "." + type(v).__qualname__ + "." + str(v.name)
        if isinstance(v, type):
            mod = getattr(v, "__module__", "") or ""
            if mod.startswith("sqlglot"):
                self.types.append(v)
            return "T" + mod + "." + getattr(v, "__qualname__", "?")
        if t is tuple or t is list:
            items = [h(x) for x in v]
            if items and all(isinstance(x, type) for x in v):
                items.sort()  # often built from a set of classes: order follows ids
            return t.__name__ + "(" + ",".join(items) + ")"
        if isinstance(v, (set, frozenset)):
            return "S(" + ",".join(sorted(h(x) for x in v)) + ")"
        if isinstance(v, dict):
            return "D" + t.__name__ + "(" + ",".join(sorted(h(k) + ":" + h(x) for k, x in list(v.items()))) + ")"
        if isinstance(v, types.FunctionType):
            cells = []
            for c in v.__closure__ or ():
                try:
                    cells.append(h(c.cell_contents))
                except ValueError:
                    cells.append("-")
            return ("F" + str(v.__module__) + "." + v.__qualname__ + "|" + h(v.__defaults__) + "|"
                    + h(v.__kwdefaults__) + "|" + ",".join(cells))
        if isinstance(v, types.MethodType):
            return "M" + h(v.__func__) + "|" + type(v.__self__).__qualname__
        if isinstance(v, (classmethod, staticmethod)):
            return t.__name__ + h(v.__func__)
        if isinstance(v, property):
            return "P" + h(v.fget) + h(v.fset) + h(v.fdel)
        if isinstance(v, functools.partial):
            return "p" + h(v.func) + h(v.args) + h(v.keywords)
        if isinstance(v, re.Pattern):
            return "R" + repr(v.pattern) + str(v.flags)
        if isinstance(v, types.ModuleType):
            return "m" + v.__name__
        if isinstance(v, (types.BuiltinFunctionType, types.WrapperDescriptorType, types.MethodWrapperType,
                          types.MethodDescriptorType, types.CodeType, types.GetSetDescriptorType,
                          types.MemberDescriptorType)):
            return "B" + t.__name__ + ":" + str(getattr(v, "__qualname__", getattr(v, "__name__", "")))
        parts = ["O" + t.__module__ + "." + t.__qualname__]
        d = getattr(v, "__dict__", None)
        if isinstance(d, dict):
            parts.append(h(d))
        for k in type(v).__mro__:
            sls = k.__dict__.get("__slots__", ())
            if isinstance(sls, str):
                sls = (sls,)
            for sl in sls:
                if sl in ("__dict__", "__weakref__"):
                    continue
                try:
                    parts.append(sl + "=" + h(getattr(v, sl)))
                except AttributeError:
                    parts.append(sl + "=-")
        return "|".join(parts)


def import_all():
    import sqlglot

    errors = []
    for m in sorted(m.name for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot.")):
        if m.endswith("__main__"):
            continue
        try:
            importlib.import_module(m)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{m}: {type(e).__name__}")
    return errors


def fingerprint() -> dict:
    """{path: hash} for module globals ("mod:name") and own class attributes
    ("mod:Qual.attr"); dict-valued entries map to {"*": hash, "k": {keyname: hash}}."""
    sys.setrecursionlimit(max(sys.getrecursionlimit(), 20000))
    hs = _Hasher()
    out = {}
    done = set()

    def entry(path, v):
        if type(v) is dict:
            ks = {}
            for k, x in list(v.items()):
                kn = keyname(k)
                ks[kn] = hs.h(x) if kn not in ks else hs.h((ks[kn], hs.h(x)))
            out[path] = {"*": hs.h(v), "k": ks, "n": len(v)}
        else:
            out[path] = hs.h(v)

    def walk_class(cls):
        if id(cls) in done:
            return
        done.add(id(cls))
        prefix = f"{cls.__module__}:{cls.__qualname__}"
        if issubclass(cls, enum.Enum):
            out[prefix] = hs.h(tuple((m.name, m.value) for m in cls))
            return
        out[prefix + ".__bases__"] = hs.h(cls.__bases__)
        for attr, v in sorted(vars(cls).items(), key=lambda kv: kv[0]):
            if attr in ("__dict__", "__weakref__", "__doc__", "__module__", "__qualname__", "__annotations__",
                        "__annotate__", "__firstlineno__", "__static_attributes__", "__slots__",
                        "__abstractmethods__", "_abc_impl", "__parameters__", "__orig_bases__"):
                continue
            if isinstance(v, type):
                # which class a setting names (tokenizer_class, nested Parser, ...) is state too
                out[f"{prefix}.{attr}"] = hs.h(v)
                continue
            if isinstance(v, types.FunctionType) and v.__qualname__ == f"{cls.__qualname__}.{attr}":
                # a method: only its defaults and closure are state
                if v.__defaults__ is None and v.__kwdefaults__ is None and not v.__closure__:
                    continue
            entry(f"{prefix}.{attr}", v)

    for modname in sorted(m for m in sys.modules if m == "sqlglot" or m.startswith("sqlglot.")):
        mod = sys.modules[modname]
        if mod is None:
            continue
        for name, v in sorted(vars(mod).items(), key=lambda kv: kv[0]):
            if name.startswith("__") and name.endswith("__"):
                continue
            if isinstance(v, type):
                out[f"{modname}:{name}"] = hs.h(v)
                continue
            if isinstance(v, types.ModuleType):
                continue
            if isinstance(v, types.FunctionType) and v.__module__ == modname and v.__qualname__ == name:
                if v.__defaults__ is None and v.__kwdefaults__ is None and not v.__closure__:
                    continue
            entry(f"{modname}:{name}", v)
        # classes reached from anywhere (module globals, registries, nested classes)
        while hs.types:
            walk_class(hs.types.pop())
    while hs.types:
        walk_class(hs.types.pop())
    return out


def diff(a: dict, b: dict) -> list:
    """[(path, kind, keys)]: kind is "attr" (whole value), "dict" (keys changed, with the
    changed key names and whether the size changed), "added" or "removed"."""
    res = []
    for p in sorted(set(a) | set(b)):
        x, y = a.get(p), b.get(p)
        if x == y:
            continue
        if x is None:
            res.append((p, "added", None))
        elif y is None:
            res.append((p, "removed", None))
        elif isinstance(x, dict) and isinstance(y, dict):
            xk, yk = x["k"], y["k"]
            ks = sorted(k for k in set(xk) | set(yk) if xk.get(k) != yk.get(k))
            if not ks and x["*"] != y["*"]:
                ks = ["*"]
            res.append((p, "dict", {"keys": ks, "size": x.get("n") != y.get("n")}))
        else:
            res.append((p, "attr", None))
    return res


def main():
    """python statefp.py OUT.json [ORIG.json]: write the fingerprint, or with ORIG the diff
    (ORIG's "unstable" paths, which differ between two runs of the pinned commit, are kept
    out of the comparison and reported separately)."""
    errors = import_all()
    fp = fingerprint()
    if len(sys.argv) > 2:
        with open(sys.argv[2]) as fh:
            orig = json.load(fh)
        res = {"errors": errors, "diff": diff(orig["fp"], fp), "orig_errors": orig.get("errors", []),
               "unstable": orig.get("unstable", [])}
    else:
        res = {"errors": errors, "fp": fp}
    with open(sys.argv[1], "w") as fh:
        json.dump(res, fh)


if __name__ == "__main__":
    main()
