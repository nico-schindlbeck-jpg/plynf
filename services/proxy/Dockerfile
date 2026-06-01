# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors

# ---------- builder ----------
FROM python:3.11-slim AS builder

WORKDIR /build

RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --upgrade pip wheel \
    && python -m pip wheel --no-deps --wheel-dir /wheels .

RUN python -m pip install --prefix=/install --no-warn-script-location /wheels/*.whl


# ---------- runtime ----------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLINTH_PROXY_HOST=0.0.0.0 \
    PLINTH_PROXY_PORT=7430

RUN groupadd --system --gid 1000 plinth \
    && useradd --system --uid 1000 --gid plinth --home-dir /home/plinth --create-home plinth

COPY --from=builder /install /usr/local

USER plinth
WORKDIR /home/plinth

EXPOSE 7430

# Liveness on the resolved port (honors Render/Fly's injected $PORT).
HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
    CMD python -c "import urllib.request,sys,os; \
port=os.environ.get('PORT') or os.environ.get('PLINTH_PROXY_PORT','7430'); \
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=2).status==200 else 1)"

# Bind $PORT if a platform injects one (Render, Fly, Railway), else
# PLINTH_PROXY_PORT, else the 7430 default. exec so uvicorn is PID 1.
CMD ["sh", "-c", "exec uvicorn plinth_proxy.api:app --host 0.0.0.0 --port ${PORT:-${PLINTH_PROXY_PORT:-7430}}"]
