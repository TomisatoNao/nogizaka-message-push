# ============================================================
# Dockerfile — 乃木坂/樱坂/日向坂 Message、博客与社交媒体监控推送服务
# ============================================================
FROM python:3.12-slim-bookworm

LABEL maintainer="TomisatoNao" \
      description="乃木坂46 / 樱坂46 / 日向坂46 Message、官方博客与社交媒体动态监控推送机器人"

# 设置工作目录与系统环境变量
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Tokyo \
    DEBIAN_FRONTEND=noninteractive \
    WEB_ADMIN_HOST=0.0.0.0

# 安装运行时系统依赖：ffmpeg (视频压制/转码/直播录制)、ca-certificates (HTTPS)、tzdata (时区)、curl (健康检查)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    tzdata \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖（利用 Docker 缓存层加速构建）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用源码、配置模板与启动脚本
COPY main.py .
COPY config.example.json .
COPY entrypoint.sh .
COPY config/ ./config/
COPY src/ ./src/
COPY tools/ ./tools/

# 赋予执行权限并创建数据与日志目录
RUN chmod +x entrypoint.sh && \
    mkdir -p data logs

# 暴露 WebUI 端口
EXPOSE 8787

# 声明持久化目录
VOLUME ["/app/config", "/app/data", "/app/logs"]

# 容器健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8787/api/health/status || exit 1

# 启动入口
ENTRYPOINT ["./entrypoint.sh"]
CMD ["python", "main.py"]
