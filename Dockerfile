# Builder
FROM python:3.12-slim-bookworm AS builder

# Create a virtual environment inside the builder
RUN python -m venv /opt/venv

# Make sure we use the venv pip
ENV PATH="/opt/venv/bin:$PATH"

# Copy the requirements file into the builder
COPY requirements.txt /requirements.txt

# Install everything from the file
# We add --extra-index-url so pip can find the "+cpu" versions of torch defined in your txt file
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /requirements.txt \
    --extra-index-url https://download.pytorch.org/whl/cpu

# Final image
FROM python:3.12-slim-bookworm

# Update system packages
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        jq \
        unzip \
        file \
        supervisor \
        sqlite3 \
    && rm -rf /var/lib/apt/lists/*
    
# Copy the virtual environment from the builder stage
COPY --from=builder /opt/venv /opt/venv

# Copy the application code
WORKDIR /app
COPY prompt.txt /opt/prompt.txt
COPY --chmod=755 scribble.bash /opt/scribble.bash
COPY --chmod=755 entrypoint.sh /usr/local/bin/entrypoint.sh
COPY --chmod=755 start-gunicorn.sh /opt/start-gunicorn.sh
COPY templates /opt/templates
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Activate the virtual environment for all subsequent commands
ENV PATH="/opt/venv/bin:$PATH"

# Entrypoint
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
