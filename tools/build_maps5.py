"""Merge coverage v4 dumps into the compact selection map shipped in the image."""
import json, glob, collections, os, sys, gzip
covdir, cmpdir, tests_json, out = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
booldir = sys.argv[5] if len(sys.argv) > 5 else ""
min_item_secs = 0.0
tests = json.load(open(tests_json))["tests"]
ids = [t["id"] for t in tests]
secs = {t["id"]: t["seconds"] for t in tests}
idx = {t: i for i, t in enumerate(ids)}
L_all = collections.defaultdict(set); L_glob = collections.defaultdict(set)
L_item = collections.defaultdict(lambda: collections.defaultdict(set))  # (f,l) -> t -> items
funcs = collections.defaultdict(set); reads = collections.defaultdict(set); classes = collections.defaultdict(set)
classes_glob = collections.defaultdict(set)
classes_items = collections.defaultdict(lambda: collections.defaultdict(set))
F_item = collections.defaultdict(lambda: collections.defaultdict(set))
items_of = collections.defaultdict(set)
import_lines = set()
for p in [q for d in covdir.split(",") for q in glob.glob(os.path.join(d, "*.json"))]:
    d = json.load(open(p))
    for ctx, rec in d.items():
        base, _, item = ctx.partition("#")
        n = idx.get(base)
        if n is None:
            if base.startswith("<"):
                for f, ls in rec["lines"].items():
                    for l in ls:
                        import_lines.add((f, l))
            continue
        it = int(item) if item else None
        if it is not None:
            items_of[n].add(it)
        keep_items = it is not None and secs[base] >= min_item_secs
        for f, ls in rec["lines"].items():
            for l in ls:
                L_all[(f, l)].add(n)
                if it is None:
                    L_glob[(f, l)].add(n)
                elif keep_items:
                    L_item[(f, l)][n].add(it)
                else:
                    L_glob[(f, l)].add(n)  # no item resolution kept: treat as whole test
        for f, ls in rec["funcs"].items():
            for l in ls:
                funcs[(f, l)].add(n)
                if keep_items:
                    F_item[(f, l)][n].add(it)
        for r in rec["reads"]:
            reads[r].add(n)
        for c in rec["classes"]:
            classes[c].add(n)
            if it is None:
                classes_glob[c].add(n)
            else:
                classes_items[c][n].add(it)
# comparison profile: (site, cls) -> all tests / glob tests / items per test
C_all = collections.defaultdict(set); C_glob = collections.defaultdict(set)
C_item = collections.defaultdict(lambda: collections.defaultdict(set))
cmp_nontest = set()
for p in [q for d in cmpdir.split(",") for q in glob.glob(os.path.join(d, "*.json"))]:
    d = json.load(open(p))
    for ctx, rec in d.items():
        base, _, item = ctx.partition("#")
        n = idx.get(base)
        for x in rec.get("cmp", ()):
            site, cls = x.split("|")
            if n is None:
                cmp_nontest.add(site)
                continue
            C_all[(site, cls)].add(n)
            if item:
                C_item[(site, cls)][n].add(int(item))
            else:
                C_glob[(site, cls)].add(n)
B_all = collections.defaultdict(set); B_glob = collections.defaultdict(set)
B_item = collections.defaultdict(lambda: collections.defaultdict(set))
bool_nontest = set()
for p in [q for d in booldir.split(",") if d for q in glob.glob(os.path.join(d, "*.json"))]:
    d = json.load(open(p))
    for ctx, rec in d.items():
        base, _, item = ctx.partition("#")
        n = idx.get(base)
        for x in rec.get("bool", ()):
            site, vec = x.split("|")
            if n is None:
                bool_nontest.add(site)
                continue
            B_all[(site, vec)].add(n)
            if item:
                B_item[(site, vec)][n].add(int(item))
            else:
                B_glob[(site, vec)].add(n)
pool = {}
def sid(s):
    k = tuple(sorted(s))
    if k not in pool:
        pool[k] = len(pool)
    return pool[k]
lines_out = collections.defaultdict(dict)
for (f, l), s in L_all.items():
    entry = [sid(s), sid(L_glob.get((f, l), ()))]
    if (f, l) in L_item:
        entry.append({str(t): sid(v) for t, v in L_item[(f, l)].items()})
    lines_out[f][str(l)] = entry
funcs_out = collections.defaultdict(dict)
for (f, l), s in funcs.items():
    funcs_out[f][str(l)] = sid(s)
m = {
    "ids": ids,
    "lines": lines_out,
    "funcs": funcs_out,
    "reads": {k: sid(v) for k, v in reads.items()},
    "classes": {k: sid(v) for k, v in classes.items()},
    "classes_glob": {k: sid(v) for k, v in classes_glob.items()},
    "classes_items": {c: {str(t): sid(v) for t, v in d.items()} for c, d in classes_items.items()},
    "items": {str(n): len(v) for n, v in items_of.items()},
    "import_lines": {f: sorted(l for ff, l in import_lines if ff == f) for f in {ff for ff, _ in import_lines}},
    "dialect_files": json.load(open("dialect_files.json")),
}
cmp_out = collections.defaultdict(dict)
for (site, cls), st in C_all.items():
    entry = [sid(st), sid(C_glob.get((site, cls), ()))]
    if (site, cls) in C_item:
        entry.append({str(t): sid(v) for t, v in C_item[(site, cls)].items()})
    cmp_out[site][cls] = entry
m["cmp"] = cmp_out
if booldir:
    bool_out = collections.defaultdict(dict)
    for (site, vec), st in B_all.items():
        entry = [sid(st), sid(B_glob.get((site, vec), ()))]
        if (site, vec) in B_item:
            entry.append({str(t): sid(v) for t, v in B_item[(site, vec)].items()})
        bool_out[site][vec] = entry
    m["bool"] = bool_out
    m["bool_nontest"] = sorted(bool_nontest)
m["cmp_nontest"] = sorted(cmp_nontest)
m["sets"] = [list(k) for k in sorted(pool, key=pool.get)]
with gzip.open(out, "wt", compresslevel=9) as fh:
    json.dump(m, fh, separators=(",", ":"))
print("sets", len(pool), "lines", sum(len(v) for v in lines_out.values()), "item-lines", len(L_item),
      "classes", len(classes), "size", os.path.getsize(out))
