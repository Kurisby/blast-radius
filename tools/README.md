# Map building (run once against the pinned sqlglot commit)

All maps in `app/` are built from the unpatched pinned commit; nothing is keyed by patch.

- `covplugin5.py` (pytest plugin): per-test and per-fixture-item coverage of sqlglot
  (`COV_LINES=1`), and a comparison-outcome profile (`COV_CMP=1`, with `cmpsite/` on
  `PYTHONPATH` so spawned workers are profiled too).
- `build_maps5.py`: merges the dumps into `app/maps.json.gz`.
- `tpcds_phases.py`, `baseline_frames.py`: TPC-DS phase coverage and recorded DuckDB results
  for the executor tests.

```
cd sqlglot
SKIP_INTEGRATION=0 PYTHONPATH=$PWD:../tools COV_ROOT=$PWD/ COV_OUTDIR=../cov_l COV_LINES=1 \
  python -m pytest -q -p no:cacheprovider -p covplugin5 tests
SKIP_INTEGRATION=0 PYTHONPATH=$PWD:../tools:../tools/cmpsite COV_ROOT=$PWD/ COV_OUTDIR=../cov_c COV_CMP=1 \
  python -m pytest -q -p no:cacheprovider -p covplugin5 tests
python ../tools/build_maps5.py ../cov_l ../cov_c ../sample/tests.json ../app/maps.json.gz
```
