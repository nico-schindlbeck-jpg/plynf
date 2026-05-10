FROM python:3.14-slim

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

ENV PLINTH_GITHUB_PORT=7426 \
    PLINTH_GITHUB_LOG_FORMAT=json

EXPOSE 7426

CMD ["python", "-m", "github_mcp"]
