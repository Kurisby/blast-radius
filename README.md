# Blast Radius entry

For each sqlglot patch, pick the tests that can observe the change and split them across workers.

- `app/selector.py` reads `/data/commits/*.patch` and `/data/tests.json` and prints one JSON line per commit.
- `app/maps.json.gz` is a per-test coverage map built from sqlglot at the pinned commit
  (`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`): which tests enter each function, execute each line,
  read each fixture file, and instantiate each dialect.
- The `Dockerfile` downloads sqlglot at the pinned commit from its own repository so the selector can
  read the original sources the patches apply to.

`make sample` builds the image, runs it on the 30 sample commits under the scorer's limits, and scores it.
