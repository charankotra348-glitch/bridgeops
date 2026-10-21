# Production image: the web UI is built, then served by the API from the same origin.
#   docker build -t bridgeops .
#   docker run -p 8000:8000 -e DATABASE_URL=... -e ADMIN_EMAIL=... -e ADMIN_PASSWORD=... bridgeops

FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    WEB_DIST=/app/web \
    COOKIE_SECURE=true
COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY api/alembic.ini .
COPY api/migrations ./migrations
COPY api/app ./app
COPY --from=web /web/dist ./web
RUN useradd --create-home appuser
USER appuser
EXPOSE 8000
# Hosts set PORT; migrations run on startup (AUTO_MIGRATE). --proxy-headers trusts the host's
# load balancer for the original scheme and client address.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
