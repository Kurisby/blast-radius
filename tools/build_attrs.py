"""Merge COV_KEYS=1 dumps (class attribute reads and class-dict key lookups per context)
into app/attrs.json.gz.

usage: build_attrs.py DUMPDIR[,DUMPDIR...] tests.json OUT

Entries are "<module>:<Class qualname>.<attr>|<key>" where key is "@" (attribute read),
a dict key name (see statefp.keyname), "*" (whole-dict use) or "#" (len). Accesses
outside a test (collection, between tests) are listed in "nontest".
"""
import collections
import glob
import gzip
import json
import os
import sys

dumps, tests_json, out = sys.argv[1], sys.argv[2], sys.argv[3]
tests = json.load(open(tests_json))["tests"]
ids = [t["id"] for t in tests]
idx = {t: i for i, t in enumerate(ids)}
A_all = collections.defaultdict(set)
A_glob = collections.defaultdict(set)
A_item = collections.defaultdict(lambda: collections.defaultdict(set))
nontest = set()
wrapped = {}
for p in [q for d in dumps.split(",") for q in glob.glob(os.path.join(d, "*.json"))]:
    d = json.load(open(p))
    for ctx, rec in d.items():
        keys = rec.get("keys", ())
        if ctx == "<wrapped>":
            for x in keys:
                owner, _, kind = x.rpartition("|")
                wrapped[owner] = kind
            continue
        base, _, item = ctx.partition("#")
        n = idx.get(base)
        for x in keys:
            if n is None:
                nontest.add(x)
                continue
            A_all[x].add(n)
            if item:
                A_item[x][n].add(int(item))
            else:
                A_glob[x].add(n)
pool = {}


def sid(s):
    k = tuple(sorted(s))
    if k not in pool:
        pool[k] = len(pool)
    return pool[k]


attrs = {}
for x, st in A_all.items():
    e = [sid(st), sid(A_glob.get(x, ()))]
    if x in A_item:
        e.append({str(t): sid(v) for t, v in A_item[x].items()})
    attrs[x] = e
m = {"ids": ids, "attrs": attrs, "nontest": sorted(nontest), "wrapped": wrapped,
     "sets": [list(k) for k in sorted(pool, key=pool.get)]}
with gzip.open(out, "wt", compresslevel=9) as fh:
    json.dump(m, fh, separators=(",", ":"))
print("entries", len(attrs), "sets", len(pool), "nontest", len(nontest), "wrapped", len(wrapped),
      "size", os.path.getsize(out))
