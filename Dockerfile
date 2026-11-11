FROM python:3.11-slim

LABEL maintainer="ywsj"
LABEL description="Telegram Group Guard Bot - 群组管理机器人"

# Version injected by CI (build-arg)
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

WORKDIR /app
COPY requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxcb1 libgl1 libglib2.0-0 libxext6 libsm6 libxrender1 \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt
# 前端依赖必须先下载到源码中，避免容器运行时请求第三方 CDN
COPY app/ ./app/
RUN test -s app/web/static/vendor/bootstrap/css/bootstrap.min.css \
 && test -s app/web/static/vendor/bootstrap/js/bootstrap.bundle.min.js \
 && test -s app/web/static/vendor/bootstrap-icons/font/bootstrap-icons.css \
 && test -s app/web/static/vendor/bootstrap-icons/font/fonts/bootstrap-icons.woff2

EXPOSE 8080
CMD ["python", "-m", "app"]
