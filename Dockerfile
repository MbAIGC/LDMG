# syntax=docker/dockerfile:1

# ---------- Stage 1: 下载静态 docker CLI + compose 插件 ----------
FROM alpine:3.20 AS docker-cli

ARG TARGETARCH
ARG DOCKER_VERSION=28.5.0
ARG COMPOSE_VERSION=v2.35.1

RUN set -eux; \
    case "${TARGETARCH}" in \
        amd64) CLI_ARCH=x86_64; COMPOSE_ARCH=x86_64 ;; \
        arm64) CLI_ARCH=aarch64; COMPOSE_ARCH=aarch64 ;; \
        arm)   CLI_ARCH=armhf; COMPOSE_ARCH=armv7 ;; \
        *) echo "unsupported TARGETARCH: ${TARGETARCH}"; exit 1 ;; \
    esac; \
    apk add --no-cache curl; \
    curl -fsSL "https://download.docker.com/linux/static/stable/${CLI_ARCH}/docker-${DOCKER_VERSION}.tgz" -o docker.tgz; \
    tar -xzf docker.tgz docker/docker; \
    curl -fsSL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-${COMPOSE_ARCH}" -o docker-compose; \
    chmod +x docker-compose; \
    rm -f docker.tgz

# ---------- Stage 2: 运行时（精简 alpine） ----------
FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1

# tzdata：配合 compose 的 TZ 环境变量正确显示本地时间
RUN apk add --no-cache tzdata

# docker CLI 静态二进制 + compose 插件
# 同时提供 docker-compose 独立命令回退（v1 老环境兼容）
COPY --from=docker-cli /docker/docker /usr/local/bin/docker
COPY --from=docker-cli /docker-compose /usr/local/lib/docker/cli-plugins/docker-compose
RUN ln -s /usr/local/lib/docker/cli-plugins/docker-compose /usr/local/bin/docker-compose

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# 启动时自动运行 bot
CMD ["python3", "bot.py"]
