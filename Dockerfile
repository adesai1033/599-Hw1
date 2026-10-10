FROM python:3.13-slim AS builder
WORKDIR /build
COPY requirements.txt .
# --prefix (not the PDF's --user) so the packages land in /install, readable by the non-root user.
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.13-slim
COPY --from=builder /install /usr/local
RUN useradd --create-home appuser
WORKDIR /app
COPY --chown=appuser:appuser . .
USER appuser
ENV MCP_SERVERS_CONFIG=/app/mcp_config.json PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8080
CMD ["python", "main.py"]
