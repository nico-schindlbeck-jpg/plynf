# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors

# ---- builder ----
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --upgrade pip build && \
    pip wheel --wheel-dir /wheels .

# ---- runtime ----
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLINTH_DATA_DIR=/data

# Non-root user
RUN groupadd --system --gid 1001 plinth && \
    useradd  --system --uid 1001 --gid 1001 --home-dir /app --shell /usr/sbin/nologin plinth && \
    mkdir -p /app /data && \
    chown -R plinth:plinth /app /data

WORKDIR /app

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

USER plinth

EXPOSE 7422

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; \
import os; port=os.environ.get('PLINTH_GATEWAY_PORT','7422'); \
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz',timeout=2).status==200 else 1)"

CMD ["python", "-m", "plinth_gateway"]
