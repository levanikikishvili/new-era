FROM python:3.13.5-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NEWERA_API_HOST=0.0.0.0 \
    NEWERA_API_PORT=8787 \
    NEWERA_SAFE_MODE=true \
    NEWERA_CHROMA_ENABLED=false

WORKDIR /app

RUN useradd --create-home --uid 10001 appuser \
    && install -d -m 0755 -o appuser -g appuser \
        /app/TradingAgentsData/memory \
        /app/TradingAgentsData/research

COPY requirements_api_linux.txt requirements_api.lock.txt /tmp/
RUN python -m pip install --no-cache-dir \
        --requirement /tmp/requirements_api_linux.txt

# Copy only the API-side source and static configuration. Secrets and local
# research data are intentionally not part of the image.
COPY main_api.py bot_manager.py kronos_matrix_strategy.py bot_registry.json /app/
COPY configs /app/configs
COPY schemas /app/schemas
COPY newera_api /app/newera_api

RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8787

CMD ["python", "-m", "uvicorn", "main_api:app", "--host", "0.0.0.0", "--port", "8787"]
