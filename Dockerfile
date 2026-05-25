FROM python:3.11-slim

# Don't write .pyc files, don't buffer stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Run as a non-root user for security
RUN useradd --create-home --shell /bin/bash app && chown -R app:app /app
USER app

EXPOSE 8000

# NOTE: Secrets (MASTER_DATABASE_URL, REDIS_PASSWORD, ADMIN_SECRET, etc.)
# must be injected at RUNTIME, not at build time.
# In Coolify: uncheck "Is Build Variable?" for those env vars.

CMD ["python", "run.py"]
