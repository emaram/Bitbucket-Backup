# ── Stage 1: Install Python dependencies ──────────────────────────────────────
FROM python:3.12-alpine AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Stage 2: Final minimal image ──────────────────────────────────────────────
FROM python:3.12-alpine

# Git is required for git clone --mirror / git push --mirror
RUN apk add --no-cache git

# Copy pre-built Python packages from builder stage
COPY --from=builder /install /usr/local

# Application source code
WORKDIR /app
COPY backup.py restore.py ./

# Mount points:
#   /app/.env            — configuration file (bind-mount)
#   /app/repos_filter.txt — optional repo filter (bind-mount)
#   /app/backups         — backup snapshot directory (bind-mount to host)
VOLUME ["/app/backups"]

ENTRYPOINT ["python"]
CMD ["backup.py"]
