#!/bin/bash
#
# Automates the processing of DnD session audio recordings.
# 1. Finds new zipped session recordings.
# 2. Extracts session date from metadata.
# 3. Organizes files into session date folders.
# 4. Transcribes each user's audio file using whisper.cpp.
# 5. Merges and sorts individual transcripts into a master session transcript.

### Functions

function printOutput {
if ! [[ "${1}" =~ ^[0-9]+$ ]]; then
    echo -e "scribble   ::   $(date "+%Y-%m-%d %H:%M:%S")   ::   [${colorRed}error${colorReset}] Invalid message level [${1}] passed to printOutput function"
    return 1
fi

case "${1}" in
    0) logLevel="[${colorRed}reqrd${colorReset}]";; # Required
    1) logLevel="[${colorRed}error${colorReset}]";; # Errors
    2) logLevel="[${colorYellow}warn${colorReset}] ";; # Warnings
    3) logLevel="[${colorGreen}info${colorReset}] ";; # Informational
    4) logLevel="[${colorCyan}verb${colorReset}] ";; # Verbose
    5) logLevel="[${colorPurple}DEBUG${colorReset}]";; # Super Secret Very Excessive Debug Mode
esac
if [[ "${1}" -le "${OUTPUT_VERBOSITY}" ]]; then
    echo -e "scribble   ::   $(date "+%Y-%m-%d %H:%M:%S")   ::   ${logLevel} ${2}"
fi
}

function timeDiff {
# Start time should be passed as ${1}
# End time can be passed as ${2}
# If no end time is defined, will use the time the function is called as the end time
# Time should be provided via: startTime="$(($(date +%s%N)/1000000))"
if [[ -z "${1}" ]]; then
    # No start time provided
    return 1
else
    startTime="${1}"
fi
if [[ -z "${2}" ]]; then
    endTime="$(($(date +%s%N)/1000000))"
fi

if [[ "$(( ${endTime:0:10} - ${startTime:0:10} ))" -le "5" ]]; then
    printf "%sms\n" "$(( endTime - startTime ))"
else
    local T="$(( ${endTime:0:10} - ${startTime:0:10} ))"
    local D="$((T/60/60/24))"
    local H="$((T/60/60%24))"
    local M="$((T/60%60))"
    local S="$((T%60))"
    (( D > 0 )) && printf '%dd' "${D}"
    (( H > 0 )) && printf '%dh' "${H}"
    (( M > 0 )) && printf '%dm' "${M}"
    (( D > 0 || H > 0 || M > 0 ))
    printf '%ds\n' "${S}"
fi
}

function send_chunk {
    local url="${1}"
    local chunk="${2}"

    # Do not send empty chunks.
    if [[ -z "${chunk}" ]]; then
        return
    fi

    printOutput 5 "Sending chunk of [${#chunk}] characters"

    local http_code
    # Use jq to safely create the JSON payload. The --arg flag is the correct
    # way to pass a shell variable into a jq expression.
    # The output is piped directly to curl's stdin via the `-d @-` flag.
    http_code=$(jq -n --arg content_arg "${chunk}" '{content: $content_arg}' | \
        curl -sS \
            -H "Content-Type: application/json" \
            -X POST \
            -d @- \
            --write-out "%{http_code}" \
            -o /dev/null \
            "${url}")

    if [[ "${http_code}" -ge 200 && "${http_code}" -lt 300 ]]; then
        printOutput 5 "Successfully sent chunk (HTTP ${http_code})"
    else
        printOutput 1 "Discord API returned HTTP status [${http_code}]"
        exit 1
    fi
}

function graceful_shutdown {
    printOutput "2" "Shutdown signal received, finishing current task and exiting."
    shutdown_requested="1"
    # If we are in the middle of sleeping, kill the sleep process
    # to allow the loop to terminate immediately.
    if [[ -n "${sleep_pid}" ]]; then
        kill "${sleep_pid}" 2>/dev/null
    fi
}

### Configuration
# Base directory where DnD sessions are stored.
BASE_DIR="/app/Sessions"
if ! [[ -d "${BASE_DIR}" ]]; then
    mkdir -p "${BASE_DIR}" || printOutput "1" "Unable to create base dir [${baseDir}]"; exit 1
fi

