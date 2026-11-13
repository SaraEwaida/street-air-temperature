# Street-level air temperature API.
# The image holds code only; the trained model, grid features and catalog are mounted from ./data:
#   docker build -t street-air-temperature .
#   docker run -p 8000:8000 -v "$(pwd)/data:/app/data" street-air-temperature
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SAT_CONFIG=configs/phoenix.yaml

# LightGBM needs the OpenMP runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[api]"
COPY configs ./configs

RUN useradd --create-home appuser
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1
CMD ["uvicorn", "streetair.api:app", "--host", "0.0.0.0", "--port", "8000"]
