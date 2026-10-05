FROM python:3.12-slim

WORKDIR /app

# libreoffice-calc: recálculo headless de fórmulas en los Estados de Resultados (Panel Contable).
# --no-install-recommends evita arrastrar los "recommends" pesados de LibreOffice
# (JRE, fuentes, paquete de ayuda…), que NO se usan para `soffice --headless
# --convert-to xlsx` y inflaban la imagen ~varios cientos de MB → el build de
# Railway fallaba ("Failed to build an image", OOM/disco). El núcleo de Calc se
# instala igual porque va como Depends, no como Recommends.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq-dev gcc libreoffice-calc \
    && rm -rf /var/lib/apt/lists/*

# uv en vez de pip: instala desde uv.lock, asi la imagen tiene exactamente las
# mismas versiones que se probaron en local y en CI.
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

# El venv va FUERA de /app a proposito: el docker-compose monta el repo en /app y
# taparia un /app/.venv de la imagen.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH=/opt/venv/bin:$PATH

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .

EXPOSE 8000

# Las migraciones las corre el servicio `migrate` del docker-compose.yml, no
# esto. El CMD es solo el default para `docker run` suelto: en el compose cada
# servicio (web, worker, beat) trae su propio `command`.
#
# `config.wsgi` y no `app.main`: Django reemplazo a FastAPI el 2026-09-04. El
# paquete `app/` sigue en la imagen como copia congelada (ver CLAUDE.md): ya no
# se sirve, y `apps/` solo le toma un diccionario.
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000"]
