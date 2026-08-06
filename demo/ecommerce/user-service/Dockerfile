FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src

EXPOSE 8000

# Single worker keeps CPU/memory failure signals easy to read on one process.
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]