FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server ./server
COPY client ./client
COPY router ./router
COPY shared_logging_utils.py ./shared_logging_utils.py

CMD ["python", "server/server.py", "node1"]
