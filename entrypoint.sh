#!/bin/sh
set -e

# 确保运行时必要目录存在
mkdir -p /app/config /app/data /app/logs

# 如果挂载的配置目录中没有 config.json，则自动从模板初始化
if [ ! -f /app/config/config.json ]; then
    echo "⚙️ [Docker] 未检测到 /app/config/config.json，自动初始化默认配置..."
    if [ -f /app/config/config.example.json ]; then
        cp /app/config/config.example.json /app/config/config.json
    elif [ -f /app/config.example.json.default ]; then
        cp /app/config.example.json.default /app/config/config.json
    fi
fi

# 如果用户将宿主机空目录挂载到了 /app/config，自动自愈补全缺失的 Python 源码模块
if [ ! -f /app/config/config.py ] && [ -d /app/config.default ]; then
    echo "⚙️ [Docker] 检测到 /app/config 缺失核心源码模块（疑似空目录挂载），执行静默自愈补全..."
    cp -rn /app/config.default/* /app/config/ 2>/dev/null || cp -r /app/config.default/* /app/config/
fi

# 执行传入的命令（默认 python main.py），并保持信号正常转发
exec "$@"
