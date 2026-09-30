FROM python:3.12-slim AS builder

WORKDIR /build
# pip-setup.sh is an OPTIONAL hook: absent by default. A build environment that
# needs pip pointed at a mirror can supply it in the build context; it is
# sourced inside the same RUN as pip (so anything it exports, e.g.
# PIP_INDEX_URL, applies to that command only and is never written to a layer).
COPY requirements.txt pip-setup.s[h] ./
# Wheels only, and every file must match a pinned hash (requirements.txt is
# generated from requirements.in with pip-compile --generate-hashes).
RUN if [ -f pip-setup.sh ]; then . ./pip-setup.sh; fi; \
    pip install --no-cache-dir --only-binary=:all: --require-hashes --prefix=/install -r requirements.txt


FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

COPY --from=builder /install /usr/local

WORKDIR /app
COPY main.py agora_db.py pg_migrate.py schema.sql ./
COPY static/ static/

# Run as an unprivileged user (numeric, so the platform can verify it).
USER 10001:10001

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
