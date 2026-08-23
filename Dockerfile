FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY data/ data/

ENV PORT=8080
EXPOSE 8080

# Cloud Run sets $PORT; shell form so it expands at container start.
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
