FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Render sets $PORT at runtime; default to 8080 for local/docker-compose use.
ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "uvicorn bot:app --host 0.0.0.0 --port ${PORT}"]
