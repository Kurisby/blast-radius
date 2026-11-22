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
```
