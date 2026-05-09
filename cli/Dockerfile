# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
#
# Optional CLI-as-container image. Use this when the operator's host
# doesn't have Python 3.11+ or you want a reproducible CI tool. The
# entrypoint is the ``plinth`` console script, so:
#
#     docker run --rm -v $HOME/.plinth:/root/.plinth \
#         ghcr.io/your-org/plinth-cli:1.0.0 health
#
# is equivalent to running ``plinth health`` on the host. The volume
# mount keeps the user's profile config in sync.

FROM python:3.12-slim AS builder

WORKDIR /build

# Build wheels for both the SDK and the CLI itself, then drop into the
# runtime stage. Caching ``COPY`` of just the metadata first lets layer
# rebuilds skip the (slow) wheel build when source code changes.
COPY sdk/python/pyproject.toml ./sdk/pyproject.toml
COPY cli/pyproject.toml ./cli/pyproject.toml

# Bring in source after metadata so re-running the build with only
# source changes re-uses the dependency layer above.
COPY sdk/python /build/sdk
COPY cli /build/cli

RUN pip install --upgrade pip build && \
    cd /build/sdk && pip wheel --wheel-dir /wheels --no-deps . && \
    cd /build/cli && pip wheel --wheel-dir /wheels --no-deps .

# ---------------------------------------------------------------------------

FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="plinth-cli"
LABEL org.opencontainers.image.description="Unified Plinth operator CLI."
LABEL org.opencontainers.image.licenses="Apache-2.0"

# Minimal runtime: just the CLI + SDK + their deps. Everything else lives
# in the ``builder`` stage and gets discarded.
COPY --from=builder /wheels /wheels

RUN pip install --no-cache-dir /wheels/*.whl click rich httpx tomli && \
    rm -rf /wheels

# Configurable defaults so ``docker run -e PLINTH_API_KEY=… plinth-cli …``
# works without any image-side editing. Real config still goes in
# ``$HOME/.plinth/config.toml`` via a bind mount.
ENV PLINTH_OUTPUT=human \
    HOME=/root

WORKDIR /work
ENTRYPOINT ["plinth"]
CMD ["--help"]
