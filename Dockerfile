# NGA 提醒助手 —— 容器镜像
#
#   docker build -t nga-reminder:local .
#   docker run --rm -p 127.0.0.1:8765:8765 -v nga-data:/data nga-reminder:local
#
# 说明：容器里弹不了桌面通知（没有系统通知后端），
# 程序会自动回退到日志后端（通知内容仍会写进容器日志与网页界面）。
# 想要真正的桌面弹窗：在宿主机上跑通知桥接（见 README）。

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# pyproject 声明了 readme = "README.md"，构建时必须一起带上。
COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

# 运行时可覆盖的环境变量：
#   NGA_HOST / NGA_PORT        监听地址与端口
#   NGA_DB_PATH                SQLite 路径
#   NGA_OPEN_BROWSER           容器里没有浏览器，固定 false
#   NGA_APP_URL                点击通知时打开的地址
#   NGA_UID / NGA_CID          也可以不配，进网页「账号设置」里填
ENV NGA_HOST=0.0.0.0 \
    NGA_PORT=8765 \
    NGA_DB_PATH=/data/nga.db \
    NGA_OPEN_BROWSER=false \
    NGA_APP_URL=http://127.0.0.1:8765/

VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8765/health', timeout=4)"

CMD ["nga-reminder"]
