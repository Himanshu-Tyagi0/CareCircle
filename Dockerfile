FROM python:3.11.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/appuser \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    CARECIRCLE_DEMO_MODE=false

RUN apt-get update && \
    apt-get install -y --no-install-recommends nginx ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 --shell /bin/bash appuser

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /app/requirements.txt

COPY app.py /app/app.py
COPY core /app/core
COPY services /app/services
COPY repositories /app/repositories
COPY integrations /app/integrations
COPY ui /app/ui
COPY pages /app/pages
COPY locales /app/locales
COPY scripts /app/scripts
COPY static /app/static
COPY db /app/db
COPY .streamlit /app/.streamlit
COPY deployment /app/deployment

RUN chmod +x /app/deployment/entrypoint.sh && \
    mkdir -p /home/appuser/.streamlit /tmp/nginx_client_body /tmp/nginx_proxy /tmp/nginx_fastcgi /tmp/nginx_uwsgi /tmp/nginx_scgi /var/log/nginx && \
    chown -R appuser:appuser /app /home/appuser /var/log/nginx /tmp

USER appuser

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:7860/_stcore/health || exit 1

CMD ["/app/deployment/entrypoint.sh"]
