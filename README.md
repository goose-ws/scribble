![alt text](https://github.com/goose-ws/scribble/blob/main/Scribble.png?raw=true "Scribble")

# Scribble

An automated, AI-powered scribe to generate narrative recaps of your Discord TTRPG sessions.

## Overview

Scribble is a tool to help provide AI-powered recaps of TTRPG sessions held over Discord. It is designed to run as a continuous service in a Docker container, watching for new audio recordings and processing them automatically.

It supports multiple LLM providers (**Google (Gemini)**, **OpenAI (ChatGPT)**, **Anthropic (Claude)**, and **Ollama**) and includes a web dashboard for file management, system statistics, and session monitoring.

> [!NOTE]
> *While I can verify it works with Gemini, Anthropic, and Ollama, there is no free API for OpenAI, so that endpoint is untested for now. Big thanks to @SnoFox for help testing the Anthropic API endpoint.*

## How It Works

The workflow is designed to be as automated as possible. The entire pipeline runs locally on your machine, with the exception of the final step (AI recap generation), which sends text to an external provider (unless you use a local Ollama instance).

1. **Record Audio**: You use **[Craig](https://craig.chat/)** to record your session on Discord.
2. **Upload Audio**: After the session, you download the multi-track FLAC `.zip` file from Craig and upload it via the Scribble Web UI.
> [!NOTE]
> **It must be the mult-track FLAC recording**
3. **Transcribe (Local)**: Scribble takes the file, unzips it, and uses **[faster-whisper](https://github.com/SYSTRAN/faster-whisper)** to locally transcribe each player's audio track.
4. **Summarize (AI)**: The individual transcripts are merged into a single, time-sorted master transcript. This transcript, along with a custom prompt, is sent to your LLM Provider of choice (or a local Ollama instance) to generate a narrative recap.
5. **Deliver**: The AI's narrative recap is received, formatted, and posted to Discord. Optionally, you can have the recap passed to a custom script for further processing.

## Getting Started

Scribble is designed to be deployed using Docker.

### Prerequisites

1. **Craig**: Invite the [Craig bot](https://craig.chat/) to your Discord server.
2. **LLM AI Provider**: An API Key for Google Gemini, OpenAI, or Anthropic. Alternatively, a URL for a local Ollama instance.
3. **Discord Webhook URL**: For a **Forum-style text channel** in Discord, where you want recaps to be posted.

### Setup

1. Create a directory for your project:
```bash
mkdir scribble
cd scribble

```


2. Create a `docker-compose.yaml` file (see examples below).
3. Start the container:
```bash
docker compose up -d

```



### Container Configuration

Scribble requires a persistent volume mapped to `/data` to store your database, configuration, and archive files.

> [!IMPORTANT]
> **Disk Space Warning:** If you enable "Archive Zip Files" in the settings, the original audio zip files will be retained in `/data/archive`. Audio files can be large; ensure your host has sufficient disk space or disable archiving to save space.

### Environment Variables

Configuration is primarily handled via the Web UI, so only a few environment variables are needed for the container itself:

| Variable | Default | Description |
| --- | --- | --- |
| `TZ` | `UTC` | Timezone identifier (e.g., `America/New_York`) for accurate session timestamps. |
| `PUID` | `1000` | User ID to run the application as. |
| `PGID` | `1000` | Group ID to run the application as. |

### Docker Compose Examples

#### NVIDIA GPU (CUDA)

Recommended for faster transcription. Requires the NVIDIA Container Toolkit. This image is about 2.48GB in size, without language transcription models.

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

#### CPU Only

Use this if you do not have a compatible GPU. Transcription will be significantly slower. This image is about 3.5GB in size, without language transcription models.

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

## Web Interface

Access the dashboard at `http://your-server-ip:13131`.

### Initial Login

On the first run, Scribble will generate a random **temporary password**. You must check the container logs to retrieve it:

```bash
docker logs scribble

```

Look for a banner containing: `FIRST RUN - TEMPORARY PASSWORD`. Log in with this password and immediately go to **Settings** to change it.

### Settings Configuration

#### Database

Scribble supports **SQLite** (default), **MariaDB/MySQL**, and **PostgreSQL**.

* **Changing DBs:** You can switch database types in the **Settings** menu.
* **Note:** Changing the database connection requires a container restart to take effect. By default, Scribble will **not** automatically move your data; if you simply switch settings, you will start with a fresh, empty instance.

**Migrating Data Between Databases**
If you want to move your existing data (e.g., from SQLite to Postgres), Scribble includes a built-in migration utility.

1. Ensure your Scribble container is running and connected to your **current** (source) database.
2. Run the interactive migration script inside the container:
```bash
docker exec -it scribble python db_migrate.py
```

3. Follow the prompts to select your **destination** database type and enter the connection details (Host, User, Password, Database Name).
4. Once the migration reports "SUCCESS," go to the Scribble **Settings** page in your browser.
5. Update the Database settings to match the **new** destination you just migrated to.
6. Restart the container:
```bash
docker restart scribble
```

#### Whisper (Transcription)

* **HF Token:** An optional Hugging Face token. This is only required if you are attempting to use a gated or private model from the Hugging Face Hub, although allegedly you will have less rate limited downloads of public models if providing a token.
* **Model/Threads:** Configure the size of the Whisper model (e.g., `small`, `medium`, `large-v3`) and thread usage. List of models available [on OpenAI's HuggingFace page](https://huggingface.co/openai/models). Note that only models with the `whisper-` prefix are whisper models; however, the `whisper-` is not included in the web configuration option. So if you want to use the `whisper-base.en` model, you would enter `base.en` in the web configuration.

#### VAD (Voice Activity Detection)

Currently, **Silero VAD** is the supported method for the faster-whisper backend. You can tune the onset/offset thresholds to adjust sensitivity to silence.

#### LLM Provider

Select your AI provider (Google, OpenAI, Anthropic, or Ollama).

* **API Key:** Required for cloud providers.
* **Model Name:** The specific model ID (e.g., `gemini-2.0-flash`, `gpt-4o`, `claude-3-opus`).
* **Ollama:** Requires a reachable URL (e.g., `http://192.168.1.50:11434`).
* *More info available on the wiki:* [LLM AI Providers](https://github.com/goose-ws/scribble/wiki/LLM-AI-Providers)

#### System Settings

* **Archive Zip Files:** If enabled, keeps the uploaded source zip in `/data/archive`. This allows you to "Re-Transcribe" a session later without re-uploading, but consumes disk space.
* **DB Space Saver:** Truncates the data uploads and thought signatures from JSON logs in the database to keep the file size manageable. **Recommended to enable this.**
* **WebUI Password:** Update your login password here.

## Usage

### Campaigns

You can manage multiple TTRPG campaigns, each with its own specific settings:

* **Discord Webhook:** Where the recaps for this campaign will be posted.
* **System Prompt:** The instruction set sent to the AI. You can use variables to dynamically insert session data:
  * `${campaignName}`
  * `${sessionNumber}`
  * `${sessionDate}`

* **Custom Scripts:** You can place executable scripts (bash, python, etc.) in the `/data/scripts` directory. These can be selected per campaign to run automatically after a recap is generated. The script receives two arguments as positional parameters:
1. Path to the generated Recap text file (Markdown format)
2. Path to the full Transcript text file (Plain text format)

Shell utilities in the container include `ffmpeg`, `curl`, `git`, and `jq`. The container uses `python3`. For complete list of container packages, use:

```bash
docker exec scribble ls /bin
docker exec scribble pip list
```

### Uploading Sessions

1. Go to the **Upload** page.
2. Select the **Campaign**.
3. **Session Number**: This is auto-calculated based on previous sessions but can be manually edited if you are uploading out of order.
4. Drop in your Craig `.zip` file.

### SSL / Reverse Proxy

It is recommended to run Scribble behind a reverse proxy like Nginx for SSL security.

* *Configuration examples available on the wiki:* [Nginx - Reverse Proxy](https://github.com/goose-ws/scribble/wiki/Nginx---Reverse-Proxy)

## Features

* **Multi-Provider Support**: Switch easily between Gemini, OpenAI, Claude, or local Ollama models.
* **Campaign Management**: Distinct configuration, webhooks, and prompts for different groups.
* **Interactive Dashboard**: View session statistics, token usage, and costs.
* **Session Management**:
* View and edit the Master Transcript.
* View and edit individual User Transcripts.
* Regenerate summaries or re-run specific pipeline steps.
* Download Transcripts or Recaps as PDF or Text.


* **Archive & Restore**: Keep source files to allow re-processing of old sessions with new models or prompts.

## License

The original code for this project is licensed under the **MIT License**.

This project relies on **[faster-whisper](https://github.com/SYSTRAN/faster-whisper)**, which is distributed under the **MIT License**.