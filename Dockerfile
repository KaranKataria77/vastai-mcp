FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY vastai_mcp/ ./vastai_mcp/

ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8000
EXPOSE 8000

CMD ["python", "-m", "vastai_mcp.server_http"]
