FROM python:3.12-slim

WORKDIR /app

# 1. Firebird-Client-Bibliothek (wird von fdb benoetigt)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libfbclient2 \
    && rm -rf /var/lib/apt/lists/*

# 2. Python-Bibliotheken mit festen Versionen
RUN pip install --no-cache-dir fdb==2.0.4 prometheus_client==0.26.0

# 3. Exporter und Metrik-Definitionen (Zugangsdaten kommen per Umgebungsvariable)
COPY firebird_exporter_remote.py .
COPY metrics.json .

# 4. Ohne Root-Rechte laufen
RUN useradd --no-create-home --shell /usr/sbin/nologin exporter
USER exporter

EXPOSE 9103

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/metrics' % os.environ.get('EXPORTER_PORT','9103'), timeout=4)"

CMD ["python3", "firebird_exporter_remote.py"]
