FROM python:3.13.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg bubblewrap ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv==0.10.12

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src/frameseek ./src/frameseek
RUN uv sync --frozen --no-dev --no-editable \
    && useradd --uid 10001 --create-home frameseek \
    && mkdir /data \
    && chown frameseek:frameseek /data

ENV PATH="/app/.venv/bin:$PATH" \
    FRAMESEEK_MODE=fixture \
    FS_COMPONENT=workspace \
    FS_DATA_DIR=/data \
    FS_REQUIRE_DATA_MOUNT=1 \
    PORT=8080 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER 10001:10001
EXPOSE 8080
CMD ["python", "-m", "frameseek.server"]
