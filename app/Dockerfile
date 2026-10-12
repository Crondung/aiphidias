# Zweistufig, und zwar aus einem konkreten Grund: moderngl liefert für
# Python 3.13 keine fertigen Wheels, sein Backend `glcontext` muss übersetzt
# werden - dafür braucht es g++ und die X11- und EGL-Header. Zum *Laufen*
# braucht es davon nichts. Einstufig gebaut hätte das Produktionsimage einen
# vollständigen C++-Compiler an Bord, für nichts.
FROM python:3.13-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        g++ \
        libegl1-mesa-dev \
        libgl1-mesa-dev \
        libx11-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt


FROM python:3.13-slim

# Mesa mit llvmpipe: OpenGL ohne Grafikkarte, gerechnet auf der CPU.
# `libgl1-mesa-dri` ist der Treiber selbst - ohne ihn findet EGL zwar einen
# Kontext, aber keinen Renderer. `fonts-dejavu-core` ist die einzige Schrift,
# die SVG-Text zu sehen bekommt; mehr Auswahl hieße, dass dasselbe SVG je nach
# installiertem Paket ein anderes Bild ergibt.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libegl1 \
        libgl1 \
        libgl1-mesa-dri \
        libglvnd0 \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

COPY aiphidias ./aiphidias

# Nicht als root laufen. Das Blob-Verzeichnis wird als Volume gemountet und
# muss diesem Nutzer gehoeren - siehe docker-compose.yml.
RUN useradd --system --uid 10001 --home /srv aiphidias \
    && mkdir -p /var/lib/aiphidias/blobs \
    && chown -R aiphidias:aiphidias /var/lib/aiphidias
USER aiphidias

# Mesa legt seinen Shader-Cache sonst unter $HOME/.cache an. /srv gehört dem
# Nutzer, aber ein Cache im Image-Layer wäre bei jedem Container-Start leer -
# hier steht er wenigstens an einem beschreibbaren Ort und überlebt Neustarts
# des Prozesses innerhalb eines Containers.
ENV MESA_SHADER_CACHE_DIR=/tmp/mesa-cache

EXPOSE 8000
# `--proxy-headers` mit `--forwarded-allow-ips=*`: die App steht ausschließlich
# hinter dem eigenen Caddy im Compose-Netz, von außen ist sie nicht erreichbar.
# Ohne das hält sie jede Anfrage für `http` und baut falsche absolute URLs.
CMD ["uvicorn", "aiphidias.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
