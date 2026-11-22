"""Serve patched sources/files from an overlay directory without touching the read-only tree.

BR_OVERLAY=<dir> holds patched files at their repo-relative paths; BR_ROOT is the sqlglot
checkout. Modules load with their original __file__ so path-relative code keeps working.
"""
import builtins
import importlib.abc
import importlib.machinery
import importlib.util
import io
import os
import sys

_installed = False


class _OverlayLoader(importlib.abc.Loader):
    def __init__(self, overlay_path, orig_path):
        self.overlay_path = overlay_path
        self.orig_path = orig_path

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        with _real_open(self.overlay_path, "rb") as fh:
            src = fh.read()
        code = compile(src, self.orig_path, "exec", dont_inherit=True)
        module.__file__ = self.orig_path
        exec(code, module.__dict__)

    def get_source(self, name):
        with _real_open(self.overlay_path, "r", encoding="utf-8") as fh:
            return fh.read()


class _OverlayFinder(importlib.abc.MetaPathFinder):
    def __init__(self, root, overlay, rels):
        self.root = root
        self.overlay = overlay
        self.rels = rels

    def find_spec(self, name, path=None, target=None):
        base = name.replace(".", "/")
        for rel, is_pkg in ((base + ".py", False), (base + "/__init__.py", True)):
            if rel in self.rels:
                orig = os.path.join(self.root, rel)
                loader = _OverlayLoader(os.path.join(self.overlay, rel), orig)
                spec = importlib.util.spec_from_file_location(
                    name, orig, loader=loader,
                    submodule_search_locations=[os.path.dirname(orig)] if is_pkg else None,
                )
                return spec
        return None


_real_open = io.open
_real_open_code = io.open_code


def install(root=None, overlay=None):
    global _installed
    root = root or os.environ.get("BR_ROOT")
    overlay = overlay or os.environ.get("BR_OVERLAY")
    if _installed or not root or not overlay or not os.path.isdir(overlay):
        return
    _installed = True
    root = os.path.abspath(root)
    rels = set()
    for dirpath, _, files in os.walk(overlay):
        for f in files:
            rels.add(os.path.relpath(os.path.join(dirpath, f), overlay).replace(os.sep, "/"))
    absmap = {os.path.join(root, r): os.path.join(overlay, r) for r in rels}
    py_rels = {r for r in rels if r.endswith(".py")}
    if py_rels:
        sys.meta_path.insert(0, _OverlayFinder(root, overlay, py_rels))
    if absmap:
        def _map(file):
            if isinstance(file, (str, bytes, os.PathLike)):
                try:
                    p = os.path.abspath(os.fsdecode(file))
                except Exception:
                    return file
                return absmap.get(p, file)
            return file

        def open(file, *args, **kwargs):  # noqa: A001
            return _real_open(_map(file), *args, **kwargs)

        def open_code(path):
            return _real_open_code(_map(path))

        builtins.open = open
        io.open = open
        io.open_code = open_code
