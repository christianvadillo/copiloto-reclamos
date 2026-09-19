FROM python:3.12-slim

# Usuario no root: el proceso solo necesita escribir en /app/data (la base SQLite).
RUN useradd --create-home --uid 1000 copiloto
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN mkdir -p /app/data && chown -R copiloto:copiloto /app/data
USER copiloto

ENV COPILOTO_DB_PATH=/app/data/copiloto.db
EXPOSE 8000

# Por defecto levanta el webhook + dashboard. El worker es OTRO contenedor/proceso
# (misma imagen, comando distinto): `docker run <imagen> copiloto worker`.
CMD ["copiloto", "serve", "--host", "0.0.0.0", "--port", "8000"]
