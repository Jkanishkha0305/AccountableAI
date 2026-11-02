FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -e .
ENV FEED_HOST=0.0.0.0
EXPOSE 8765
CMD ["python", "dashboard/feed/serve.py"]
