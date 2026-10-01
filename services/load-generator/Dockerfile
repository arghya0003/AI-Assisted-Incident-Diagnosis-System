# 3.11, not 3.12: pymongo 3.13 (needed for mongo:3.4) has no 3.12 wheels.
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py orders_store.py ./

EXPOSE 5002
CMD ["python", "main.py"]
