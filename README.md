![alt text](https://github.com/goose-ws/scribble/blob/main/scribble.png?raw=true "Scribble")

[![Pulls on DockerHub](https://img.shields.io/docker/pulls/goosews/scribble.svg?style=for-the-badge&label=DockerHub%20pulls&logo=docker)](https://hub.docker.com/r/goosews/scribble)
[![Stars on DockerHub](https://img.shields.io/docker/stars/goosews/scribble.svg?style=for-the-badge&label=DockerHub%20stars&logo=docker)](https://hub.docker.com/r/goosews/scribble)
[![Stars on GitHub](https://img.shields.io/github/stars/goose-ws/scribble.svg?style=for-the-badge&label=GitHub%20Stars&logo=github)](https://github.com/goose-ws/scribble)

# Scribble

An automated, AI-powered scribe to generate narrative recaps of your TTRPG sessions.

## Overview

Scribble is a tool to help provide AI-powered recaps of TTRPG sessions held over Discord. It is designed to run as a continuous service in a Docker container, watching for new audio recordings and processing them automatically.

It supports multiple LLM providers (**Google Gemini**, **OpenAI**, **Anthropic Claude**, and **Ollama**) and includes a web dashboard for file management, system statistics, and session monitoring.*

*\* While I can verify it works with Gemini and Ollama, there are no free API tiers for OpenAI or Anthropic, so those endpoints are untested for now.*

## How It Works

The workflow is designed to be as automated as possible after the initial setup:

1.  **Record Audio**: You use **[Craig](https://craig.chat/)** to record your session on Discord.
2.  **Upload Audio**: After the session, you download the multi-track FLAC `.zip` file from Craig and upload it via the Scribble Web UI (or drop it into the `Sessions` folder).
3.  **Transcribe**: Scribble detects the new file, unzips it, and uses **[whisperx](https://github.com/m-bain/whisperX)** to perform a time-accurate, speaker-separated transcription of each player's audio track.
4.  **Summarize**: The individual transcripts are merged into a single, time-sorted master transcript. This transcript, along with a custom prompt, is sent to your configured **LLM Provider**.
5.  **Deliver**: The AI's narrative recap is received, formatted, and posted to a **Discord channel** via a webhook.
6.  **Track**: Detailed metrics (tokens used, estimated cost, API latency) are logged to a database and visualized on the **Statistics** page.

The process is currently CPU-only, as that is what I am hardware limited to working with/testing. Contributions to add GPU support are welcome!

## Getting Started

While Scribble could be installed on bare metal, the easiest way to use it is with the provided Docker image.

### Prerequisites

Before you begin, you will need:

1.  **Craig**: Invite the [Craig bot](https://craig.chat/) to your Discord server.
2.  **LLM AI Provider**: An API Key for Google Gemini, OpenAI, or Anthropic. Alternatively, a URL for a local Ollama instance.
3.  **Discord Webhook URL**: For a **Forum-style text channel** in Discord, where you want recaps to be posted. (Server Settings -> Integrations -> Webhooks -> New Webhook).

### Quick Start

1.  Create a directory for your project on your host machine.
    ```bash
    mkdir scribble-server
    cd scribble-server
    ```
2.  Inside that directory, create a `docker-compose.yml` file (see example below).
3.  Create the `app` directory that you referenced in the `volumes` section:
    ```bash
    mkdir app
    ```
4.  Start the container:
    ```bash
    docker compose up -d
    ```
5.  **First-Run Setup**:
      * The container will automatically create `app/Sessions` and `app/sample_prompt.txt`.
      * Edit `app/sample_prompt.txt` to define the instructions for the AI.
      * When you are satisfied, **rename it to `prompt.txt`**.
6.  **Usage**:
      * Access the Web UI at `http://your-server-ip:12345`.
      * Log in using the password you set in `WEB_PASSWORD`.
      * Upload your Craig `.zip` file via the **Upload** page.
      * Monitor progress on the **Status** page.

## Configuration

### Docker Compose Example

```yaml
services:
  scribble:
    image: goosews/scribble:latest
    container_name: scribble
    restart: unless-stopped
    ports:
      - "12345:12345" # Flask Web UI
    environment:
      # --- Required ---
      # Choose one provider: google, openai, anthropic, ollama
      LLM_PROVIDER: "google" 
      LLM_API_KEY: "YOUR_API_KEY_HERE"
      LLM_MODEL: "gemini-2.5-flash"
      DISCORD_WEBHOOK: "YOUR_DISCORD_WEBHOOK_URL_HERE"

      # --- Optional: Web UI Security ---
      WEB_PASSWORD: "change_me"
      WEB_COOKIE_KEY: "random_secret_string" # For session security

      # --- Optional: Cost Tracking (Per Million Tokens) ---
      TOKEN_COST_INPUT: "0.075"   # Example cost per 1M input tokens
      TOKEN_COST_OUTPUT: "0.30"   # Example cost per 1M output tokens

      # --- Optional: Whisper Performance Tuning ---
      OUTPUT_VERBOSITY: "3"
      WHISPER_MODEL: "large-v3"
      WHISPER_THREADS: "24"
      WHISPER_BATCH_SIZE: "24"
      
      RESPAWN_TIME: "3600" # Check for new files every hour
    volumes:
      - ./app:/app

```

### Environment Variables

#### General

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `DISCORD_WEBHOOK` | **Yes** | `(not set)` | The URL for the Discord webhook. |
| `PUID` / `PGID` | No | `0` | User/Group ID for file permissions. |
| `TZ` | No | `Etc/UTC` | Local timezone. |
| `RESPAWN_TIME` | No | `3600` | Wait time (seconds) between processing cycles. |
| `OUTPUT_VERBOSITY` | No | `3` | `1`: Errors, `2`: Warnings, `3`: Info, `4`: Verbose. |
| `KEEP_AUDIO` | No | `true` | Set to `false` to delete FLAC files after processing. |

#### LLM Configuration

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `LLM_PROVIDER` | **Yes** | `(not set)` | `google`, `openai`, `anthropic`, or `ollama`. |
| `LLM_API_KEY` | **Yes** | `(not set)` | API Key (Required for cloud providers). |
| `LLM_MODEL` | **Yes** | `(not set)` | Model name (e.g., `gpt-4o`, `claude-3-5-sonnet`, `gemini-1.5-pro`). |
| `OLLAMA_URL` | *If Ollama* | `(not set)` | Full URL to Ollama instance (e.g., `http://192.168.1.50:11434`). |
| `TOKEN_COST_INPUT` | No | `0` | Cost in USD per 1 Million input tokens (for stats). |
| `TOKEN_COST_OUTPUT` | No | `0` | Cost in USD per 1 Million output tokens (for stats). |

#### Web UI

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `WEB_PASSWORD` | No | (random) | Password for the web interface. |
| `WEB_COOKIE_KEY` | No | (random) | Secret key for Flask sessions. |

#### Whisper Tuning

| Variable | Default | Description |
| --- | --- | --- |
| `WHISPER_MODEL` | `large-v3` | The whisper model to use. |
| `WHISPER_THREADS` | (all) | Number of CPU threads for whisperx. |
| `WHISPER_BATCH_SIZE` | `8` | Parallel processing batch size. |
| `WHISPER_BEAM_SIZE` | `5` | Beam search size (1-5). |
| `WHISPER_VAD_METHOD` | `pyannote` | `silero` is faster, `pyannote` is more accurate. |
| `WHISPER_COMPUTE_TYPE` | `int8` | Quantization type (int8 recommended for CPU). |

## Features

* **Multi-Provider Support**: Switch easily between Gemini, OpenAI, Claude, or local Ollama models.
* **Web Dashboard**:
* **Upload**: Drag-and-drop or URL upload for session zips.
* **Status**: View progress bars, read logs, and download transcripts.
* **Statistics**: Visualize token usage, costs, and API latency over time.
* **Prompt Editor**: Edit the system prompt directly from the browser.

* **Session Management**:
* Retry specific steps (Re-Transcribe, Re-Build Transcript, Re-Generate Recap) with a single click.
* Automatic file cleanup (optional).

## Performance Reference

Processing is CPU-intensive. On a dual Intel E5-2670 system (24 threads), a 2.5-hour session with 6 speakers takes approximately **7 hours** to fully transcribe using `large-v3`.

## TODO

* Move from state based processing to action based processing
* Add GPU support for WhisperX

## License

The original code for this project is licensed under the **MIT License**.
This project relies on `whisperx`, which is distributed under the **BSD-2-Clause License**.