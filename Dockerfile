FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SHIFTWISE_DB_PATH=/data/scheduler.db \
    SHIFTWISE_HOST=0.0.0.0 \
    SHIFTWISE_PORT=5000

RUN groupadd --gid 10001 shiftwise \
    && useradd --uid 10001 --gid 10001 --shell /bin/false --create-home --home-dir /home/shiftwise shiftwise \
    && mkdir -p /app /data \
    && chown -R shiftwise:shiftwise /app /data /home/shiftwise

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=shiftwise:shiftwise app.py mock_seed.py docker-entrypoint.sh ./
COPY --chown=shiftwise:shiftwise shiftwise ./shiftwise
COPY --chown=shiftwise:shiftwise templates ./templates

RUN chmod +x /app/docker-entrypoint.sh

USER shiftwise

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/healthz')" || exit 1

EXPOSE 5000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--threads", "2", "--timeout", "30", "--access-logfile", "-", "--error-logfile", "-", "app:app"]
