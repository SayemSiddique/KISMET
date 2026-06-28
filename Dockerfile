FROM python:3.12-slim

WORKDIR /app

# Install build deps for Pillow / rembg native extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libffi-dev \
        libjpeg-dev \
        libpng-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src/ ./src/

RUN pip install --no-cache-dir ".[web]"

EXPOSE 8000

CMD ["uvicorn", "src.web:build_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
