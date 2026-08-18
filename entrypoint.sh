#!/bin/sh
set -e

# 确保运行时必要目录存在
mkdir -p /app/config /app/data /app/logs

# 如果挂载的配置目录中没有 config.json，则自动从模板初始化
if [ ! -f /app/config/config.json ]; then
    echo "⚙️ [Docker] 未检测到 /app/config/config.json，自动初始化默认配置..."
    cp /app/config.example.json /app/config/config.json
fi

# 执行传入的命令（默认 python main.py），并保持信号正常转发
exec "$@"
