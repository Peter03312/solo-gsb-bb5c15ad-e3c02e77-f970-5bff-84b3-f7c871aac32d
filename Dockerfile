# 多设备现场录音时间轴对齐服务
# 仅使用 Python 标准库（http.server + sqlite3），无第三方依赖。
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    TIMELINE_HOST=0.0.0.0 \
    TIMELINE_PORT=8000 \
    TIMELINE_DB_PATH=/data/timeline.db

WORKDIR /srv
COPY app ./app

# 数据目录（SQLite 库文件；启动时自动建表初始化）
RUN mkdir -p /data && useradd -r -u 10001 appuser && chown -R appuser /data
USER appuser
VOLUME /data

EXPOSE 8000

# 启动即初始化数据库并监听 HTTP
CMD ["python", "-m", "app"]
