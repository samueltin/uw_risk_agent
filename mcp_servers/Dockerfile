FROM python:3.11-slim

WORKDIR /app

COPY mcp_servers/requirements-mcp.txt /app/requirements-mcp.txt
RUN pip install --no-cache-dir -r /app/requirements-mcp.txt

COPY mcp_servers /app/mcp_servers

ENV PORT=8001
EXPOSE 8001

CMD ["python", "mcp_servers/risk_server_v4.py"]
