FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ app/
COPY config/ config/
# The corpus and its precomputed embeddings ship in the image so the service can seed an empty
# incidents table at startup, with no Ollama and no manual step (issue #19).
COPY corpus/incidents/ corpus/incidents/
COPY corpus/embeddings.jsonl corpus/embeddings.jsonl

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
