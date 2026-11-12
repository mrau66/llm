FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY kb.py service.py mcp_server.py integration.py smoke_mcp.py ./
USER 1000:1000
CMD ["python", "mcp_server.py"]
