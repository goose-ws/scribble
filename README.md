# ✒️ Scribble

An automated, AI-powered scribe to generate narrative recaps of your TTRPG sessions.

## Overview

Scribble is a tool to help provide AI-powered recaps of TTRPG sessions held over Discord. It's designed to run as a continuous service in a Docker container, watching for new audio recordings and processing them automatically.

## How It Works

The workflow is designed to be as automated as possible after the initial setup:

1.  **Record Audio**: You use **[Craig](https://craig.chat/)** to record your session on Discord.
2.  **Upload Audio**: After the session, you download the multi-track FLAC `.zip` file from Craig and drop it into a designated folder.
3.  **Transcribe**: Scribble detects the new file, unzips it, and uses **[whisperx](https://github.com/m-bain/whisperX)** to perform a time-accurate, speaker-separated transcription of each player's audio track.
4.  **Summarize**: The individual transcripts are merged into a single, time-sorted master transcript. This transcript, along with a custom prompt, is sent to the **Google Gemini API**.
5.  **Deliver**: Gemini's narrative recap is received, formatted, and posted to a **Discord channel** via a webhook.

The process is currently CPU-only. Contributions to add GPU support are welcome\!

## Getting Started

While Scribble could be installed on bare metal, the easiest way to use it is with the provided Docker image.

### Prerequisites

Before you begin, you will need three things:

1.  **Craig**: Invite the [Craig bot](https://craig.chat/) to your Discord server.
2.  **Google Gemini API Key**: Get a key from the [Google AI Studio](https://aistudio.google.com/apikey).
3.  **Discord Webhook URL**: Create a webhook in the Discord channel where you want recaps to be posted. (Server Settings -\> Integrations -\> Webhooks -\> New Webhook).

### Quick Start

1.  Create a directory for your project on your host machine.
    ```bash
    mkdir scribble-server
    cd scribble-server
    ```
2.  Inside that directory, create a `docker-compose.yml` file with the following content:
    ```yaml

    services:
      scribble:
        image: goosews/scribble:latest
        container_name: scribble
        restart: unless-stopped
        environment:
          # --- Required ---
          GEMINI_API_KEY: "YOUR_GEMINI_API_KEY_HERE"
          DISCORD_WEBHOOK: "YOUR_DISCORD_WEBHOOK_URL_HERE"
          
          # --- Optional: User/Group Mapping ---
          PUID: "1000"
          PGID: "1000"
          TZ: "America/New_York"
          
          # --- Optional: Whisper Performance Tuning ---
          OUTPUT_VERBOSITY: "3"
          WHISPER_MODEL: "large-v3"
          WHISPER_THREADS: "24"
          WHISPER_BATCH_SIZE: "24"
          WHISPER_BEAM_SIZE: "3"
          WHISPER_VAD_METHOD: "silero"
          
          SPOOL_TIME: "3600" # Check for new files every hour
        volumes:
          - ./app:/app
    ```
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
      * When your TTRPG session is over, download the **multi-track FLAC zip file** from Craig.
      * Place the entire `.zip` file into the `app/Sessions` directory.
      * Scribble will automatically detect and process the file on its next cycle (defined by `SPOOL_TIME`).

## Configuration

### Environment Variables

| Variable | Required | Default | Description |
| :--- | :--- | :--- | :--- |
| `GEMINI_API_KEY` | **Yes** | `""` | Your API key for the Google Gemini service. |
| `DISCORD_WEBHOOK` | **Yes** | `""` | The URL for the Discord webhook that will post the recap. |
| `PUID` | No | `0` | The User ID for file permissions. Match to your host user. |
| `PGID` | No | `0` | The Group ID for file permissions. Match to your host group. |
| `TZ` | No | `Etc/UTC`| Your local timezone (e.g., `America/New_York`). |
| `SPOOL_TIME` | No | `3600` | Time in seconds to wait between checking for new files. |
| `OUTPUT_VERBOSITY` | No | `3` | `1`: Errors, `2`: Warnings, `3`: Info, `4`: Verbose. |
| `WHISPER_MODEL` | No | `large-v3` | The whisper model to use (e.g., `medium.en`, `base.en`). |
| `WHISPER_THREADS` | No | (all) | Number of CPU threads for whisperx to use. |
| `WHISPER_BATCH_SIZE`| No | `8` | Processes multiple audio chunks in parallel. `24` is a good starting point for high-thread CPUs. |
| `WHISPER_BEAM_SIZE` | No | `5` | `1` is fastest but may be less accurate. `3` may be a good balance. |
| `WHISPER_CHUNK_SIZE`| No | `30` | Time in seconds to merge VAD segments. |
| `WHISPER_VAD_METHOD`| No | `pyannote` | VAD method. `silero` may be significantly faster on CPU. |
| `WHISPER_VAD_ONSET` | No | `0.5` | Voice activity detection onset threshold. |
| `WHISPER_VAD_OFFSET`| No | `0.363` | Voice activity detection offset threshold. |
| `WHISPER_LANGUAGE` | No | `en` | Two-letter language code for the audio. |
| `WHISPER_COMPUTE_TYPE`| No | `int8` | `int8` is recommended for CPU inference. |

### Volumes

  * `- ./app:/app`: This is the main data directory.
      * **`app/prompt.txt`**: This is where you place your prompt file.
      * **`app/Sessions/`**: This is where you drop the `.zip` files from Craig.
      * The container will write all working files and final transcripts into subdirectories within `app/Sessions/`.

## Performance Reference

Processing can be very time-consuming. For context, a 2-hour 30-minute session with 6 total speakers took **just under 7 hours** to transcribe on the following hardware:

  * **Motherboard**: SuperMicro X9DR3-LN4F+
  * **CPU**: Dual Intel E5-2670
  * **RAM**: 96 GB 1066 MHz DDR3
  * **Storage**: 512 GB NVMe SSD

## License

The original code for this project (including the `Dockerfile` and `scribble.bash` script) is licensed under the **MIT License**.

This project relies on `whisperx`, which is a separate project distributed under the **BSD-2-Clause License**. To comply with its license, the original copyright notice for `whisperx` must be retained. You can find the full license for `whisperx` at its GitHub repository: [https://github.com/m-bain/whisperX](https://github.com/m-bain/whisperX).