# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors

# ---------- builder ----------
FROM python:3.11-slim AS builder

WORKDIR /build

# Build deps for any wheels that need compilation.
RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --upgrade pip wheel \
    && python -m pip wheel --no-deps --wheel-dir /wheels .

# Install runtime deps into a relocatable prefix.
RUN python -m pip install --prefix=/install --no-warn-script-location /wheels/*.whl


# ---------- runtime ----------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLINTH_DATA_DIR=/data \
    PLINTH_WORKSPACE_HOST=0.0.0.0 \
    PLINTH_WORKSPACE_PORT=7421

# Non-root user for the runtime.
RUN groupadd --system --gid 1000 plinth \
    && useradd --system --uid 1000 --gid plinth --home-dir /home/plinth --create-home plinth \
    && mkdir -p /data \
    && chown -R plinth:plinth /data

COPY --from=builder /install /usr/local

USER plinth
WORKDIR /home/plinth

EXPOSE 7421

HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
    CMD python -c "import urllib.request,sys; \
import os; \
port=os.environ.get('PLINTH_WORKSPACE_PORT','7421'); \
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=2).status==200 else 1)"

CMD ["python", "-m", "plinth_workspace"]
