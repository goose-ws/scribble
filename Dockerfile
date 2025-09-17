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
        --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir whisperx

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
    && rm -rf /var/lib/apt/lists/*
    
# Copy the virtual environment from the builder stage
COPY --from=builder /opt/venv /opt/venv

# Install gosu-amd64
RUN curl -o /usr/local/bin/gosu -sSL "https://github.com/tianon/gosu/releases/download/1.18/gosu-amd64" && \
    chmod +x /usr/local/bin/gosu

# Copy the application code
WORKDIR /app
COPY prompt.txt /opt/prompt.txt
COPY --chmod=755 scribble.bash /opt/scribble.bash
COPY --chmod=755 entrypoint.sh /usr/local/bin/entrypoint.sh

# Activate the virtual environment for all subsequent commands
ENV PATH="/opt/venv/bin:$PATH"

# Ignore spammy warnings
ENV PYTHONWARNINGS="ignore:Could not initialize NNPACK"

# Set the entrypoint to our new script
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

# Debug command
#CMD ["sh", "-c", "bash -x /opt/scribble.bash 2>/app/debug.txt"]
# Default command
CMD ["/opt/scribble.bash"]
