FROM python:3.11-slim

WORKDIR /app

# Install runtime dependencies for MCP stdio server & introspection
RUN pip install --no-cache-dir \
    mcp>=1.2 \
    fastembed>=0.4 \
    numpy>=2.0 \
    pillow>=10.0 \
    fastapi>=0.115 \
    httpx>=0.27

COPY . .

# Pre-initialize SQLite schema
RUN python -c "from src.db import init_db; init_db()"

ENTRYPOINT ["python", "run_mcp.py"]
