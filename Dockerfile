FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Ensure data directories exist (bind-mounted at runtime, but needed for local builds)
RUN mkdir -p /app/jobs /app/resumes

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
