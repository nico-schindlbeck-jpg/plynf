FROM python:3.11-slim AS builder
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --upgrade pip wheel && pip install --no-cache-dir .

FROM python:3.11-slim
RUN groupadd --system --gid 1001 plinth && \
    useradd  --system --uid 1001 --gid 1001 --home-dir /app --create-home plinth && \
    mkdir -p /data && \
    chown -R plinth:plinth /data
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
USER plinth
ENV PLINTH_DATA_DIR=/data
EXPOSE 7425
CMD ["python", "-m", "plinth_identity"]
