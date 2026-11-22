# The scorer builds this on linux/amd64 and runs it with no network.
# Same base image the tracks were labeled in: its Python decides which sqlglot tests run.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# The packages of the labeling environment, so sqlglot's tests run here as they did there.
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --disable-pip-version-check -r /tmp/requirements.lock

# sqlglot at the pinned commit, from its own repository.
ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
RUN python - <<'PY'
import io, os, shutil, tarfile, urllib.request
sha = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
url = f"https://github.com/tobymao/sqlglot/archive/{sha}.tar.gz"
data = urllib.request.urlopen(url, timeout=300).read()
with tarfile.open(fileobj=io.BytesIO(data)) as tf:
    tf.extractall("/tmp/sg", filter="data")
src = os.path.join("/tmp/sg", os.listdir("/tmp/sg")[0])
shutil.move(src, "/sqlglot")
shutil.rmtree("/tmp/sg")
PY

COPY app /app
# Byte-compile everything the run imports: the disk is read-only at run time.
# Also parse the pinned commit's TPC CSVs once (the executor tests' table rows).
RUN python -m compileall -q /sqlglot/sqlglot /sqlglot/tests /app \
    && python -c "import sys; sys.path.insert(0, '/app'); import runner; runner.build_pre_rows('/app/pre_rows.pkl')" \
    && chmod -R a+rX /app /sqlglot

ENV SKIP_INTEGRATION=0 PYTHONDONTWRITEBYTECODE=1
WORKDIR /sqlglot
CMD ["python", "/app/selector.py"]
