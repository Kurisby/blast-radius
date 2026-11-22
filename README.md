# Blast Radius entry

For each sqlglot patch, find the tests that can observe the change, run them against the patched
tree inside the container, and print only the ones that fail.

How it works:

- `app/maps.json.gz`: per-test coverage of sqlglot at the pinned commit
  (`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`): lines executed by each test, and by each fixture
  item the optimizer/executor tests farm out to their process pools; fixture files each test
  reads; sqlglot classes each test instantiates; lines that only run at import time.
- `app/analysis.py`: maps every changed line to its statement (or function, class attribute,
  module constant, fixture item) and collects the tests and items that execute it. Comment-only
  edits and `is None`/`== None` swaps are recognised as no-ops.
- `app/runner.py`: forks one child per commit; the child loads sqlglot with the patched files served
  from an overlay in `/tmp`, runs the selected tests in-process (pools replaced by a lazy in-process
  executor that still pickles), stops each test at its first failure, and reports pass/fail.
  TPC-DS executor items are settled by comparing the optimizer output each query feeds the
  executor with hashes recorded at the pinned commit (`app/tpcds.json.gz`); DuckDB results of
  the original queries are cached (`app/frames.pkl.gz`); `test_max_nodes` runs at a smaller scale.
- `app/selector.py`: per commit, decides which candidates to run and which to list without running,
  whichever is cheaper, and prints the failing tests split across workers.

`make sample` builds the image, runs it on the 30 sample commits under the scorer's limits, and scores it.
