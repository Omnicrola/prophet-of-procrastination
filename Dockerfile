# syntax=docker/dockerfile:1
FROM python:3.11-slim

# Install build deps for lxml (stripped out after install to save space)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libxml2 \
        libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies as a separate layer for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot source
COPY bot/ bot/

# Create non-root user and data directory
RUN useradd --no-create-home --shell /bin/false botuser \
    && mkdir -p /data \
    && chown botuser:botuser /data

USER botuser

# The bot makes only outbound connections — no ports to expose
CMD ["python", "-m", "bot.main"]
