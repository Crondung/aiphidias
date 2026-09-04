FROM python:3.13-slim

# Pillow braucht keine Build-Tools mehr (manylinux-Wheels), deshalb kein gcc im Image.
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY aiphidias ./aiphidias

# Nicht als root laufen. Das Blob-Verzeichnis wird als Volume gemountet und
# muss diesem Nutzer gehoeren - siehe docker-compose.yml.
RUN useradd --system --uid 10001 --home /srv aiphidias \
    && mkdir -p /var/lib/aiphidias/blobs \
    && chown -R aiphidias:aiphidias /var/lib/aiphidias
USER aiphidias

EXPOSE 8000
# `--proxy-headers` mit `--forwarded-allow-ips=*`: die App steht ausschließlich
# hinter dem eigenen Caddy im Compose-Netz, von außen ist sie nicht erreichbar.
# Ohne das hält sie jede Anfrage für `http` und baut falsche absolute URLs.
CMD ["uvicorn", "aiphidias.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
