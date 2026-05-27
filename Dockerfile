# Uses the official Playwright image so Chromium + all system deps are pre-installed.
# Update the tag to match the playwright version in requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1

EXPOSE 8080

# Single worker — required because app.py uses an in-process state dict.
# Timeout 300s to cover slow Jarvis exports.
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:8080", "--workers", "1", "--timeout", "300"]
