# FoundF customer dashboard — small read-only experience image.
FROM python:3.12-slim

WORKDIR /app

COPY deploy/dashboard/requirements.txt .
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple/ \
    -r requirements.txt

COPY api ./api
COPY data_provider ./data_provider
COPY foundf_db ./foundf_db
COPY portfolio_manager ./portfolio_manager
COPY strategy_manager ./strategy_manager
COPY config ./config

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/status')"

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
