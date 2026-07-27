FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8080
# --timeout 300: AI format-learning on first import can exceed gunicorn's
# 30s default, which kills the worker mid-request (Internal Server Error).
# --threads 4: keeps other pages responsive while an import runs.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--timeout", "300", "--threads", "4", "app:app"]
