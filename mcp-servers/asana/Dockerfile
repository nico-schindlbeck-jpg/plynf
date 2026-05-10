FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --upgrade pip && pip install .

# Non-root user.
RUN useradd --create-home --uid 10001 plinth
USER plinth

ENV PLINTH_ASANA_MCP_PORT=7433 \
    PLINTH_ASANA_MCP_LOG_FORMAT=json

EXPOSE 7433

CMD ["python", "-m", "asana_mcp"]
