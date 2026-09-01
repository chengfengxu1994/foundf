# FoundF — Production Dockerfile (NAS-optimized)
FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app

# Install Python packages and dcron
COPY requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends curl dcron && rm -rf /var/lib/apt/lists/* && pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple/ -r requirements.txt

# Copy application code
COPY . .

# Port
EXPOSE 8000

# Healthcheck — use python instead of curl (no apt-get needed)
HEALTHCHECK --interval=60s --timeout=10s --retries=3 --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/status')"

# Default: API server
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
