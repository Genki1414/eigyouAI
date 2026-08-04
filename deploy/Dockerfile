FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Asia/Tokyo

RUN apt-get update && apt-get install -y --no-install-recommends \
      cron tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# FormSender(senders.py)用。ChromiumとOS依存ライブラリを一括インストール
RUN playwright install --with-deps chromium

COPY . .
RUN mkdir -p /app/out /app/data

# APIサーバ（本番はこの前段にnginx/CloudflareでTLS終端を置く）
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/health').status==200 else 1)"

CMD ["python3", "api.py", "serve", "--port", "8787"]
