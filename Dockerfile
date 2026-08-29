# 构建阶段
FROM python:3.11-slim as builder

WORKDIR /app

COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# 运行阶段
FROM python:3.11-slim

WORKDIR /app

# 从构建阶段拷贝已安装的包
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH

# 拷贝代码
COPY . .

# 环境变量设置
ENV PYTHONPATH=/app
ENV HETZNER_CONFIG_PATH=/app/config.yaml
ENV WEB_CONFIG_PATH=/app/web_config.json

EXPOSE 1227

# 生产入口
CMD ["uvicorn", "production-main:app", "--host", "0.0.0.0", "--port", "1227"]
