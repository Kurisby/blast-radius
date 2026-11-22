# Map building (run once against the pinned sqlglot commit)

All maps in `app/` are built from the unpatched pinned commit; nothing is keyed by patch.

- `covplugin5.py` (pytest plugin): per-test and per-fixture-item coverage of sqlglot
  (`COV_LINES=1`), a comparison-outcome profile (`COV_CMP=1`: for each comparison, the
  outcome classes that tell `is`/`==`, `</<=`, `>/>=` apart, and every `==`/`!=` evaluation),
  and a boolean-formula profile (`COV_BOOL=1`: leaf truth vectors of `and`/`or`/`not`
  conditions). The profiles need `cmpsite/` on `PYTHONPATH` so spawned workers are profiled too.
- `build_maps5.py`: merges the dumps into `app/maps.json.gz` (comma-separated dump dirs, e.g.
  two runs under different `PYTHONHASHSEED`s). The selector uses `==`/`!=` swaps only when
  the map carries `"cmp_ok": 1`, set after building from a profile that records every evaluation.
- `covplugin5.py` with `COV_KEYS=1`: imports every sqlglot module, then wraps the own data
  attributes of every sqlglot class to record per context which attributes are read and which
  class-dict keys are looked up (`COV_DEFS=1`: which function parameters a call left at their
  default). Reads of a tokenizer setting while `Tokenizer._init_core` hands it to the core
  are recorded apart (`@init`), with the core slot that stores it (`mirror`) and the reads
  of that slot, so a changed setting selects the tests whose tokenizing reads its slot.
  `build_attrs.py` merges those dumps into `app/attrs.json.gz`. At run time the
  selector compares the import-time state of a patched tree with the pinned commit's
  (`app/statefp.py`, fingerprint built in the image) and selects the tests that read what changed.
- `tpcds_attr_phases.py`: which class attributes and class-dict keys each TPC-DS query reads in
  each phase (parse, optimize, execute, DuckDB transpile), so a changed setting gets the cheap
  check of the phase that reads it (`app/tpcds_attrs.json.gz`; built under 3 hash seeds, merged).
- `tpcds_phases.py`, `baseline_frames.py`: TPC-DS phase coverage and recorded DuckDB results
  for the executor tests.
- `mkmut.py`: local evaluation only. Makes random kit-style mutants of the pinned tree and
  records which tests really fail (full suite), to check the selector for misses.

```
cd sqlglot
mkdir -p ../cov_l ../cov_c ../cov_b
SKIP_INTEGRATION=0 PYTHONPATH=$PWD:../tools COV_ROOT=$PWD/ COV_OUTDIR=../cov_l COV_LINES=1 \
  python -m pytest -q -p no:cacheprovider -p covplugin5 tests
SKIP_INTEGRATION=0 PYTHONPATH=$PWD:../tools:../tools/cmpsite COV_ROOT=$PWD/ COV_OUTDIR=../cov_c COV_CMP=1 \
  python -m pytest -q -p no:cacheprovider -p covplugin5 tests
SKIP_INTEGRATION=0 PYTHONPATH=$PWD:../tools:../tools/cmpsite COV_ROOT=$PWD/ COV_OUTDIR=../cov_b COV_BOOL=1 \
  python -m pytest -q -p no:cacheprovider -p covplugin5 tests
python ../tools/build_maps5.py ../cov_l ../cov_c ../sample/tests.json ../app/maps.json.gz ../cov_b
python -c "import gzip,json; p='../app/maps.json.gz'; m=json.load(gzip.open(p)); m['cmp_ok']=1; json.dump(m, gzip.open(p,'wt'))"
mkdir -p ../cov_k ../cov_k2 ../cov_d ../cov_d2
SKIP_INTEGRATION=0 PYTHONPATH=$PWD:../tools:../tools/cmpsite COV_ROOT=$PWD/ COV_OUTDIR=../cov_k COV_KEYS=1 \
  python -m pytest -q -p no:cacheprovider -p covplugin5 tests
# (each profile is run twice, the second time under another PYTHONHASHSEED into ../cov_k2, ../cov_d2)
SKIP_INTEGRATION=0 PYTHONPATH=$PWD:../tools:../tools/cmpsite COV_ROOT=$PWD/ COV_OUTDIR=../cov_d COV_DEFS=1 \
  python -m pytest -q -p no:cacheprovider -p covplugin5 tests
python ../tools/build_attrs.py ../cov_k,../cov_k2,../cov_d,../cov_d2 ../sample/tests.json ../app/attrs.json.gz
```