# Whisper Options
# Validate WHISPER_MODEL
if [[ -z "${WHISPER_MODEL}" ]]; then
    printOutput "2" "No model defined, defaulting to [large-v3]"
    WHISPER_MODEL="large-v3"
fi

# Validate WHISPER_THREADS
if [[ -n "${WHISPER_THREADS}" ]] && ! [[ "${WHISPER_THREADS}" =~ ^[0-9]+$ ]]; then
    printOutput "2" "Invalid thread count [${WHISPER_THREADS}] -- Setting to [$(nproc)]"
    WHISPER_THREADS="$(nproc)"
else
    if [[ "${WHISPER_THREADS}" -gt "$(nproc)" ]]; then
        printOutput "2" "Invalid thread count [${WHISPER_THREADS}] is greater than nproc [$(nproc)] -- Setting to [$(nproc)]"
        WHISPER_THREADS="$(nproc)"
    fi
fi

# Validate WHISPER_LANGUAGE
if [[ -n "${WHISPER_LANGUAGE}" ]] && ! [[ "${WHISPER_LANGUAGE}" =~ ^[a-z]{2}$ ]]; then
    printOutput "2" "Invalid language code [${WHISPER_LANGUAGE}] -- Using default [en]"
    WHISPER_LANGUAGE="en"
fi

# Validate WHISPER_COMPUTE_TYPE
if [[ -n "${WHISPER_COMPUTE_TYPE}" ]]; then
    case "${WHISPER_COMPUTE_TYPE}" in
        float16|float32|int8)
            # This is a valid type
            ;;
        *)
            printOutput "2" "Invalid compute type [${WHISPER_COMPUTE_TYPE}] -- Using default [int8]"
            WHISPER_COMPUTE_TYPE="int8"
            ;;
    esac
fi

# Validate WHISPER_CHUNK_SIZE (positive integer)
if [[ -n "${WHISPER_CHUNK_SIZE}" ]] && ! [[ "${WHISPER_CHUNK_SIZE}" =~ ^[1-9][0-9]*$ ]]; then
    printOutput "2" "Invalid chunk size [${WHISPER_CHUNK_SIZE}] -- Using default [30]"
    WHISPER_CHUNK_SIZE="30"
fi

# Validate WHISPER_BEAM_SIZE (positive integer)
if [[ -n "${WHISPER_BEAM_SIZE}" ]] && ! [[ "${WHISPER_BEAM_SIZE}" =~ ^[1-9][0-9]*$ ]]; then
    printOutput "2" "Invalid beam size [${WHISPER_BEAM_SIZE}] -- Using default [5]"
    WHISPER_BEAM_SIZE="5"
fi

# Validate WHISPER_BATCH_SIZE (positive integer)
if [[ -n "${WHISPER_BATCH_SIZE}" ]] && ! [[ "${WHISPER_BATCH_SIZE}" =~ ^[1-9][0-9]*$ ]]; then
    printOutput "2" "Invalid batch size [${WHISPER_BATCH_SIZE}] -- Using default [8]"
    WHISPER_BATCH_SIZE="8"
fi

