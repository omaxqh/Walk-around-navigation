FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ROUTESNAP_DATA_DIR=/data

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --system routesnap \
    && useradd --system --gid routesnap --home-dir /app routesnap \
    && mkdir -p /data \
    && chown routesnap:routesnap /data

COPY --chown=routesnap:routesnap app.py emoji_learner.py poi_disambiguate.py ./
COPY --chown=routesnap:routesnap config ./config

USER routesnap

EXPOSE 5001

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import json,urllib.request; data=json.load(urllib.request.urlopen('http://127.0.0.1:5001/health', timeout=3)); assert data['status']=='ok'"

CMD ["gunicorn", "--workers", "2", "--threads", "4", "--timeout", "90", "--bind", "0.0.0.0:5001", "--access-logfile", "-", "--error-logfile", "-", "app:app"]
