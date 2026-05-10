# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors

FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install the SDK + worker. Operators are expected to layer their own
# handlers module on top of this image.
COPY ../sdk/python /tmp/sdk
RUN pip install /tmp/sdk

COPY pyproject.toml README.md /app/
COPY src/ /app/src/
RUN pip install /app

# Drop privileges
RUN useradd --create-home --uid 1000 plinth
USER plinth

ENTRYPOINT ["plinth-workflow-worker"]
