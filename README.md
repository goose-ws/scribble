![alt text](https://github.com/goose-ws/scribble/blob/main/Scribble.png?raw=true "Scribble")

# Scribble

An automated, AI-powered scribe to generate narrative recaps of your Discord TTRPG sessions.

## Overview

Scribble is a tool to help provide AI-powered recaps of TTRPG sessions held over Discord. It is designed to run as a continuous service in a Docker container, watching for new audio recordings and processing them automatically.

It supports multiple LLM providers (**Google (Gemini)**, **OpenAI (ChatGPT)**\*, **Anthropic (Claude)**, and **Ollama**) and includes a web dashboard for file management, system statistics, and session monitoring.

*\* While I can verify it works with Gemini, Anthropic, and Ollama, there are no free API for OpenAI, so that endpoints is untested for now. Big thanks to @SnoFox for help testing the Anthropic API endpoint.*

## How It Works

The workflow is designed to be as automated as possible after the initial setup:

1.  **Record Audio**: You use **[Craig](https://craig.chat/)** to record your session on Discord.
2.  **Upload Audio**: After the session, you download the multi-track FLAC `.zip` file from Craig and upload it via the Scribble Web UI.
3.  **Transcribe**: Scribble takes the file, unzips it, and uses **[faster-whisper](https://github.com/SYSTRAN/faster-whisper)** to transcribe each player's audio track.
4.  **Summarize**: The individual transcripts are merged into a single, time-sorted master transcript. This transcript, along with a custom prompt, is sent to your LLM Provider of choice.
5.  **Deliver**: The AI's narrative recap is received, formatted, and posted to Discord. Optionally, you can have the recap passed to a custom script, as well.

## Getting Started

While Scribble could be installed on bare metal, the easiest way to use it is with the provided Docker image. It comes in two flavors:

```
ghcr.io/goose-ws/scribble:cuda-latest
```

This image has CUDA support, and is 3.4 GB in size (Without models)

```
ghcr.io/goose-ws/scribble:cpu-latest
```

This image does not have CUDA support, and is 1.4 GB in size (Without models)

### Prerequisites

Before you begin, you will need:

1.  **Craig**: Invite the [Craig bot](https://craig.chat/) to your Discord server.
2.  **LLM AI Provider**: An API Key for Google Gemini, OpenAI, or Anthropic. Alternatively, a URL for a local Ollama instance.
3.  **Discord Webhook URL**: For a **Forum-style text channel** in Discord, where you want recaps to be posted. (Server Settings -> Integrations -> Webhooks -> New Webhook).

### Quick Start

1.  Create a directory for your project on your host machine.
    ```bash
    mkdir scribble
    cd scribble
    ```
2.  Inside that directory, create a `docker-compose.yaml` file (see example below).
3.  Create the `data` directory that you referenced in the `volumes` section:
    ```bash
    mkdir data
    ```
4.  Start the container:
    ```bash
    docker compose up -d
    ```
5.  **First-Run Setup**:
      * The container will set a default password for the web UI, viewable in the docker logs.
      * Log in, go to the **Settings** page, and configure your setup to your liking.
6.  **Usage**:
      * Access the Web UI at `http://your-server-ip:13131`.
      * Upload your Craig `.zip` file via the **Upload** page.

## Configuration

### Docker Compose Example

#### With CUDA support

```yaml
services:
  scribble:
    container_name: scribble
    hostname: scribble
    image: ghcr.io/goose-ws/scribble:cuda-latest
    ports:
      - 13131:13131
    environment:
      TZ: "America/New_York"
      PUID: "1000"
      PGID: "1000"
    volumes:
      - "./data:/data"
    deploy:
      resources:
        reservations:
          devices:
          - driver: nvidia
            count: all
            capabilities: [gpu]
    restart: unless-stopped
    logging:
      driver: json-file
      options:
        max-file: "1"
        max-size: "10M"
```

#### Without CUDA support

```yaml
services:
  scribble:
    container_name: scribble
    hostname: scribble
    image: ghcr.io/goose-ws/scribble:cpu-latest
    ports:
      - 13131:13131
    environment:
      TZ: "America/New_York"
      PUID: "1000"
      PGID: "1000"
    volumes:
      - "./data:/data"
    restart: unless-stopped
    logging:
      driver: json-file
      options:
        max-file: "1"
        max-size: "10M"
```

### Environment Variables

#### General

| Variable | Default | Description |
| --- | --- | --- |
| `PUID` | `1000` | User ID to run as. |
| `PGID` | `1000` | Group ID to run as. |
| `TZ` | `UTC` | Timezone identifer - [List of TZ's](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones#List). |

### Settings

* By default, Scribble uses sqlite for storage -- There is support for MariaDB/MySQL, and PostgreSQL. Just be sure to restart the container after changing your DB settings.

`TODO`

## Features

* **Multi-Provider Support**: Switch easily between Gemini, OpenAI, Claude, or local Ollama models.
* **Multi-Campaign Support**: Separate Discord endpoints and LLM AI prompt options for different campaigns.
* **Web Upload**: Drag-and-drop support for zip files.
* **Session Status**: View progress bars, read logs, and download transcripts.
* **Dashboard Statistics**: Visualize token usage, costs, and API latency over time.
* **Archiving**: Choose to archive or remove the zip files once done processing.

## License

The original code for this project is licensed under the **MIT License**.

This project relies on **[faster-whisper](https://github.com/SYSTRAN/faster-whisper)**, which is distributed under the **MIT License**.