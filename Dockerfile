FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/gabrielmahia/kenya-sms"
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -e . 2>/dev/null || pip install --no-cache-dir -r requirements.txt 2>/dev/null || true
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