# Validate WHISPER_VAD_ONSET (float)
if [[ -n "${WHISPER_VAD_ONSET}" ]] && ! [[ "${WHISPER_VAD_ONSET}" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
    printOutput "2" "Invalid VAD onset value [${WHISPER_VAD_ONSET}] -- Using default [0.5]"
    WHISPER_VAD_ONSET="0.5"
fi

# Validate WHISPER_VAD_OFFSET (float)
if [[ -n "${WHISPER_VAD_OFFSET}" ]] && ! [[ "${WHISPER_VAD_OFFSET}" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
    printOutput "2" "Invalid VAD offset value [${WHISPER_VAD_OFFSET}] -- Using default [0.363]"
    WHISPER_VAD_OFFSET="0.363"
fi

# Validate WHISPER_VAD_METHOD
if [[ "${WHISPER_VAD_METHOD}" != "pyannote" && "${WHISPER_VAD_METHOD}" != "silero" ]]; then
    printOutput "2" "Invalid VAD method [${WHISPER_VAD_METHOD}] -- Using default [pyannote]"
    WHISPER_VAD_METHOD="pyannote"
fi

# Gemini options
MODEL_ID="gemini-2.5-pro"
GENERATE_CONTENT_API="streamGenerateContent"
PROMPT_FILE="/app/prompt.txt"

if [[ -z "${GEMINI_API_KEY}" ]]; then
    printOutput "1" "No valid Gemini API key set"
    exit 1
fi

# Discord options
DISCORD_MSG_LIMIT="2000"
if [[ -z "${DISCORD_WEBHOOK}" ]]; then
    printOutput "1" "No valid Discord webhook URL set"
    exit 1
fi

# Set default verbosity level (adjust as needed)
if ! [[ "${OUTPUT_VERBOSITY}" =~ ^[1-5]$ ]]; then
    printOutput "1" "Invalid output verbosity [${OUTPUT_VERBOSITY}] -- Assuming 'info' level"
    OUTPUT_VERBOSITY="3"
fi

if ! [[ -e "/app/prompt.txt" ]]; then
    if ! [[ -e "/app/sample_prompt.txt" ]]; then
        cp "/opt/prompt.txt" "/app/sample_prompt.txt"
    fi
    printOutput "1" "No [/app/prompt.txt] found -- Please edit [/app/sample_prompt.txt] to your needs, to [/app/prompt.txt], and re-run"
    exit 1
fi

# Set the default spool time
if ! [[ "${RESPAWN_TIME}" =~ ^[0-9]+$ ]]; then
    printOutput "1" "Invalid spool time [${RESPAWN_TIME}] -- Setting to [3600] seconds (one hour)"
    RESPAWN_TIME="3600"
fi

# Color definitions for printOutput function
colorRed="\033[1;31m"
colorGreen="\033[1;32m"
colorYellow="\033[1;33m"
colorBlue="\033[1;34m"
colorPurple="\033[1;35m"
colorCyan="\033[1;36m"
colorReset="\033[0m"

# Trap SIGINT (Ctrl+C) and SIGTERM (docker stop)
trap graceful_shutdown SIGINT SIGTERM

# Main Execution
while [[ -z "${shutdown_requested}" ]]; do
    totalStartTime="$(($(date +%s%N)/1000000))"
    printOutput 3 "Starting Scribble"

    # Find zipped session files non-recursively.
    mapfile -d '' zip_files < <(find "${BASE_DIR}" -maxdepth 1 -type f -name "craig-*.flac.zip" -print0)

    if (( ${#zip_files[@]} == 0 )); then
        printOutput 3 "No 'craig-*.flac.zip' files found to process."
    fi

    for zip_file in "${zip_files[@]}"; do
        printOutput 3 "Processing Zip File [${zip_file}]"
        startTime="$(($(date +%s%N)/1000000))"
        
        # Make sure the file is done being written by getting its size twice, three seconds apart
        read -ra size_1 < <(du -sb "${file}")
        sleep 3
        read -ra size_2 < <(du -sb "${file}")
        if [[ "${size_1[0]}" -ne "${size_2[0]}" ]]; then
            printOutput "2" "File appears to have changed sized, possibly still being copied/written -- Waiting for size to stabilize"
            while [[ "${size_1[0]}" -ne "${size_2[0]}" ]]; do
                sleep 3
                read -ra size_1 < <(du -sb "${file}")
                sleep 3
                read -ra size_2 < <(du -sb "${file}")
            done
        fi
        printOutput "4" "Verified file"

        # The unzipped folder name is assumed to match the zip filename without the extension.
        unzipped_dir="${zip_file%.zip}"
        info_file="${unzipped_dir}/info.txt"

        # Unzip the archive.
        while read -r line; do
            printOutput "4" "${line}"
        done < <(unzip -o "${zip_file}" -d "${unzipped_dir}")
        printOutput 4 "Unzipped in $(timeDiff "${startTime}")"

        if [[ ! -f "${info_file}" ]]; then
            printOutput 2 "'info.txt' not found in the unzipped folder '${unzipped_dir}'. Skipping."
            continue
        fi

        # Find the start time line and extract the YYYY-MM-DD date.
        while read -r session_date; do
            if [[ "${session_date}" =~ ^"Start time:".* ]]; then
                session_date="${session_date#*:}"
                session_date="${session_date:1:10}"
                break
            fi
        done < "${info_file}"
        
        if [[ -z "${session_date}" ]]; then
            printOutput 2 "Could not find 'Start time:' in ${info_file}. Skipping."
            rm -rf "${unzipped_dir}" # Clean up
            continue
        fi
        workdir="${BASE_DIR}/${session_date}"

        # If session folder already exists, remove the newly unzipped folder.
        # The existing folder will be processed.
        if [[ -d "${workdir}" ]]; then
            printOutput 3 "Work directory '${workdir}' already exists. Assuming it's a work-in-progress."
            printOutput 4 "Removing temporary unzipped folder: ${unzipped_dir}"
            rm -rf "${unzipped_dir}"
        else
            # Otherwise, rename the unzipped folder to be the new work directory.
            printOutput 3 "Creating new work directory by renaming unzipped folder to '${workdir}'"
            mv "${unzipped_dir}" "${workdir}"
        fi
        
        # Remove raw.dat if it exists.
        if [[ -e "${workdir}/raw.dat" ]]; then
            rm -f "${workdir}/raw.dat"
        fi
        
        # Process the session directory (either the pre-existing one or the new one).
        printOutput 3 "--- Starting transcription for session in ${workdir} ---"
        mkdir -p "${workdir}/progress" "${workdir}/transcripts"

        # Find all flac files and loop through them.
        while read -r file; do
            startTime="$(($(date +%s%N)/1000000))"
            filename="$(basename "${file}")"
            # Extract username using bash parameter expansion, as you prefer.
            username="${filename%.flac}"
            username="${username#*-}"

            transcript_json="${workdir}/transcripts/${username}_transcript.json"
            transcript_file="${workdir}/transcripts/${username}_transcript.txt"
            progress_file="${workdir}/progress/${username}.txt"

            # If a transcript for this user already exists, skip transcription.
            if [[ -f "${transcript_file}" ]]; then
                printOutput 4 "Transcript for ${username} already exists. Skipping."
                continue
            fi

            printOutput 3 "Transcribing file [${file}] for user [${username}]"
            unset outputArr transcriptArr

            # Execute whisper command and read its output line by line.
            # This captures both stderr and stdout into our loop.
            while read -r line; do
                if [[ "${line}" =~ .*'Could not initialize NNPACK!' ]]; then
                    continue
                fi
                printOutput "4" "${line}"
                echo "${line}" >> "${progress_file}"
            done < <(whisperx \
                        --model "${WHISPER_MODEL}" \
                        --language "${WHISPER_LANGUAGE}" \
                        --compute_type "${WHISPER_COMPUTE_TYPE}" \
                        --vad_onset "${WHISPER_VAD_ONSET}" \
                        --vad_offset "${WHISPER_VAD_OFFSET}" \
                        --vad_method "${WHISPER_VAD_METHOD}" \
                        --threads "${WHISPER_THREADS}" \
                        --chunk_size "${WHISPER_CHUNK_SIZE}" \
                        --beam_size "${WHISPER_BEAM_SIZE}" \
                        --batch_size "${WHISPER_BATCH_SIZE}" \
                        --output_dir "${workdir}" \
                        --output_format json \
                        --device cpu \
                        --no_align \
                        "${file}" 2>&1)
            
            # Generate a .txt transcript from the json file
            unset transcript_output
            while IFS=$'\t' read -r start_seconds text; do
                # Skip empty lines
                if [[ -z "${text}" ]]; then
                    continue
                else
                    # Remove any leading spaces
                    text="${text# }"
                fi

                # Round the seconds to the nearest whole number
                total_seconds=$(printf "%.0f" "${start_seconds}")

                # Calculate hours, minutes, and seconds
                ss=$((total_seconds % 60))
                mm=$((total_seconds / 60 % 60))
                hh=$((total_seconds / 3600))

                # Print the formatted line
                transcript_output+=("$(printf "[%02d:%02d:%02d] %s\n" "${hh}" "${mm}" "${ss}" "${text}")")
            done < <(jq -r '.segments[] | [.start, .text] | @tsv' "${file%.flac}.json")
            printf '%s\n' "${transcript_output[@]}" > "${transcript_file}"
            
            # Move the json file
            mv "${file%.flac}.json" "${transcript_json}"

            printOutput 3 "Transcription for ${username} complete -- Took $(timeDiff "${startTime}")"
        done < <(find "${workdir}" -type f -name "*.flac")
        printOutput 3 "--- All transcriptions for this session are complete. ---"
        
        session_transcript_file="${workdir}/session_transcript.txt"

        # Write session transcript file
        if ! [[ -f "${session_transcript_file}" ]]; then
            printOutput 3 "--- Merging transcripts for session in ${workdir} ---"
            unset transcript_output
            readarray -t transcript_files < <(find "${workdir}/transcripts/" -type f -name "*_transcript.txt")
            for file in "${transcript_files[@]}"; do
                username="${file##*/}"
                username="${username%_transcript.txt}"
                while read -r ts line; do
                    transcript_output+=("${ts} ${username}: ${line}")
                done < "${file}"
            done
            sort -V -o "${session_transcript_file}" < <(printf '%s\n' "${transcript_output[@]}")
        fi

        gemini_recap_file="${workdir}/gemini_recap.txt"
        if ! [[ -f "${gemini_recap_file}" ]]; then
            # Send the request to Gemini for a recap
            # Process Files and Build JSON Parts
            # To avoid "Argument list too long" errors, we'll stream the construction
            # of the JSON parts into a temporary file instead of using shell variables.

            # Create a temporary file to hold the stream of JSON part objects.
            PARTS_FILE=$(mktemp)
            # Schedule the cleanup of the temp file for when the script exits.
            trap 'rm -f "${PARTS_FILE}" request.json' EXIT

            # Set the correct base64 command and arguments using an array. This is robust.
            declare -a base64_cmd
            if [[ "$(uname)" == "Darwin" ]]; then
                base64_cmd=(base64 -i)
            else
                base64_cmd=(base64 -w 0)
            fi

            # Prep the file upload
            MIME_TYPE=$(file --brief --mime-type "${session_transcript_file}")
            printOutput 4 "Processing file: ${session_transcript_file} [MIME type: ${MIME_TYPE}]"

            # Base64 encode the file and pipe it directly into a jq command.
            # Using -R (raw) and -s (slurp) is the most robust way to read the
            # entire output of the base64 command into a single JSON value.
            "${base64_cmd[@]}" "${session_transcript_file}" | jq -R -s --arg mime_type "${MIME_TYPE}" \
              '{inlineData: {mimeType: $mime_type, data: .}}' >> "${PARTS_FILE}"

            # Define the detailed prompt for the AI.
            PROMPT_TEXT="$(<"${PROMPT_FILE}")"

            # Create the JSON object for the final text prompt and append it to the stream file.
            jq -n --arg text "${PROMPT_TEXT}" '{text: $text}' >> "${PARTS_FILE}"

            # Create the final request.json body using the temporary file.
            jq -n \
              --slurpfile parts_array "${PARTS_FILE}" \
              '{
                contents: [
                  {
                    parts: $parts_array
                  }
                ]
              }' > request.json

            # Execute the API call and capture the full response.
            printOutput 3 "Sending request to the Gemini API..."
            API_RESPONSE=$(curl -s \
              -X POST \
              -H "Content-Type: application/json" \
              "https://generativelanguage.googleapis.com/v1beta/models/${MODEL_ID}:${GENERATE_CONTENT_API}?key=${GEMINI_API_KEY}" \
              -d '@request.json')
              
            if [[ "$(jq ".[0].error.code" <<<"${API_RESPONSE}")" == "503" ]]; then
                printOutput 2 "Received API response 503: $(jq -r ".[0].error.message")"
                printOutput 3 "Skipping processing until next run"
                continue
            fi
              
            printOutput 5 "Received API response:"
            printOutput 5 "------------------------------------------------------------------"
            while read -r line; do
                printOutput 5 "${line}"
            done <<<"${API_RESPONSE}"
            printOutput 5 "------------------------------------------------------------------"

            # Check for errors from the API.
            if ! echo "${API_RESPONSE}" | jq -e 'type == "array"' > /dev/null; then
                printOutput 1 "The API did not return a valid stream (expected a JSON array)."
                printOutput 1 "API Response:"
                echo "${API_RESPONSE}" | jq . # Pretty-print the error JSON
                exit 1
            fi

            # Process the successful response.
            printOutput 3 "Processing response..."
            SUMMARY=$(echo "${API_RESPONSE}" | jq -r '[.[] | .candidates[].content.parts[].text] | add')

            # Check if the summary was generated.
            if [[ -z "${SUMMARY}" ]]; then
                printOutput 1 "Failed to generate a summary from the API response."
                exit 1
            else
                unset SUMMARY_ARR
                while read -r line; do if ! [[ "${line}" == "***" ]]; then SUMMARY_ARR+=("${line}"); fi; done <<<"${SUMMARY}"
                SUMMARY="$(printf '%s\n' "${SUMMARY_ARR[@]}")"
                SUMMARY="${SUMMARY//$'\n\n\n'/$'\n\n'}"
            fi
            printOutput 3 "Summary successfully generated:"
            printOutput 4 "------------------------------------------------------------------"
            while read -r line; do
                printOutput "4" "${line}"
            done <<<"${SUMMARY}"
            printOutput 4 "------------------------------------------------------------------"
            echo "${SUMMARY}" > "${gemini_recap_file}"
            
            # Finally, send the summary to Discord
            formatted_date=$(date -d "${session_date}" +"%B %-d, %Y")
            # Normalize all line endings to LF (\n) for consistent processing.
            SUMMARY="$(<"${gemini_recap_file}")"
            SUMMARY="${SUMMARY//$'\r\n'/$'\n'}"
            SUMMARY="${SUMMARY//$'\r'/$'\n'}"
            SUMMARY="# ${formatted_date} session recap"$'\n\n'"${SUMMARY}"

            # Split message into an array of paragraphs
            # This logic reads the message line by line. It accumulates lines into a
            # 'current_paragraph' variable. When it hits a blank line, it considers
            # the paragraph complete and adds it to the array.
            unset paragraphs
            unset current_paragraph
            # Append two newlines to the end to ensure the loop processes the final paragraph.
            while IFS= read -r line; do
                if [[ -z "${line}" ]]; then
                    # Blank line found: end of a paragraph.
                    if [[ -n "${current_paragraph}" ]]; then
                        # Add the completed paragraph to the array, removing the last trailing newline string.
                        paragraphs+=("${current_paragraph%$'\n'}")
                        current_paragraph=""
                    fi
                else
                    # Not a blank line: append it to the current paragraph.
                    current_paragraph+="${line}"$'\n'
                fi
            done <<< "${SUMMARY}"$'\n\n'

            # Send each paragraph as a separate message
            for paragraph in "${paragraphs[@]}"; do
                # Skip any empty array elements.
                if [[ -z "${paragraph}" ]]; then
                    continue
                fi

                # If a single paragraph is too long, it must be chunked and sent.
                if (( ${#paragraph} > DISCORD_MSG_LIMIT )); then
                    printOutput 2 "A paragraph exceeds the character limit; it will be split mid-paragraph."
                    temp_paragraph="${paragraph}"
                    while ((${#temp_paragraph} > 0)); do
                        send_chunk "${DISCORD_WEBHOOK}" "${temp_paragraph:0:DISCORD_MSG_LIMIT}"
                        temp_paragraph="${temp_paragraph:DISCORD_MSG_LIMIT}"
                        sleep 0.5
                    done
                else
                    # Otherwise, send the whole paragraph as one message.
                    send_chunk "${DISCORD_WEBHOOK}" "${paragraph}"
                fi

                # A brief pause to respect Discord's rate limits between messages.
                sleep 0.5
            done
        fi

        # Clean up the original zip file after processing.
        printOutput 3 "Processing complete for this session. Removing original zip file: ${zip_file}"
        rm -f "${zip_file}"
    done

    printOutput 3 "All processing complete | Execution took $(timeDiff "${totalStartTime}")"
    
    if [[ -n "${shutdown_requested}" ]]; then
        break
    fi

    # Run sleep in the background and get its Process ID (PID)
    printOutput 3 "Sleeping for [${RESPAWN_TIME}] seconds"
    sleep "${RESPAWN_TIME}" &
    sleep_pid="${!}"
    
    # Wait for the sleep command to finish (or be killed by the trap)
    wait "${sleep_pid}"
    unset sleep_pid
done

printOutput "3" "Shutting down Scribble"
exit 0
