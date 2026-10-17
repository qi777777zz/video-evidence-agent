FROM python:3.12-slim AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY pyproject.toml README.md requirements.lock ./
COPY video_agent ./video_agent
COPY evaluation ./evaluation
RUN python -m pip install --upgrade pip && python -m pip wheel --wheel-dir /wheels ".[processing]" -c requirements.lock

FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 HOME=/home/app HF_HOME=/models/huggingface
RUN apt-get update && \
    apt-get install --no-install-recommends -y ffmpeg libglib2.0-0 libgl1 && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd --gid 10001 app && \
    useradd --uid 10001 --gid app --create-home --home-dir /home/app app && \
    mkdir -p /app /models && chown -R app:app /app /models
COPY --from=builder /wheels /wheels
RUN python -m pip install /wheels/*.whl && rm -rf /wheels
WORKDIR /app
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "video_agent.api:app", "--host", "0.0.0.0", "--port", "8000"]
