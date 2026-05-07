FROM python:3.11-slim AS builder
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --upgrade pip wheel && pip install --no-cache-dir .

FROM python:3.11-slim
RUN useradd -m -u 1001 plinth
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
USER plinth
ENV PLINTH_DATA_DIR=/data
EXPOSE 7425
CMD ["python", "-m", "plinth_identity"]
