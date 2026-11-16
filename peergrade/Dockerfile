FROM node:22-alpine AS frontend
WORKDIR /build/frontend
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home --uid 10001 peergrade
COPY app.py ai_service.py domain.py models.py manage.py ./
COPY --from=frontend /build/frontend/dist ./frontend/dist
USER peergrade
EXPOSE 8000
# 可信代理来源：IPv4/IPv6 回环 + Docker 默认私有网段 + 运营商级 NAT(100.64/10) + IPv6 ULA(fc00::/7)。
# 覆盖 daemon 启用 IPv6 或自定义 default-address-pools 的场景，否则 XFF 不被信任会让登录限流退回共享桶。
# 必须与 Caddyfile 的 X-Forwarded-For 覆写配套；Caddy 之前若再挂 CDN/额外 LB，需另配 trusted_proxies + {client_ip}。
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10,fc00::/7"]
