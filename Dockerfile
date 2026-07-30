# Base image with Python already installed
FROM python:3.12-slim

# Install ffmpeg (required by yt-dlp to merge separate video+audio streams)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (better build caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the bot's code
COPY . .

CMD ["python", "main.py"]
