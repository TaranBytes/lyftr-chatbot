# ---- build stage: install dependencies into a virtualenv ----
FROM python:3.12-slim AS builder
WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- runtime stage: only the venv and the app code ----
FROM python:3.12-slim
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_URL=sqlite:////data/app.db
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY app ./app
RUN mkdir -p /data
EXPOSE 8000
# $PORT is set by hosts such as Railway; defaults to 8000 locally.
CMD ["sh", "-c", "exec uvicorn app.main:create_app_from_env --factory --host 0.0.0.0 --port ${PORT:-8000}"]
