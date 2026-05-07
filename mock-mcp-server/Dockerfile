FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --upgrade pip && pip install .

# Non-root user for safety.
RUN useradd --create-home --uid 10001 plinth && \
    mkdir -p /tmp/plinth-mock-fixtures && \
    chown -R plinth:plinth /tmp/plinth-mock-fixtures
USER plinth

ENV PLINTH_MOCK_PORT=7423 \
    PLINTH_MOCK_FIXTURES_DIR=/tmp/plinth-mock-fixtures \
    PLINTH_MOCK_LOG_FORMAT=json

EXPOSE 7423

CMD ["python", "-m", "mock_mcp"]
