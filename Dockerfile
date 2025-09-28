# Builder
FROM python:3.12-slim-bookworm AS builder

# Create a virtual environment inside the builder
RUN python -m venv /opt/venv

# Update pip, create venv, and install all python packages in a single RUN layer
ENV PATH="/opt/venv/bin:$PATH"
RUN python -m venv /opt/venv && \
    pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
        torch==2.8.0 \
        torchaudio==2.8.0 \
        --index-url https://download.pytorch.org/whl/cpu
# Add flask and requests for the web uploader
RUN pip install --no-cache-dir \
        whisperx \
        flask \
        requests \
        gunicorn

# Final image
FROM python:3.12-slim-bookworm

# Update packages
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        jq \
        unzip \
        file \
        supervisor \
    && rm -rf /var/lib/apt/lists/*
    
# Copy the virtual environment from the builder stage
COPY --from=builder /opt/venv /opt/venv

# Copy the application code
WORKDIR /app
COPY prompt.txt /opt/prompt.txt
COPY --chmod=755 scribble.bash /opt/scribble.bash
COPY --chmod=755 entrypoint.sh /usr/local/bin/entrypoint.sh
COPY --chmod=755 app.py /opt/app.py
COPY --chmod=755 start-gunicorn.sh /opt/start-gunicorn.sh
COPY templates /opt/templates
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Activate the virtual environment for all subsequent commands
ENV PATH="/opt/venv/bin:$PATH"

# Entrypoint
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]