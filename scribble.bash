#!/bin/bash
#
# Automates the processing of DnD session audio recordings.
# 1. Finds new zipped session recordings.
# 2. Extracts session date from metadata.
# 3. Organizes files into session date folders.
# 4. Transcribes each user's audio file using whisper.cpp.
# 5. Merges and sorts individual transcripts into a master session transcript.

# Let supervisord finish spinning up
sleep 3

# Color definitions for printOutput function
colorRed="\033[1;31m"
colorGreen="\033[1;32m"
colorYellow="\033[1;33m"
colorPurple="\033[1;35m"
colorCyan="\033[1;36m"
colorReset="\033[0m"

### Functions

function printOutput {
    # Verify input level is an integer
    if ! [[ "${1}" =~ ^[0-9]+$ ]]; then
        echo -e "scribble   ::   $(date "+%Y-%m-%d %H:%M:%S")   ::   [${colorRed}error${colorReset}] Invalid message level [${1}] passed to printOutput function"
        return 1
    fi

    local level_num
    local color
    local logLevel
    local message
    
    level_num="${1}"
    message="${2}"
    message="${message//${LLM_API_KEY}/${LLM_API_KEY_CENSORED}}"
    message="${message//${DISCORD_WEBHOOK}/${DISCORD_WEBHOOK_CENSORED}}"
    if [[ -n "${DISCORD_WEBHOOK_THREAD}" ]]; then
        message="${message//${DISCORD_WEBHOOK_THREAD}/${DISCORD_WEBHOOK_THREAD_CENSORED}}"
    fi
    logLevel=""
    unset color

    case "${level_num}" in
        0) logLevel="reqrd"; color="${colorRed}";;
        1) logLevel="error"; color="${colorRed}";; 
        2) logLevel="warn "; color="${colorYellow}";;
        3) logLevel="info "; color="${colorGreen}";; 
        4) logLevel="verb "; color="${colorCyan}";;
        5) logLevel="DEBUG"; color="${colorPurple}";;
    esac

    local timestamp
    local formatted_msg
    local clean_msg
    
    timestamp="$(date "+%Y-%m-%d %H:%M:%S")"
    formatted_msg="scribble   ::   ${timestamp}   ::   [${color}${logLevel}${colorReset}] ${message}"
    clean_msg="scribble   ::   ${timestamp}   ::   [${logLevel}] ${message}"

    # Print to Stdout for Docker
    if [[ "${level_num}" -le "${OUTPUT_VERBOSITY}" ]]; then
        echo -e "${formatted_msg}"
    fi

    # Write to Session Log (if variable is set)
    # We strip color codes for the text file
    if [[ -n "${sessionLog}" ]]; then
        echo "${clean_msg}" >> "${sessionLog}"
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
local url
local chunk

url="${1}"
chunk="${2}"

# Do not send empty chunks.
if [[ -z "${chunk}" ]]; then
    return
fi

printOutput "5" "Sending chunk of [${#chunk}] characters"

local http_code
local chunk_coded
# Use jq to safely create the JSON payload. The --arg flag is the correct
# way to pass a shell variable into a jq expression.
chunk_coded="$(jq -n --arg content_arg "${chunk}" '{content: $content_arg}')"
printOutput "5" "Issuing curl command [curl -sS -H \"Content-Type: application/json\" -X POST -d \"${chunk_coded}\" --write-out \"%{http_code}\" -o /dev/null \"${TARGET_WEBHOOK}\")]"
http_code=$(curl -sS \
        -H "Content-Type: application/json" \
        -X POST \
        -d "${chunk_coded}" \
        --write-out "%{http_code}" \
        -o "/dev/null" \
        "${url}")

if [[ "${http_code}" -ge "200" && "${http_code}" -lt "300" ]]; then
    printOutput "5" "Successfully sent chunk [HTTP code ${http_code}]"
else
    printOutput "1" "Discord API returned HTTP status [${http_code}]"
    return 1
fi
}

function censorData {
if [[ -z "${1}" ]]; then
    return 1
fi
local data
data="${1}"

dataMiddle="${data:3:-3}"
dataMiddle="${dataMiddle//?/*}"
dataOutput="${data:0:3}${dataMiddle}${data: -3}"

echo "${dataOutput}"
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

function sqDb {
    if [[ -z "${1}" ]]; then
        return 1
    fi
    local sqOutput sqExit
    
    # FIX: Pass ${1} into sqlite3 via stdin (<<<) instead of as an argument.
    # This bypasses the ARG_MAX limit for large JSON payloads.
    sqOutput="$(sqlite3 "${sqliteDb}" <<< "${1}" 2>&1)"
    sqExit="${?}"
    
    if [[ "${sqExit}" -eq "0" ]]; then
        if [[ -n "${sqOutput}" ]]; then
            echo "${sqOutput}"
        fi
        return 0
    else
        # Prepare the log query
        # We must escape single quotes in the output/command for the log SQL wrapper
        local safeCommand="${1//\'/\'\'}"
        local safeOutput="${sqOutput//\'/\'\'}"
        local logQuery="INSERT INTO sqLog (TIME, COMMAND, OUTPUT) VALUES ('$(date)', '${safeCommand}', '[Exit Code: ${sqExit}] ${safeOutput}');"

        # FIX: Also pass the logging query via stdin, or this will fail too
        sqlite3 "${sqliteDb}" <<< "${logQuery}"
        
        if [[ -n "${sqOutput}" ]]; then
            echo "${sqOutput}"
        fi
        return 1
    fi
}

function calculateTokenCost {
if [[ -z "${1}" || -z "${2}" ]]; then
    return 1
fi

local tokensIn tokensOut
tokensIn="${1:-0}"
tokensOut="${2:-0}"

local tokensOutPerMillion tokensInPerMillion
tokensInPerMillion="${TOKEN_COST_INPUT}"
tokensOutPerMillion="${TOKEN_COST_OUTPUT}"

function convertRate {
    local val
    val="${1}"
    if [[ "${val}" != *"."* ]]; then val="${val}.0"; fi
    local whole dec
    whole="${val%.*}"
    dec="${val#*.}"
    # Pad decimal with 7 zeros to ensure correct scale
    dec="${dec}0000000"
    # Truncate to exactly 7 digits
    dec="${dec:0:7}"
    # Combine and strip leading zeros (force base-10)
    echo "$(( 10#${whole}${dec} ))"
}

local rateInInt rateOutInt
rateInInt=$(convertRate "$tokensInPerMillion")
rateOutInt=$(convertRate "$tokensOutPerMillion")

local costRaw costPadded tokenCents tokenDollars
(( costRaw = (tokensIn * rateInInt) + (tokensOut * rateOutInt) ))

costPadded="00000000000000${costRaw}"
tokenCents="${costPadded: -13}"
tokenDollars="${costPadded:0:${#costPadded}-13}"
(( tokenDollars = 10#${tokenDollars} ))

while [[ "${tokenCents}" == *"0" ]] && [[ "${#tokenCents}" -gt 2 ]]; do
    tokenCents="${tokenCents%0}"
done

echo "${tokenDollars}.${tokenCents}"
}

function sendPromptGoogle {
    # Create a temporary file to hold the stream of JSON part objects.
    partsFile="$(mktemp)"

    # Set the correct base64 command and arguments using an array.
    declare -a base64_cmd
    if [[ "$(uname)" == "Darwin" ]]; then
        base64_cmd=(base64 -i)
    else
        base64_cmd=(base64 -w 0)
    fi

    # Prep the file upload
    mimeType=$(file --brief --mime-type "${inputFile}")
    printOutput "4" "Processing file: ${inputFile} [MIME type: ${mimeType}]"

    # Base64 encode the file and pipe it directly into a jq command.
    "${base64_cmd[@]}" "${inputFile}" | jq -R -s --arg mime_type "${mimeType}" '{inlineData: {mimeType: $mime_type, data: .}}' > "${partsFile}"

    # Define the detailed prompt for the AI.
    promptText="$(<"${PROMPT_FILE}")"

    # Create the JSON object for the final text prompt and append it to the stream file.
    jq -n --arg text "${promptText}" '{text: $text}' >> "${partsFile}"

    # Create the final request.json body using the temporary file.
    echo "${partsFile}" | jq -n \
      --slurpfile parts_array "${partsFile}" \
      '{ contents: [ { parts: $parts_array } ] }' > "${workDir}/request.json"
    rm -f "${partsFile}"

    local requestJsonContent
    requestJsonContent="$(<"${workDir}/request.json")"

    # Execute the API call and capture the full response.
    printOutput "3" "Initiating LLM AI API call"

    local startTime endTime
    startTime="$(($(date +%s%N)/1000000))"

    local httpCode
    httpCode="$(curl -s \
      -w "%{http_code}" \
      -o "${workDir}/response.json" \
      -X POST \
      -H "Content-Type: application/json" \
      "https://generativelanguage.googleapis.com/v1beta/models/${LLM_MODEL}:streamGenerateContent?key=${LLM_API_KEY}" \
      -d "@${workDir}/request.json" 2>/dev/null)"

    local curlExitCode="${?}"
    endTime="$(($(date +%s%N)/1000000))"

    local durationMs
    durationMs="$((endTime - startTime))"
    local durationSeconds

    if [[ "${durationMs}" -lt 1000 ]]; then
        printf -v durationSeconds "0.%03d" "${durationMs}"
    else
        durationSeconds="${durationMs:0:${#durationMs}-3}.${durationMs: -3}"
    fi

    apiTimeDiff="$(timeDiff "${startTime}")"
    printOutput "3" "LLM AI API call complete -- Took ${apiTimeDiff} [HTTP: ${httpCode}]"
    # 1. Handle Curl Errors (Network/DNS)
    if [[ "${curlExitCode}" -ne "0" ]]; then
        printOutput "1" "Curl returned non-zero exit code [${curlExitCode}]"
        sqDb "INSERT INTO gemini_logs (model_name, request_timestamp, request_epoch, duration_seconds, request_json, finish_reason) VALUES ('${LLM_MODEL}', '$(date '+%Y-%m-%d %H:%M:%S')', $(date +%s), ${durationSeconds}, '${requestJsonContent//\'/\'\'}', 'CURL_ERROR_${curlExitCode}');"
        return 1
    fi

    apiResponse="$(<"${workDir}/response.json")"
    rm -f "${workDir}/response.json" "${workDir}/request.json"

    # 2. Handle API Errors
    local apiErrorCode
    apiErrorCode="$(jq -r ".[0].error.code // empty" <<<"${apiResponse}")"

    if [[ -n "${apiErrorCode}" ]]; then
        local apiErrorMessage
        apiErrorMessage="$(jq -r ".[0].error.message // .error.message" <<<"${apiResponse}")"
        printOutput "2" "Received API response ${apiErrorCode} [${apiErrorMessage}]"
        
        # Log the error to DB
        sqDb "INSERT INTO gemini_logs (model_name, request_timestamp, request_epoch, duration_seconds, request_json, response_json, finish_reason) VALUES ('${LLM_MODEL}', '$(date '+%Y-%m-%d %H:%M:%S')', $(date +%s), ${durationSeconds}, '${requestJsonContent//\'/\'\'}', '${apiResponse//\'/\'\'}', 'API_ERROR_${apiErrorCode}');"
        return 1
    fi

    # 3. Parse Token Data (Only if no error)
    promptTokenCount="$(jq ".[-1].usageMetadata.promptTokenCount // 0" <<<"${apiResponse}")" # Input tokens
    outputTokenCount="$(jq ".[-1].usageMetadata.candidatesTokenCount // 0" <<<"${apiResponse}")" # Output tokens part 1
    thoughtTokenCount="$(jq ".[-1].usageMetadata.thoughtsTokenCount // 0" <<<"${apiResponse}")" # Output tokens part 2
    totalTokenCount="$(jq ".[-1].usageMetadata.totalTokenCount // 0" <<<"${apiResponse}")"

    tokenSum="$(( promptTokenCount + outputTokenCount + thoughtTokenCount ))"
    
    # Get the cost
    tokensIn="${promptTokenCount}"
    tokensOut="$(( thoughtTokenCount + outputTokenCount ))"
    tokenCost="$(calculateTokenCost "${tokensIn}" "${tokensOut}")"
    printOutput "4" "API cost estimated to be [\$${tokenCost}] based on [${tokensIn}] prompt tokens and [${tokensOut}] output tokens"
    local modelVersion finishReason
    modelVersion="$(jq -r ".[-1].modelVersion // \"${LLM_MODEL}\"" <<<"${apiResponse}")"
    finishReason="$(jq -r ".[-1].candidates[0].finishReason // \"unknown\"" <<<"${apiResponse}")"

    local safeRequest safeResponse
    safeRequest="${requestJsonContent}"
    safeResponse="${apiResponse}"
    if [[ "${SAVE_DB_SPACE}" -eq "true" ]]; then
        safeRequest="$(jq '.contents[].parts[].inlineData.data = "[truncated]"' <<<"${safeRequest}")"
        safeResponse="$(jq '.[].thoughtSignature = "[truncated]"' <<<"${safeResponse}")"
    fi
    
    safeRequest="${safeRequest//\'/\'\'}"
    safeResponse="${safeResponse//\'/\'\'}"
    
    sqDb "INSERT INTO gemini_logs (
        model_name, 
        prompt_token_count, 
        thought_token_count, 
        output_token_count, 
        total_token_count, 
        cost, 
        request_timestamp, 
        request_epoch, 
        duration_seconds, 
        http_status_code, 
        finish_reason, 
        request_json, 
        response_json
    ) VALUES (
        '${modelVersion}', 
        ${promptTokenCount}, 
        ${thoughtTokenCount}, 
        ${outputTokenCount}, 
        ${totalTokenCount}, 
        '${tokenCost}',
        '$(date '+%Y-%m-%d %H:%M:%S')',
        $(date +%s),
        ${durationSeconds}, 
        ${httpCode}, 
        '${finishReason}', 
        '${safeRequest}', 
        '${safeResponse}'
    );"

    printOutput "5" "Model version [${modelVersion}]"
    if [[ "${tokenSum}" -eq "${totalTokenCount}" ]]; then
        printOutput "5" "Token Receipt [Prompt: ${promptTokenCount}][Thought: ${thoughtTokenCount}][Output: ${outputTokenCount}][Total: ${totalTokenCount}]"
    else
        printOutput "5" "Token Receipt Mismatch [Sum: ${tokenSum}][Total: ${totalTokenCount}]"
    fi

    # 4. Process the successful response text
    if ! echo "${apiResponse}" | jq -e 'type == "array"' > /dev/null; then
        printOutput "1" "The API did not return a valid stream (expected a JSON array)."
        return 1
    fi

    summary=$(jq -r '[.[] | .candidates[].content.parts[].text] | add' <<<"${apiResponse}")

    if [[ -z "${summary}" ]]; then
        printOutput "1" "Failed to generate a summary from the API response."
        return 1
    else
        printOutput "5" "Generated response [${#summary} characters]"
    fi
}

function sendPromptOpenAI {
    # Create a temporary file to hold the array of content parts.
    partsFile="$(mktemp)"

    # Set the correct base64 command and arguments using an array.
    declare -a base64_cmd
    if [[ "$(uname)" == "Darwin" ]]; then
        base64_cmd=(base64 -i)
    else
        base64_cmd=(base64 -w 0)
    fi

    # Prep the file upload
    mimeType=$(file --brief --mime-type "${inputFile}")
    printOutput "4" "Processing file: ${inputFile} [MIME type: ${mimeType}]"

    # Part 1: The File
    # Base64 encode the file and pipe it into jq to create the 'input_file' object.
    "${base64_cmd[@]}" "${inputFile}" | jq -R -s \
        --arg mime "${mimeType}" \
        --arg fname "$(basename "${inputFile}")" \
        '{
            type: "input_file",
            filename: $fname,
            file_data: ("data:" + $mime + ";base64," + .)
        }' > "${partsFile}"

    # Part 2: The Prompt Text
    # Read the prompt and append it as an 'input_text' object to the stream file.
    promptText="$(<"${PROMPT_FILE}")"
    jq -n --arg text "${promptText}" \
        '{
            type: "input_text",
            text: $text
        }' >> "${partsFile}"

    # Create the final request.json body using the temporary file.
    jq -n --slurpfile parts_array "${partsFile}" \
        --arg model "${LLM_MODEL}" \
        '{
            model: $model,
            input: [
                {
                    role: "user",
                    content: $parts_array
                }
            ]
        }' > "${workDir}/request.json"
    rm -f "${partsFile}"

    local requestJsonContent
    requestJsonContent="$(<"${workDir}/request.json")"

    # Execute the API call and capture the full response.
    printOutput "3" "Initiating OpenAI API call"

    local startTime endTime
    startTime="$(($(date +%s%N)/1000000))"

    local httpCode
    # Note: Keeping your specified /v1/responses endpoint
    httpCode="$(curl -s \
        -w "%{http_code}" \
        -o "${workDir}/response.json" \
        "https://api.openai.com/v1/responses" \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer ${LLM_API_KEY}" \
        -d "@${workDir}/request.json" 2>/dev/null)"

    local curlExitCode="${?}"
    endTime="$(($(date +%s%N)/1000000))"

    local durationMs
    durationMs="$((endTime - startTime))"
    local durationSeconds

    if [[ "${durationMs}" -lt 1000 ]]; then
        printf -v durationSeconds "0.%03d" "${durationMs}"
    else
        durationSeconds="${durationMs:0:${#durationMs}-3}.${durationMs: -3}"
    fi

    apiTimeDiff="$(timeDiff "${startTime}")"
    printOutput "3" "OpenAI API call complete -- Took ${apiTimeDiff} [HTTP: ${httpCode}]"

    # 1. Handle Curl Errors (Network/DNS)
    if [[ "${curlExitCode}" -ne "0" ]]; then
        printOutput "1" "Curl returned non-zero exit code [${curlExitCode}] -- See 'response.json' for more"
        sqDb "INSERT INTO openai_logs (model_name, request_timestamp, request_epoch, duration_seconds, request_json, finish_reason) VALUES ('${LLM_MODEL}', '$(date '+%Y-%m-%d %H:%M:%S')', $(date +%s), ${durationSeconds}, '${requestJsonContent//\'/\'\'}', 'CURL_ERROR_${curlExitCode}');"
        return 1
    fi

    apiResponse="$(<"${workDir}/response.json")"
    rm -f "${workDir}/response.json" "${workDir}/request.json"

    # 2. Handle API Errors (JSON)
    local apiErrorMessage
    apiErrorMessage="$(jq -r ".error.message // empty" <<<"${apiResponse}")"

    if [[ -n "${apiErrorMessage}" ]]; then
        local apiErrorCode
        apiErrorCode="$(jq -r ".error.code" <<<"${apiResponse}")"
        printOutput "2" "Received API response Error [${apiErrorCode}]: ${apiErrorMessage}"
        
        # Log the error to DB
        sqDb "INSERT INTO openai_logs (model_name, request_timestamp, request_epoch, duration_seconds, request_json, response_json, finish_reason) VALUES ('${LLM_MODEL}', '$(date '+%Y-%m-%d %H:%M:%S')', $(date +%s), ${durationSeconds}, '${requestJsonContent//\'/\'\'}', '${apiResponse//\'/\'\'}', 'API_ERROR_${apiErrorCode}');"
        return 1
    fi

    # 3. Parse Token Data (Only if no error)
    # OpenAI Standard Structure
    promptTokenCount="$(jq ".usage.prompt_tokens // 0" <<<"${apiResponse}")"
    outputTokenCount="$(jq ".usage.completion_tokens // 0" <<<"${apiResponse}")"
    # OpenAI o1/o3 series reasoning tokens
    thoughtTokenCount="$(jq ".usage.completion_tokens_details.reasoning_tokens // 0" <<<"${apiResponse}")" 
    totalTokenCount="$(jq ".usage.total_tokens // 0" <<<"${apiResponse}")"
    
    tokenSum="$(( promptTokenCount + outputTokenCount ))"
    # Note: In OpenAI, total_tokens usually equals prompt + completion. 
    # Reasoning tokens are usually *included* inside completion_tokens, not added on top.

    # Get the cost
    tokensIn="${promptTokenCount}"
    tokensOut="${outputTokenCount}"
    tokenCost="$(calculateTokenCost "${tokensIn}" "${tokensOut}")"
    printOutput "4" "API call cost [${tokenCost}] based on [${tokensIn}] tokens in and [${tokensOut}] tokens out"

    modelVersion="$(jq -r ".model // \"${LLM_MODEL}\"" <<<"${apiResponse}")"
    finishReason="$(jq -r ".choices[0].finish_reason // \"unknown\"" <<<"${apiResponse}")"

    local safeRequest safeResponse
    safeRequest="${requestJsonContent//\'/\'\'}"
    safeResponse="${apiResponse//\'/\'\'}"

    sqDb "INSERT INTO openai_logs (
        model_name, 
        prompt_token_count, 
        thought_token_count, 
        output_token_count, 
        total_token_count, 
        cost,
        request_timestamp, 
        request_epoch, 
        duration_seconds, 
        http_status_code, 
        finish_reason, 
        request_json, 
        response_json
    ) VALUES (
        '${modelVersion}', 
        ${promptTokenCount}, 
        ${thoughtTokenCount}, 
        ${outputTokenCount}, 
        ${totalTokenCount}, 
        '${tokenCost}',
        '$(date '+%Y-%m-%d %H:%M:%S')',
        $(date +%s),
        ${durationSeconds}, 
        ${httpCode}, 
        '${finishReason}', 
        '${safeRequest}', 
        '${safeResponse}'
    );"

    printOutput "5" "Model version [${modelVersion}]"
    if [[ "${tokenSum}" -eq "${totalTokenCount}" ]]; then
        printOutput "5" "Token Receipt [Prompt: ${promptTokenCount}][Output: ${outputTokenCount}][Total: ${totalTokenCount}]"
    else
        printOutput "5" "Token Receipt Mismatch [Sum: ${tokenSum}][Total: ${totalTokenCount}]"
    fi

    # 4. Process the successful response text
    summary=$(jq -r '.output_text // empty' <<<"${apiResponse}")

    # Fallback check
    if [[ -z "${summary}" ]]; then
        summary=$(jq -r '.choices[0].message.content // empty' <<<"${apiResponse}")
    fi

    if [[ -z "${summary}" ]]; then
        printOutput "1" "Failed to generate a summary from the API response."
        return 1
    else
        printOutput "5" "Generated response [${#summary} characters]"
    fi
}

function sendPromptOllama {
    # 1. Read the file content directly (no Base64)
    if [[ -f "${inputFile}" ]]; then
        fileContent="$(<"${inputFile}")"
        printOutput "4" "Read file: ${inputFile} [${#fileContent} characters]"
    else
        printOutput "1" "Input file not found: ${inputFile}"
        return 1
    fi

    # 2. Prepare the JSON payload using jq
    jsonBodyFile="${workDir}/request.json"
    promptText="$(<"${PROMPT_FILE}")"

    # We use jq to handle all the escaping safely.
    jq -n \
      --arg model "${LLM_MODEL}" \
      --arg prompt "${promptText}" \
      --arg file_content "${fileContent}" \
      --arg filename "$(basename "${inputFile}")" \
      '{
        model: $model,
        messages: [
          {
            role: "user",
            content: ($prompt + "\n\n" + "### " + $filename + "\n" + $file_content)
          }
        ],
        stream: false
      }' > "${jsonBodyFile}"

    local requestJsonContent
    requestJsonContent="$(<"${jsonBodyFile}")"

    # 3. Sanitize the Ollama URL and set it up for our endpoint
    # Note: Modifying the global variable OLLAMA_URL inside a function is risky
    # if it's reused. Using a local variable for the actual call.
    local targetUrl="${OLLAMA_URL#*://}"
    targetUrl="${targetUrl%%/*}"
    targetUrl="http://${targetUrl}/v1/chat/completions"

    printOutput "3" "Initiating API call to [${targetUrl}]"

    local startTime endTime
    startTime="$(($(date +%s%N)/1000000))"

    local httpCode
    # Capture HTTP code and Body
    httpCode="$(curl -s \
      -w "%{http_code}" \
      -o "${workDir}/response.json" \
      "${targetUrl}" \
      -H "Content-Type: application/json" \
      -H "Authorization: Bearer ${LLM_API_KEY}" \
      -d "@${jsonBodyFile}" 2>/dev/null)"

    local curlExitCode="${?}"
    endTime="$(($(date +%s%N)/1000000))"

    local durationMs
    durationMs="$((endTime - startTime))"
    local durationSeconds

    if [[ "${durationMs}" -lt 1000 ]]; then
        printf -v durationSeconds "0.%03d" "${durationMs}"
    else
        durationSeconds="${durationMs:0:${#durationMs}-3}.${durationMs: -3}"
    fi

    apiTimeDiff="$(timeDiff "${startTime}")"
    printOutput "3" "API call complete -- Took ${apiTimeDiff} [HTTP: ${httpCode}]"

    # 1. Handle Curl Errors
    if [[ "${curlExitCode}" -ne "0" ]]; then
        printOutput "1" "Curl failed with exit code [${curlExitCode}]"
        sqDb "INSERT INTO ollama_logs (model_name, request_timestamp, request_epoch, duration_seconds, request_json, finish_reason) VALUES ('${LLM_MODEL}', '$(date '+%Y-%m-%d %H:%M:%S')', $(date +%s), ${durationSeconds}, '${requestJsonContent//\'/\'\'}', 'CURL_ERROR_${curlExitCode}');"
        return 1
    fi

    apiResponse="$(<"${workDir}/response.json")"
    rm -f "${workDir}/response.json" "${jsonBodyFile}"

    # 2. Handle API Errors (JSON)
    # Check for standard OpenAI-style error OR Ollama 'error' field
    local apiErrorMessage
    apiErrorMessage="$(jq -r ".error.message // .error // empty" <<<"${apiResponse}")"

    if [[ -n "${apiErrorMessage}" ]]; then
        # Ollama sometimes puts the error string directly in .error, sometimes in .error.message
        printOutput "2" "Received API response Error: ${apiErrorMessage}"
        
        sqDb "INSERT INTO ollama_logs (model_name, request_timestamp, request_epoch, duration_seconds, request_json, response_json, finish_reason) VALUES ('${LLM_MODEL}', '$(date '+%Y-%m-%d %H:%M:%S')', $(date +%s), ${durationSeconds}, '${requestJsonContent//\'/\'\'}', '${apiResponse//\'/\'\'}', 'API_ERROR');"
        return 1
    fi

    # 3. Parse Token Data
    # Standard OpenAI format usually works for Ollama /v1/ endpoints
    promptTokenCount="$(jq ".usage.prompt_tokens // 0" <<<"${apiResponse}")"
    outputTokenCount="$(jq ".usage.completion_tokens // 0" <<<"${apiResponse}")"
    # Future proofing: DeepSeek-R1 via Ollama might use reasoning_tokens soon
    thoughtTokenCount="$(jq ".usage.completion_tokens_details.reasoning_tokens // 0" <<<"${apiResponse}")"
    totalTokenCount="$(jq ".usage.total_tokens // 0" <<<"${apiResponse}")"
    
    tokenSum="$(( promptTokenCount + outputTokenCount + thoughtTokenCount ))"

    # Get the cost (Likely 0 for local, but keeps data consistent)
    tokensIn="${promptTokenCount}"
    tokensOut="$(( outputTokenCount + thoughtTokenCount ))"
    tokenCost="$(calculateTokenCost "${tokensIn}" "${tokensOut}")"
    
    modelVersion="$(jq -r ".model // \"${LLM_MODEL}\"" <<<"${apiResponse}")"
    
    # Check OpenAI standard first, then fallback to Ollama native 'done_reason'
    finishReason="$(jq -r ".choices[0].finish_reason // .done_reason // .message.done_reason // \"unknown\"" <<<"${apiResponse}")"

    local safeRequest safeResponse
    safeRequest="${requestJsonContent//\'/\'\'}"
    safeResponse="${apiResponse//\'/\'\'}"

    sqDb "INSERT INTO ollama_logs (
        model_name, 
        prompt_token_count, 
        thought_token_count, 
        output_token_count, 
        total_token_count, 
        cost,
        request_timestamp, 
        request_epoch, 
        duration_seconds, 
        http_status_code, 
        finish_reason, 
        request_json, 
        response_json
    ) VALUES (
        '${modelVersion}', 
        ${promptTokenCount}, 
        ${thoughtTokenCount}, 
        ${outputTokenCount}, 
        ${totalTokenCount}, 
        '${tokenCost}',
        '$(date '+%Y-%m-%d %H:%M:%S')',
        $(date +%s),
        ${durationSeconds}, 
        ${httpCode}, 
        '${finishReason}', 
        '${safeRequest}', 
        '${safeResponse}'
    );"

    printOutput "5" "Responding model [${modelVersion}]"
    printOutput "5" "Token Receipt [Prompt ${promptTokenCount}][Output ${outputTokenCount}][Total Report ${totalTokenCount}]"

    # 4. Parse the Response
    summary=$(jq -r '.choices[0].message.content // empty' <<<"${apiResponse}")

    if [[ -z "${summary}" ]]; then
        printOutput "1" "Failed to parse content from API response."
        # Debug: print the first 100 chars of response
        printOutput "5" "Raw response start: ${apiResponse:0:100}..."
        return 1
    else
        printOutput "5" "Generated response [${#summary} characters]"
    fi
}

function sendPromptAnthropic {
    # 1. Upload the file first
    if [[ ! -f "${inputFile}" ]]; then
        printOutput "1" "Input file not found: ${inputFile}"
        return 1
    fi

    local mimeType
    mimeType=$(file --brief --mime-type "${inputFile}")
    printOutput "4" "Uploading file: ${inputFile} [MIME: ${mimeType}]"

    local startTime endTime
    startTime="$(($(date +%s%N)/1000000))"

    printOutput "5" "Executing curl call to upload file"
    
    local httpCode
    httpCode="$(curl -s \
        -w "%{http_code}" \
        -o "${workDir}/upload_response.json" \
        "https://api.anthropic.com/v1/files" \
        -H "x-api-key: ${LLM_API_KEY}" \
        -H "anthropic-version: 2023-06-01" \
        -H "anthropic-beta: files-api-2025-04-14" \
        -F "file=@${inputFile}" 2>/dev/null)"

    local curlExitCode="${?}"
    endTime="$(($(date +%s%N)/1000000))"
    
    local uploadTimeDiff
    uploadTimeDiff="$(timeDiff "${startTime}")"
    printOutput "3" "File upload complete -- Took ${uploadTimeDiff} [HTTP: ${httpCode}]"

    if [[ "${curlExitCode}" -ne "0" ]]; then
        printOutput "1" "Curl returned non-zero exit code [${curlExitCode}]"
        sqDb "INSERT INTO anthropic_logs (model_name, request_timestamp, request_epoch, duration_seconds, request_json, finish_reason) VALUES ('${LLM_MODEL}', '$(date '+%Y-%m-%d %H:%M:%S')', $(date +%s), ${durationSeconds}, '${requestJsonContent//\'/\'\'}', 'CURL_ERROR_${curlExitCode}');"
        return 1
    fi

    local uploadResponse
    uploadResponse="$(<"${workDir}/upload_response.json")"
    
    printOutput "5" "Upload response:"
    while read -r line; do
        printOutput "5" "${line}"
    done < "${workDir}/upload_response.json"

    # Extract the file_id
    local fileId
    fileId="$(jq -r ".id // empty" <<<"${uploadResponse}")"
    
    if [[ -z "${fileId}" ]]; then
        local errorMsg
        errorMsg="$(jq -r ".error.message // \"Unknown error\"" <<<"${uploadResponse}")"
        printOutput "1" "Failed to upload file: ${errorMsg}"
        rm -f "${workDir}/upload_response.json"
        return 1
    fi

    printOutput "3" "File uploaded successfully with ID: ${fileId}"
    rm -f "${workDir}/upload_response.json"

    # 2. Prepare the prompt text
    promptText="$(<"${PROMPT_FILE}")"

    # 3. Construct the JSON body with file reference
    jq -n \
        --arg model "${LLM_MODEL}" \
        --arg fileId "${fileId}" \
        --arg text "${promptText}" \
        '{
            model: $model,
            max_tokens: 1024,
            messages: [
                {
                    role: "user",
                    content: [
                        {
                            type: "text",
                            text: $text
                        },
                        {
                            type: "document",
                            source: {
                                type: "file",
                                file_id: $fileId
                            }
                        }
                    ]
                }
            ]
        }' > "${workDir}/request.json"

    local requestJsonContent
    requestJsonContent="$(<"${workDir}/request.json")"

    # 4. Execute the API Call
    printOutput "3" "Initiating Anthropic API call"
    
    startTime="$(($(date +%s%N)/1000000))"

    printOutput "5" "Executing curl call [curl -s -w \"%{http_code}\" -o \"${workDir}/response.json\" \"https://api.anthropic.com/v1/messages\" -H \"Content-Type: application/json\" -H \"x-api-key: ${LLM_API_KEY}\" -H \"anthropic-version: 2023-06-01\" -H \"anthropic-beta: files-api-2025-04-14\" -d \"@${workDir}/request.json\"]"
    
    httpCode="$(curl -s \
        -w "%{http_code}" \
        -o "${workDir}/response.json" \
        "https://api.anthropic.com/v1/messages" \
        -H "Content-Type: application/json" \
        -H "x-api-key: ${LLM_API_KEY}" \
        -H "anthropic-version: 2023-06-01" \
        -H "anthropic-beta: files-api-2025-04-14" \
        -d "@${workDir}/request.json" 2>/dev/null)"

    curlExitCode="${?}"
    endTime="$(($(date +%s%N)/1000000))"

    local durationMs
    durationMs="$((endTime - startTime))"
    local durationSeconds

    if [[ "${durationMs}" -lt 1000 ]]; then
        printf -v durationSeconds "0.%03d" "${durationMs}"
    else
        durationSeconds="${durationMs:0:${#durationMs}-3}.${durationMs: -3}"
    fi

    apiTimeDiff="$(timeDiff "${startTime}")"
    printOutput "3" "Anthropic API call complete -- Took ${apiTimeDiff} [HTTP: ${httpCode}]"
    

    # 5. Handle Curl Errors
    if [[ "${curlExitCode}" -ne "0" ]]; then
        printOutput "1" "Curl returned non-zero exit code [${curlExitCode}]"
        sqDb "INSERT INTO anthropic_logs (model_name, request_timestamp, request_epoch, duration_seconds, request_json, finish_reason) VALUES ('${LLM_MODEL}', '$(date '+%Y-%m-%d %H:%M:%S')', $(date +%s), ${durationSeconds}, '${requestJsonContent//\'/\'\'}', 'CURL_ERROR_${curlExitCode}');"
        return 1
    fi

    apiResponse="$(<"${workDir}/response.json")"
    rm -f "${workDir}/response.json" "${workDir}/request.json"

    # 6. Check for API Errors (JSON)
    local apiErrorType
    apiErrorType="$(jq -r ".error.type // empty" <<<"${apiResponse}")"
    if [[ -n "${apiErrorType}" ]]; then
        local apiErrorMsg
        apiErrorMsg="$(jq -r ".error.message" <<<"${apiResponse}")"
        printOutput "2" "Anthropic API Error [${apiErrorType}]: ${apiErrorMsg}"
        
        sqDb "INSERT INTO anthropic_logs (model_name, request_timestamp, request_epoch, duration_seconds, request_json, response_json, finish_reason) VALUES ('${LLM_MODEL}', '$(date '+%Y-%m-%d %H:%M:%S')', $(date +%s), ${durationSeconds}, '${requestJsonContent//\'/\'\'}', '${apiResponse//\'/\'\'}', 'API_ERROR_${apiErrorType}');"
        return 1
    fi

    # 7. Parse Token Data
    # Anthropic uses input_tokens and output_tokens
    promptTokenCount="$(jq ".usage.input_tokens // 0" <<<"${apiResponse}")"
    outputTokenCount="$(jq ".usage.output_tokens // 0" <<<"${apiResponse}")"
    thoughtTokenCount=0 # Placeholder: Anthropic doesn't expose reasoning tokens in standard usage yet
    totalTokenCount="$(( promptTokenCount + outputTokenCount ))" # Anthropic doesn't send a total field, we calculate it
    tokenSum="${totalTokenCount}"

    # Get the cost
    tokensIn="${promptTokenCount}"
    tokensOut="${outputTokenCount}"
    tokenCost="$(calculateTokenCost "${tokensIn}" "${tokensOut}")"
    printOutput "4" "API call cost [${tokenCost}] based on [${tokensIn}] tokens in and [${tokensOut}] tokens out"

    modelVersion="$(jq -r ".model // \"${LLM_MODEL}\"" <<<"${apiResponse}")"
    finishReason="$(jq -r ".stop_reason // \"unknown\"" <<<"${apiResponse}")"

    local safeRequest safeResponse
    safeRequest="${requestJsonContent//\'/\'\'}"
    safeResponse="${apiResponse//\'/\'\'}"

    sqDb "INSERT INTO anthropic_logs (
        model_name, 
        prompt_token_count, 
        thought_token_count, 
        output_token_count, 
        total_token_count, 
        cost,
        request_timestamp, 
        request_epoch, 
        duration_seconds, 
        http_status_code, 
        finish_reason, 
        request_json, 
        response_json
    ) VALUES (
        '${modelVersion}', 
        ${promptTokenCount}, 
        ${thoughtTokenCount}, 
        ${outputTokenCount}, 
        ${totalTokenCount}, 
        '${tokenCost}',
        '$(date '+%Y-%m-%d %H:%M:%S')',
        $(date +%s),
        ${durationSeconds}, 
        ${httpCode}, 
        '${finishReason}', 
        '${safeRequest}', 
        '${safeResponse}'
    );"

    printOutput "5" "Responding model [${modelVersion}]"
    printOutput "5" "Token Receipt [Prompt ${promptTokenCount}][Output ${outputTokenCount}][Total ${tokenSum}]"

    # 8. Parse the Response
    # Anthropic returns content in: .content[].text
    summary=$(jq -r '.content[] | select(.type=="text") | .text' <<<"${apiResponse}")

    if [[ -z "${summary}" ]]; then
        printOutput "1" "Failed to extract summary from API response."
        return 1
    else
        printOutput "5" "Generated response [${#summary} characters]"
    fi
}

function createDb {
sqlite3 "${sqliteDb}" <<EOF
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- ==========================================
-- 1. GEMINI
-- ==========================================
CREATE TABLE IF NOT EXISTS gemini_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name TEXT,
    prompt_token_count INTEGER,
    thought_token_count INTEGER,
    output_token_count INTEGER,
    total_token_count INTEGER,
    cost REAL,
    request_timestamp TEXT,
    request_epoch INTEGER,
    duration_seconds REAL,
    finish_reason TEXT,
    http_status_code INTEGER,
    request_json TEXT,
    response_json TEXT
);

-- ==========================================
-- 2. OPENAI
-- ==========================================
CREATE TABLE IF NOT EXISTS openai_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name TEXT,
    prompt_token_count INTEGER,
    thought_token_count INTEGER,
    output_token_count INTEGER,
    total_token_count INTEGER,
    cost REAL,
    request_timestamp TEXT,
    request_epoch INTEGER,
    duration_seconds REAL,
    finish_reason TEXT,
    http_status_code INTEGER,
    request_json TEXT,
    response_json TEXT
);

-- ==========================================
-- 3. OLLAMA
-- ==========================================
CREATE TABLE IF NOT EXISTS ollama_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name TEXT,
    prompt_token_count INTEGER,
    thought_token_count INTEGER,
    output_token_count INTEGER,
    total_token_count INTEGER,
    cost REAL,
    request_timestamp TEXT,
    request_epoch INTEGER,
    duration_seconds REAL,
    finish_reason TEXT,
    http_status_code INTEGER,
    request_json TEXT,
    response_json TEXT
);

-- ==========================================
-- 4. ANTHROPIC
-- ==========================================
CREATE TABLE IF NOT EXISTS anthropic_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name TEXT,
    prompt_token_count INTEGER,
    thought_token_count INTEGER,
    output_token_count INTEGER,
    total_token_count INTEGER,
    cost REAL,
    request_timestamp TEXT,
    request_epoch INTEGER,
    duration_seconds REAL,
    finish_reason TEXT,
    http_status_code INTEGER,
    request_json TEXT,
    response_json TEXT
);

-- ==========================================
-- 5. Discord
-- ==========================================
CREATE TABLE IF NOT EXISTS discord_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT,
    channel_id TEXT,
    author_username TEXT,
    content TEXT,
    discord_timestamp TEXT,
    request_timestamp TEXT,
    request_epoch INTEGER,
    duration_seconds REAL,
    http_status_code INTEGER,
    request_json TEXT,
    response_json TEXT
);

-- ==========================================
-- 6. DB log
-- ==========================================
CREATE TABLE IF NOT EXISTS sqLog (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    TIME TEXT,
    COMMAND TEXT,
    OUTPUT TEXT
);
EOF
}

### Configuration
# Base directory where DnD sessions are stored.
baseDir="/app/Sessions"
sqliteDb="/app/api.db"

if ! [[ -e "${sqliteDb}" ]]; then
    createDb
    if [[ -e "${sqliteDb}" ]]; then
        printOutput "3" "Successfully initialized database"
    else
        printOutput "1" "Failed to initialize database"
        exit 1
    fi
else
    printOutput "5" "Verified database presence"
fi

if ! [[ -d "${baseDir}" ]]; then
    if mkdir -p "${baseDir}"; then
        printOutput "5" "Created base dir [${baseDir}]"
    else
        printOutput "1" "Unable to create base dir [${baseDir}]"
        exit 1
    fi
else
    # Directory exists, but we need to verify permissions
    if [[ ! -r "${baseDir}" || ! -w "${baseDir}" ]]; then
        printOutput "1" "Unable to read/write to [${baseDir}]"
        exit 1
    fi
    printOutput "5" "Validated base dir [${baseDir}]"
fi

# Whisper Options
# Validate WHISPER_MODEL
if [[ -z "${WHISPER_MODEL}" ]]; then
    printOutput "2" "No model defined, defaulting to [large-v3]"
    WHISPER_MODEL="large-v3"
else
    printOutput "5" "Validated whisper model [${WHISPER_MODEL}]"
fi

# Validate WHISPER_THREADS
if [[ -n "${WHISPER_THREADS}" ]] && ! [[ "${WHISPER_THREADS}" =~ ^[0-9]+$ ]]; then
    printOutput "2" "Invalid thread count [${WHISPER_THREADS}] -- Setting to [$(nproc)]"
    WHISPER_THREADS="$(nproc)"
else
    if [[ "${WHISPER_THREADS}" -gt "$(nproc)" ]]; then
        printOutput "2" "Invalid thread count [${WHISPER_THREADS}] is greater than nproc [$(nproc)] -- Setting to [$(nproc)]"
        WHISPER_THREADS="$(nproc)"
    else
        printOutput "5" "Validated whisper thread count [${WHISPER_THREADS}]"
    fi
fi

# Validate WHISPER_LANGUAGE
if [[ -n "${WHISPER_LANGUAGE}" ]] && ! [[ "${WHISPER_LANGUAGE}" =~ ^[a-z]{2}$ ]]; then
    printOutput "2" "Invalid language code [${WHISPER_LANGUAGE}] -- Setting to [en]"
    WHISPER_LANGUAGE="en"
else
    printOutput "5" "Validated whisper language [${WHISPER_LANGUAGE}]"
fi

# Validate WHISPER_COMPUTE_TYPE
if [[ -n "${WHISPER_COMPUTE_TYPE}" ]]; then
    case "${WHISPER_COMPUTE_TYPE}" in
        float16|float32|int8)
            # This is a valid type
            printOutput "5" "Validated whisper compute type [${WHISPER_COMPUTE_TYPE}]"
            ;;
        *)
            # This is not
            printOutput "2" "Invalid whisper compute type [${WHISPER_COMPUTE_TYPE}] -- Setting to [int8]"
            WHISPER_COMPUTE_TYPE="int8"
            ;;
    esac
fi

# Validate WHISPER_CHUNK_SIZE (positive integer)
if [[ -n "${WHISPER_CHUNK_SIZE}" ]] && ! [[ "${WHISPER_CHUNK_SIZE}" =~ ^[1-9][0-9]*$ ]]; then
    printOutput "2" "Invalid chunk size [${WHISPER_CHUNK_SIZE}] -- Setting to [30]"
    WHISPER_CHUNK_SIZE="30"
else
    printOutput "5" "Validated whisper chunk size [${WHISPER_CHUNK_SIZE}]"
fi

# Validate WHISPER_BEAM_SIZE (positive integer)
if [[ -n "${WHISPER_BEAM_SIZE}" ]] && ! [[ "${WHISPER_BEAM_SIZE}" =~ ^[1-9][0-9]*$ ]]; then
    printOutput "2" "Invalid beam size [${WHISPER_BEAM_SIZE}] -- Setting to [5]"
    WHISPER_BEAM_SIZE="5"
else
    printOutput "5" "Validated whisper beam size [${WHISPER_BEAM_SIZE}]"
fi

# Validate WHISPER_BATCH_SIZE (positive integer)
if [[ -n "${WHISPER_BATCH_SIZE}" ]] && ! [[ "${WHISPER_BATCH_SIZE}" =~ ^[1-9][0-9]*$ ]]; then
    printOutput "2" "Invalid batch size [${WHISPER_BATCH_SIZE}] -- Setting to [8]"
    WHISPER_BATCH_SIZE="8"
else
    printOutput "5" "Validated whisper batch size [${WHISPER_BATCH_SIZE}]"
fi

# Validate WHISPER_VAD_ONSET (float)
if [[ -n "${WHISPER_VAD_ONSET}" ]] && ! [[ "${WHISPER_VAD_ONSET}" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
    printOutput "2" "Invalid VAD onset value [${WHISPER_VAD_ONSET}] -- Setting to [0.5]"
    WHISPER_VAD_ONSET="0.5"
else
    printOutput "5" "Validated whisper VAD onset value size [${WHISPER_VAD_ONSET}]"
fi

# Validate WHISPER_VAD_OFFSET (float)
if [[ -n "${WHISPER_VAD_OFFSET}" ]] && ! [[ "${WHISPER_VAD_OFFSET}" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
    printOutput "2" "Invalid VAD offset value [${WHISPER_VAD_OFFSET}] -- Setting to [0.363]"
    WHISPER_VAD_OFFSET="0.363"
else
    printOutput "5" "Validated whisper VAD offset value size [${WHISPER_VAD_OFFSET}]"
fi

# Validate WHISPER_VAD_METHOD
if [[ "${WHISPER_VAD_METHOD}" != "pyannote" && "${WHISPER_VAD_METHOD}" != "silero" ]]; then
    printOutput "2" "Invalid VAD method [${WHISPER_VAD_METHOD}] -- Setting to [pyannote]"
    WHISPER_VAD_METHOD="pyannote"
else
    printOutput "5" "Validated whisper VAD method [${WHISPER_VAD_METHOD}]"
fi

PROMPT_FILE="/app/prompt.txt"

# 1. Validate Provider
if [[ -z "${LLM_PROVIDER}" ]]; then
    printOutput "2" "No LLM provider defined"
    exit 1
fi

# Validate API key and model
declare -A validModels
case "${LLM_PROVIDER}" in
    google)
        printOutput "5" "Validated LLM provider [${LLM_PROVIDER}]"
        
        # 1. Validate API Key Variable
        if [[ -z "${LLM_API_KEY}" ]]; then
            printOutput "1" "No API key set for provider [${LLM_PROVIDER}]. Please set [LLM_API_KEY]."
            exit 1
        else
            LLM_API_KEY_CENSORED="$(censorData "${LLM_API_KEY}")"
            printOutput "5" "Validated LLM API Key [${LLM_API_KEY_CENSORED}]"
        fi

        # 2. Validate Model Variable
        if [[ -z "${LLM_MODEL}" ]]; then
            printOutput "1" "No LLM model set. Please set [LLM_MODEL]."
            exit 1
        fi

        # 3.Validate the key
        while :; do
            # Construct URL with parameter expansion for pageToken
            url="https://generativelanguage.googleapis.com/v1beta/models?key=${LLM_API_KEY}${pageToken:+&pageToken=${pageToken}}"
            
            # Fetch response
            response=$(curl -s "${url}")

            # Check for API errors immediately (Validates the API Key)
            if jq -e '.error' <<<"${response}" > /dev/null; then
                local errorMsg
                errorMsg=$(jq -r '.error.message' <<<"${response}")
                printOutput "1" "Google API Error [${errorMsg}]"
                exit 1
            else
                printOutput "5" "Validated LLM API Key authorization"
            fi

            # Parse JSON and populate the array
            while IFS='|' read -r codeName fullName; do
                if [[ -n "${codeName}" ]]; then
                    printOutput "5" "Found model [${codeName}] (${fullName})"
                    validModels["${codeName}"]="${fullName}"
                fi
            done < <(jq -r '
                .models[]? 
                | select(.supportedGenerationMethods | index("generateContent")) 
                | "\(.name | sub("^models/";""))|\(.displayName)"
            ' <<<"${response}")

            # Handle Next Page Token
            pageToken=$(jq -r '.nextPageToken // empty' <<<"${response}")

            if [[ -z "${pageToken}" ]]; then
                break
            fi
        done

        # Check if the requested model exists in the validModels array
        if [[ -z "${validModels[${LLM_MODEL}]}" ]]; then
            printOutput "1" "Invalid model [${LLM_MODEL}] for provider [google]."
            printOutput "1" "Found [${#validModels[@]}] valid models supporting content generation"
            for key in "${!validModels[@]}"; do
                echo " - ${key} (${validModels[${key}]})"
            done
            printOutput "1" "For more info, see: https://ai.google.dev/gemini-api/docs/models"
            exit 1
        else
            printOutput "5" "Successfully validated model [${LLM_MODEL}] against Google API."
        fi
        ;;

    ollama)
        printOutput "5" "Validated LLM provider [${LLM_PROVIDER}]"
        
        # 0. Validate URL
        if [[ -z "${OLLAMA_URL}" ]]; then
             printOutput "1" "Provider is set to [ollama] but [OLLAMA_URL] is not set."
             exit 1
        else
            printOutput "5" "Validated Ollama URL [${OLLAMA_URL}]"
        fi
        
        # 1. Validate API Key Variable
        if [[ -z "${LLM_API_KEY}" ]]; then
            printOutput "1" "No API key set for provider [${LLM_PROVIDER}]. Please set [LLM_API_KEY]."
            exit 1
        else
            LLM_API_KEY_CENSORED="$(censorData "${LLM_API_KEY}")"
            printOutput "5" "Validated LLM API Key [${LLM_API_KEY_CENSORED}]"
        fi

        # 2. Validate Model Variable
        if [[ -z "${LLM_MODEL}" ]]; then
            printOutput "1" "No LLM model set. Please set [LLM_MODEL]."
            exit 1
        fi

        local response
        response=$(curl -s "${OLLAMA_URL}/api/tags")

        # Check for connection errors (curl returns exit code 0 usually, so we check empty response or jq error)
        if [[ -z "${response}" ]]; then
            printOutput "1" "Could not connect to Ollama at [${OLLAMA_URL}]."
            exit 1
        fi

        # Create array of valid Ollama models
        while read -r modelName; do
            if [[ -n "${modelName}" ]]; then
                # Strip ':latest' if you prefer loose matching, or keep strict
                validModels["${modelName}"]=1
            fi
        done < <(jq -r '.models[].name' <<<"${response}")

        if [[ -z "${validModels[${LLM_MODEL}]}" ]]; then
            printOutput "1" "Invalid model [${LLM_MODEL}] for provider [ollama]."
            printOutput "3" "Available Ollama models:"
            for key in "${!validModels[@]}"; do
                echo " - ${key}"
            done
            exit 1
        else
            printOutput "5" "Successfully validated model [${LLM_MODEL}] against Ollama."
        fi
        ;;

    openai)
        printOutput "5" "Validated LLM provider [${LLM_PROVIDER}]"

        # 1. Validate API Key
        if [[ -z "${LLM_API_KEY}" ]]; then
            printOutput "1" "No API key set for provider [${LLM_PROVIDER}]. Please set [LLM_API_KEY]."
            exit 1
        else
            LLM_API_KEY_CENSORED="$(censorData "${LLM_API_KEY}")"
            printOutput "5" "Validated LLM API Key [${LLM_API_KEY}]"
        fi

        # 2. Validate Model Variable
        if [[ -z "${LLM_MODEL}" ]]; then
            printOutput "1" "No LLM model set. Please set [LLM_MODEL]."
            exit 1
        fi

        # 3. Fetch available models (deep validation)
        printOutput "4" "Fetching available OpenAI models for validation"

        response=$(curl -s \
            -H "Authorization: Bearer ${LLM_API_KEY}" \
            -H "Content-Type: application/json" \
            https://api.openai.com/v1/models)

        # Validate response
        if [[ -z "${response}" ]]; then
            printOutput "1" "No response from OpenAI API."
            exit 1
        fi

        # Check for API error
        if jq -e '.error' <<<"${response}" > /dev/null; then
            errorMsg=$(jq -r '.error.message' <<<"${response}")
            printOutput "1" "OpenAI API Error [${errorMsg}]"
            exit 1
        else
            printOutput "5" "Validated LLM API Key authorization"
        fi

        # Build list of valid models
        while read -r modelId; do
            if [[ -n "${modelId}" ]]; then
                validModels["${modelId}"]="1"
            fi
        done < <(jq -r '.data[].id' <<<"${response}")

        # Validate requested model
        if [[ -z "${validModels[${LLM_MODEL}]}" ]]; then
            printOutput "1" "Invalid model [${LLM_MODEL}] for provider [openai]."
            printOutput "3" "Available OpenAI models:"
            for key in "${!validModels[@]}"; do
                echo " - ${key}"
            done
            printOutput "1" "For more info, see: https://platform.openai.com/docs/models"
            exit 1
        else
            printOutput "5" "Successfully validated model [${LLM_MODEL}] against OpenAI API."
        fi
        ;;

    anthropic)
        printOutput "5" "Validated LLM provider [${LLM_PROVIDER}]"
        
        # 1. Validate API Key Variable
        if [[ -z "${LLM_API_KEY}" ]]; then
            printOutput "1" "No API key set for provider [${LLM_PROVIDER}]. Please set [LLM_API_KEY]."
            exit 1
        else
            LLM_API_KEY_CENSORED="$(censorData "${LLM_API_KEY}")"
            printOutput "5" "Validated LLM API Key [${LLM_API_KEY}]"
        fi
        
        # 2. Validate Model Variable
        if [[ -z "${LLM_MODEL}" ]]; then
            printOutput "1" "No LLM model set. Please set [LLM_MODEL]."
            exit 1
        fi

        # 3. Fetch available models (deep validation)
        printOutput "4" "Fetching available Anthropic models for validation"

        response=$(curl -s \
            -H "x-api-key: ${LLM_API_KEY}" \
            -H "anthropic-version: 2023-06-01" \
            -H "Content-Type: application/json" \
            https://api.anthropic.com/v1/models)

        # Validate response
        if [[ -z "${response}" ]]; then
            printOutput "1" "No response from Anthropic API."
            exit 1
        fi

        # Check for API error
        if jq -e '.error' <<<"${response}" > /dev/null; then
            errorMsg=$(jq -r '.error.message' <<<"${response}")
            printOutput "1" "Anthropic API Error [${errorMsg}]"
            exit 1
        else
            printOutput "5" "Validated LLM API Key authorization"
        fi

        # Build list of valid models
        while read -r modelId; do
            if [[ -n "${modelId}" ]]; then
                validModels["${modelId}"]="1"
            fi
        done < <(jq -r '.data[].id' <<<"${response}")

        # Validate requested model
        if [[ -z "${validModels[${LLM_MODEL}]}" ]]; then
            printOutput "1" "Invalid model [${LLM_MODEL}] for provider [anthropic]."
            printOutput "3" "Available Anthropic models:"
            for key in "${!validModels[@]}"; do
                echo " - ${key}"
            done
            printOutput "1" "For more info, see: https://docs.anthropic.com/en/docs/models-overview"
            exit 1
        else
            printOutput "5" "Successfully validated model [${LLM_MODEL}] against Anthropic API."
        fi
        ;;

    *)
        printOutput "1" "LLM provider [${LLM_PROVIDER}] failed validation"
        exit 1
        ;;
esac

# 3. Audio Retention Policy
if [[ -z "${KEEP_AUDIO}" ]]; then
    # Default to true (safe) if not specified
    KEEP_AUDIO="true"
fi
if [[ "${KEEP_AUDIO,,}" != "true" && "${KEEP_AUDIO,,}" != "false" ]]; then
    printOutput "2" "Invalid value for KEEP_AUDIO [${KEEP_AUDIO}] -- Setting to [true]"
    KEEP_AUDIO="true"
else
    KEEP_AUDIO="${KEEP_AUDIO,,}"
    printOutput "5" "Validated KEEP_AUDIO value [${KEEP_AUDIO}]"
fi

# --- Discord Configuration ---
if [[ -z "${DISCORD_WEBHOOK}" ]]; then
    printOutput "1" "No valid Discord webhook URL set"
    exit 1
else
    if [[ "${DISCORD_WEBHOOK}" =~ ^https://discord\.com/api/webhooks/[0-9]+/[a-zA-Z0-9_-]+$ ]]; then
        printOutput "5" "Validated Discord webhook format"
        # Strip the token
        DISCORD_WEBHOOK_ID="${DISCORD_WEBHOOK%/*}"
        # Strip every leading
        DISCORD_WEBHOOK_ID="${DISCORD_WEBHOOK_ID##*/}"
        # Censor the ID
        DISCORD_WEBHOOK_ID="$(censorData "${DISCORD_WEBHOOK_ID}")"
        # Now do the token
        DISCORD_WEBHOOK_TOKEN="${DISCORD_WEBHOOK##*/}"
        DISCORD_WEBHOOK_TOKEN="$(censorData "${DISCORD_WEBHOOK_TOKEN}")"
        DISCORD_WEBHOOK_CENSORED="https://discord.com/api/webhooks/${DISCORD_WEBHOOK_ID}/${DISCORD_WEBHOOK_TOKEN}"
        unset DISCORD_WEBHOOK_ID DISCORD_WEBHOOK_TOKEN
        printOutput "5" "Validated Discord webhook [${DISCORD_WEBHOOK}]"
    else
        printOutput "1" "Discord webhook format check"
        exit 1
    fi
fi

# --- System Configuration ---
# Set default verbosity level (adjust as needed)
if ! [[ "${OUTPUT_VERBOSITY}" =~ ^[1-5]$ ]]; then
    printOutput "1" "Invalid output verbosity [${OUTPUT_VERBOSITY}] -- Setting to [3] (info)"
    OUTPUT_VERBOSITY="3"
else
    printOutput "5" "Validated output verbosity [${OUTPUT_VERBOSITY}]"
fi

# Prompt File Check
if ! [[ -e "${PROMPT_FILE}" ]]; then
    if ! [[ -e "/app/sample_prompt.txt" ]]; then
        cp "/opt/prompt.txt" "/app/sample_prompt.txt"
    fi
    printOutput "1" "No [${PROMPT_FILE}] found -- Please edit [/app/sample_prompt.txt], rename to [prompt.txt], and re-run"
    exit 0
else
    printOutput "5" "Validated prompt file"
fi

# Set the default spool time
if ! [[ "${RESPAWN_TIME}" =~ ^[0-9]+$ ]]; then
    printOutput "1" "Invalid spool time [${RESPAWN_TIME}] -- Setting to [3600] seconds (one hour)"
    RESPAWN_TIME="3600"
else
    printOutput "5" "Validated respawn time [${RESPAWN_TIME}]"
fi

# Set the default save DB size option
if [[ "${SAVE_DB_SPACE,,}" == "true" ]]; then
    printOutput "5" "Validated save DB space as [true]"
    SAVE_DB_SPACE="${SAVE_DB_SPACE,,}"
elif [[ "${SAVE_DB_SPACE}" == "false" ]]; then
    printOutput "5" "Validated save DB space as [false]"
    SAVE_DB_SPACE="${SAVE_DB_SPACE,,}"
else
    printOutput "1" "Invliad save DB space option [${SAVE_DB_SPACE}] -- Setting to [true]"
    SAVE_DB_SPACE="true"
fi

# Custom Script Check
if [[ -n "${CUSTOM_SCRIPT}" ]]; then
    scriptPath="/app/${CUSTOM_SCRIPT}"
    if [[ -f "${scriptPath}" ]]; then
        # Check if executable by the current user
        if [[ ! -x "${scriptPath}" ]]; then
            printOutput "2" "Custom script [${CUSTOM_SCRIPT}] found but not executable. Attempting to fix..."
            chmod u+x "${scriptPath}"
        fi
        # Re-verify after attempted fix
        if [[ -x "${scriptPath}" ]]; then
            printOutput "5" "Verified custom script [${CUSTOM_SCRIPT}] is executable"
        else
            printOutput "1" "Custom script [${CUSTOM_SCRIPT}] is not executable and permissions could not be fixed. It will be ignored."
            # Unset variable to prevent execution attempts later
            unset CUSTOM_SCRIPT
        fi
    else
        printOutput "1" "Custom script [${CUSTOM_SCRIPT}] defined but not found at [${scriptPath}]"
        # Unset variable so we don't try to run a phantom file
        unset CUSTOM_SCRIPT
    fi
fi

# Trap SIGINT (Ctrl+C) and SIGTERM (docker stop)
trap graceful_shutdown SIGINT SIGTERM

# Main Execution
while [[ -z "${shutdown_requested}" ]]; do
    totalStartTime="$(($(date +%s%N)/1000000))"
    printOutput "3" "Starting Scribble"

    # Find zipped session files non-recursively.
    readarray -t zipFiles < <(find "${baseDir}" -maxdepth 1 -type f -name "craig-*.flac.zip")

    if (( ${#zipFiles[@]} == 0 )); then
        printOutput "3" "No 'craig-*.flac.zip' files found to process."
    else
        printOutput "4" "Located [${#zipFiles[*]}] files to be processed"
        for zipFile in "${zipFiles[@]}"; do
            printOutput "5" "${zipFile}"
        done
    fi

    for zipFile in "${zipFiles[@]}"; do
        printOutput "3" "Processing [${zipFile}]"
        startTime="$(($(date +%s%N)/1000000))"
        
        # Make sure the file is done being written by getting its size twice, three seconds apart
        printOutput "3" "Verifying file write completion"
        read -ra size_1 < <(du -sb "${zipFile}")
        sleep 3
        read -ra size_2 < <(du -sb "${zipFile}")
        printOutput "5" "Size 1 [${size_1[0]}] | Size 2 [${size_2[0]}]"
        if [[ "${size_1[0]}" -ne "${size_2[0]}" ]]; then
            printOutput "2" "File appears to have changed sized, possibly still being copied/written -- Waiting for size to stabilize"
            while [[ "${size_1[0]}" -ne "${size_2[0]}" ]]; do
                printOutput "5" "Size 1 Recheck [${size_1[0]}] | Size 2 Recheck [${size_2[0]}]"
                sleep 3
                read -ra size_1 < <(du -sb "${zipFile}")
                sleep 3
                read -ra size_2 < <(du -sb "${zipFile}")
            done
        fi
        printOutput "4" "Verified file as written"

        # The unzipped folder name is assumed to match the zip filename without the extension.
        unzippedDir="${zipFile%.zip}"
        infoFile="${unzippedDir}/info.txt"

        # Unzip the archive.
        while read -r line; do
            printOutput "4" "${line}"
        done < <(unzip -o "${zipFile}" -d "${unzippedDir}")
        printOutput "4" "Unzipped in $(timeDiff "${startTime}")"

        if [[ ! -f "${infoFile}" ]]; then
            printOutput "2" "'info.txt' not found in the unzipped folder '${unzippedDir}' -- Skipping"
            continue
        else
            printOutput "5" "Found 'info.txt' file"
        fi

        # Find the start time line and extract the YYYY-MM-DD date.
        unset sessionDate
        while read -r line; do
            printOutput "5" "Processing line [${line}]"
            if [[ "${line}" =~ ^"Start time:".* ]]; then
                sessionDate="${line#*:}"
                sessionDate="${sessionDate:1}"
                printOutput "5" "Isolated session date [${sessionDate}]"
                sessionDate="$(date -d "${sessionDate}" "+%F")"
                printOutput "5" "Converted start date to local time [${sessionDate}]"
                break
            fi
        done < "${infoFile}"
        
        if [[ -z "${sessionDate}" ]]; then
            printOutput "2" "Could not find 'Start time:' in ${infoFile} -- Skipping"
            if rm -rf "${unzippedDir}"; then
                printOutput "5" "Clean up successful"
            else
                printOutput "1" "Failed to remove [${unzippedDir}]"
            fi
            continue
        else
            printOutput "5" "Found session date [${sessionDate}]"
        fi
        workDir="${baseDir}/${sessionDate}"
        
        # If session folder already exists, remove the newly unzipped folder.
        # The existing folder will be processed.
        if [[ -d "${workDir}" ]]; then
            printOutput "3" "Work directory '${workDir}' already exists. Assuming it's a work-in-progress."
            printOutput "5" "Removing temporary unzipped folder [${unzippedDir}]"
            if rm -rf "${unzippedDir}"; then
                printOutput "5" "Clean up successful"
            else
                printOutput "1" "Failed to remove [${unzippedDir}]"
            fi
        else
            # Otherwise, rename the unzipped folder to be the new work directory.
            printOutput "3" "Creating new work directory by renaming unzipped folder to '${workDir}'"
            if mv "${unzippedDir}" "${workDir}"; then
                printOutput "5" "Move successful"
            else
                printOutput "1" "Failed to move [${unzippedDir}] to [${workDir}] -- Skipping"
                continue
            fi
        fi
        # Define our session source
        basename "${zipFile}" > "${workDir}/.source"
    done
    
    readarray -t workDirs < <(find /app/Sessions/ -maxdepth 1 -type d -regextype egrep -regex ".*/[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    if [[ "${#workDirs[@]}" -ne "0" ]]; then
        printOutput "5" "Found [${#workDirs[@]}] sessions to iterate through"
        for workDir in "${workDirs[@]}"; do 
            printOutput "5" "${workDir}"
        done
    fi
    for workDir in "${workDirs[@]}"; do
        # Skip completed directories
        if [[ -e "${workDir}/.complete" ]]; then
            printOutput "5" "Skipping completed session [${workDir}]"
            continue
        fi
        printOutput "5" "Iterating through [${workDir}]"
        
        # Define our session log
        sessionLog="${workDir}/session.log"
        printOutput "5" "Session log [${sessionLog}]"
        # Define our session date
        sessionDate="${workDir##*/}"
        printOutput "5" "Isolated session date [${sessionDate}]"
        
        # Remove raw.dat if it exists.
        if [[ -e "${workDir}/raw.dat" ]]; then
            if rm -f "${workDir}/raw.dat"; then
                printOutput "5" "Removed 'raw.dat' file"
            else
                printOutput "2" "Failed to remove 'raw.dat' file"
            fi
        else
            printOutput "5" "'raw.dat' file not found"
        fi
        
        # Process the session directory (either the pre-existing one or the new one).
        if mkdir -p "${workDir}/progress" "${workDir}/transcripts"; then
            printOutput "3" "--- Starting transcription for session [${workDir##*/}] ---"
            printOutput "5" "Created work directories"
        else
            printOutput "1" "Failed to create work directories"
            exit 1
        fi

        # Find all flac files and loop through them.
        readarray -t flacFiles < <(find "${workDir}" -type f -name "*.flac")
        if [[ "${#flacFiles[@]}" -eq "0" ]]; then
            printOutput "1" "Failed to locate any flac files for session [${workDir##*/}] -- Skipping"
            continue
        else
            printOutput "5" "Located [${#flacFiles[@]}] flac files for session [${workDir}]"
            for file in "${flacFiles[@]}"; do
                printOutput "5" "${file}"
            done
        fi
        for file in "${flacFiles[@]}"; do
            startTime="$(($(date +%s%N)/1000000))"
            printOutput "5" "Processing file [${file}]"
            filename="$(basename "${file}")"
            # Extract username using bash parameter expansion, as you prefer.
            username="${filename%.flac}"
            username="${username#*-}"
            printOutput "5" "Isolated username [${username}]"

            transcript_json="${workDir}/transcripts/${username}_transcript.json"
            transcript_file="${workDir}/transcripts/${username}_transcript.txt"
            progress_file="${workDir}/progress/${username}.txt"

            # If a transcript for this user already exists, skip transcription.
            if [[ -f "${transcript_file}" ]]; then
                printOutput "5" "Transcript for ${username} already exists -- Skipping"
                continue
            fi

            printOutput "3" "Transcribing file [${file}] for user [${username}]"
            unset outputArr transcriptArr
            
            # Execute whisper command and read its output line by line.
            # This captures both stderr and stdout into our loop.
            printOutput "5" "Executing whisperx [whisperx --model \"${WHISPER_MODEL}\" --language \"${WHISPER_LANGUAGE}\" --compute_type \"${WHISPER_COMPUTE_TYPE}\" --vad_onset \"${WHISPER_VAD_ONSET}\" --vad_offset \"${WHISPER_VAD_OFFSET}\" --vad_method \"${WHISPER_VAD_METHOD}\" --threads \"${WHISPER_THREADS}\" --chunk_size \"${WHISPER_CHUNK_SIZE}\" --beam_size \"${WHISPER_BEAM_SIZE}\" --batch_size \"${WHISPER_BATCH_SIZE}\" --output_dir \"${workDir}\" --output_format json --device cpu --no_align \"${file}\" 2>&1]"
            startTimeWhisper="$(($(date +%s%N)/1000000))"
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
                        --output_dir "${workDir}" \
                        --output_format json \
                        --device cpu \
                        --no_align \
                        "${file}" 2>&1)
            printOutput "3" "Transcription complete [Took $(timeDiff "${startTimeWhisper}]")]"
            
            # Generate a .txt transcript from the json file
            unset transcriptOutput
            while IFS=$'\t' read -r startSecs text; do
                # Skip empty lines
                if [[ -z "${text}" ]]; then
                    continue
                else
                    # Remove any leading spaces
                    text="${text# }"
                fi

                # Round the seconds to the nearest whole number
                totalSecs=$(printf "%.0f" "${startSecs}")

                # Calculate hours, minutes, and seconds
                ss=$((totalSecs % 60))
                mm=$((totalSecs / 60 % 60))
                hh=$((totalSecs / 3600))

                # Print the formatted line
                transcriptOutput+=("$(printf "[%02d:%02d:%02d] %s\n" "${hh}" "${mm}" "${ss}" "${text}")")
            done < <(jq -r '.segments[] | [.start, .text] | @tsv' "${file%.flac}.json")
            printf '%s\n' "${transcriptOutput[@]}" > "${transcript_file}"
            
            # Move the json file
            mv "${file%.flac}.json" "${transcript_json}"

            printOutput "3" "Transcription for ${username} complete -- Took $(timeDiff "${startTime}")"
        done
        
        inputFile="${workDir}/session_transcript.txt"

        # Write session transcript file
        if ! [[ -f "${inputFile}" ]]; then
            printOutput "3" "--- Merging transcripts for session in ${workDir} ---"
            unset transcriptOutput
            readarray -t transcript_files < <(find "${workDir}/transcripts/" -type f -name "*_transcript.txt")
            for file in "${transcript_files[@]}"; do
                username="${file##*/}"
                username="${username%_transcript.txt}"
                while read -r ts line; do
                    printOutput "5" "Generated formatted line [${ts} ${username}: ${line}]"
                    transcriptOutput+=("${ts} ${username}: ${line}")
                done < "${file}"
            done
            sort -V -o "${inputFile}" < <(printf '%s\n' "${transcriptOutput[@]}")
        else
            printOutput "5" "Found existing trascript file [${inputFile}]"
        fi

        recapFile="${workDir}/session_recap.txt"
        if ! [[ -f "${recapFile}" ]]; then
            printOutput "3" "Generating recap using provider [${LLM_PROVIDER}]..."
            case "${LLM_PROVIDER}" in
                google) 
                    if sendPromptGoogle; then
                        printOutput "5" "Summary generated successfully"
                    else
                        printOutput "1" "Summary generation failed -- Skipping"
                        continue
                    fi
                    ;;
                anthropic)
                    if sendPromptAnthropic; then
                        printOutput "5" "Summary generated successfully"
                    else
                        printOutput "1" "Summary generation failed -- Skipping"
                        continue
                    fi
                    ;;
                openai)
                    if sendPromptOpenAI; then
                        printOutput "5" "Summary generated successfully"
                    else
                        printOutput "1" "Summary generation failed -- Skipping"
                        continue
                    fi
                    ;;
                ollama)
                    if sendPromptOllama; then
                        printOutput "5" "Summary generated successfully"
                    else
                        printOutput "1" "Summary generation failed -- Skipping"
                        continue
                    fi
                    ;;
            esac

            unset summaryArr
            while read -r line; do
                if ! [[ "${line}" == "***" ]]; then
                    summaryArr+=("${line}")
                fi
            done <<<"${summary}"
            # Remove first element if blank
            if [[ -z "${summaryArr[0]}" ]]; then
                summaryArr=("${summaryArr[@]:1}")
            fi
            # Remove last element if blank
            if [[ -z "${summaryArr[-1]}" ]]; then
                unset "summaryArr[-1]"
            fi
            summary="$(printf '%s\n' "${summaryArr[@]}")"
            while [[ "${summary}" =~ .*$'\n\n\n'.* ]]; do
                summary="${summary//$'\n\n\n'/$'\n\n'}"
            done
            formattedDate=$(date -d "${sessionDate}" +"%B %-d, %Y")
            printOutput "5" "Created formatted date [${formattedDate}] from session date [${sessionDate}]"
            summary="## ${formattedDate} Session Recap"$'\n\n'"🤖 LLM Provider: \`${LLM_PROVIDER}\`"$'\n'"📋 Model: \`${LLM_MODEL}\`"$'\n'"⌚ API time: \`${apiTimeDiff}\`"$'\n'"🧾 Tokens: \`${tokensIn} in | ${tokensOut} out | ${totalTokenCount} total\`"$'\n\n'"${summary}"

            printOutput "3" "Summary successfully generated:"
            printOutput "4" "------------------------------------------------------------------"
            while read -r line; do
                printOutput "4" "${line}"
            done <<<"${summary}"
            printOutput "4" "------------------------------------------------------------------"
            echo "${summary}" > "${recapFile}"
            
            # Split message into an array of paragraphs
            # This logic reads the message line by line. It accumulates lines into a
            # 'currentParagraph' variable. When it hits a blank line, it considers
            # the paragraph complete and adds it to the array.
            unset paragraphs
            unset currentParagraph
            # Append two newlines to the end to ensure the loop processes the final paragraph.
            while IFS= read -r line; do
                if [[ -z "${line}" ]]; then
                    # Blank line found: end of a paragraph.
                    if [[ -n "${currentParagraph}" ]]; then
                        # Add the completed paragraph to the array, removing the last trailing newline string.
                        paragraphs+=("${currentParagraph%$'\n'}")
                        unset currentParagraph
                    fi
                else
                    # Not a blank line: append it to the current paragraph.
                    # Discord markdown for headers maxes out at three "#", make sure Gemini didn't send four:
                    while [[ "${line}" == *"####"* ]]; do
                        line="${line//####/###}"
                    done
                    if [[ "${line}" == "***" ]]; then
                        # It's a divider, use proper markdown
                        line="~~            ~~"
                    fi
                    currentParagraph+="${line}"$'\n'
                fi
            done <<< "${summary}"$'\n\n'
            
            # 1. Define the Thread Title
            threadTitle="${formattedDate} Session Recap"
            
            # 2. Start the thread.
            # We send a "Starter" message. This creates the thread in a Forum Channel.
            # We must use '?wait=true' to get the JSON response containing the new Thread/Message ID.
            printOutput "3" "Creating Discord Thread: ${threadTitle}"
            
            startResponse="$(jq -n --arg content "# ${threadTitle}" --arg title "${threadTitle}" '{content: $content, thread_name: $title}')"
            printOutput "5" "Issuing curl command [curl -sS -H \"Content-Type: application/json\" -X POST \"${DISCORD_WEBHOOK_CENSORED}?wait=true\" -d \"${startResponse}\"]"
            discordJson="${workDir}/discord.json"

            # --- Start Timing ---
            startTime="$(($(date +%s%N)/1000000))"

            # Capture HTTP code in variable, save Body to file
            httpCode=$(curl -sS \
              -w "%{http_code}" \
              -o "${discordJson}" \
              -H "Content-Type: application/json" \
              -X POST "${DISCORD_WEBHOOK}?wait=true" \
              -d "${startResponse}" 2>/dev/null)

            curlExitCode="${?}"
            
            # --- End Timing & Calc Duration ---
            endTime="$(($(date +%s%N)/1000000))"
            durationMs="$((endTime - startTime))"
            if [[ "${durationMs}" -lt 1000 ]]; then
                printf -v durationSeconds "0.%03d" "${durationMs}"
            else
                durationSeconds="${durationMs:0:${#durationMs}-3}.${durationMs: -3}"
            fi

            if [[ "${curlExitCode}" -eq "0" ]]; then
                printOutput "5" "Discord curl returned exit code 0"

                # --- DB Logging (Success) ---
                # Read the response from the file we just saved
                apiResponse="$(<"${discordJson}")"

                # Parse fields
                msgId="$(jq -r ".id // empty" <<<"${apiResponse}")"
                channelId="$(jq -r ".channel_id // empty" <<<"${apiResponse}")"
                authorName="$(jq -r ".author.username // empty" <<<"${apiResponse}")"
                discordTs="$(jq -r ".timestamp // empty" <<<"${apiResponse}")"
                msgContent="$(jq -r ".content // empty" <<<"${apiResponse}")"

                # Sanitize for SQL
                safeRequest="${startResponse//\'/\'\'}"
                safeResponse="${apiResponse//\'/\'\'}"
                safeContent="${msgContent//\'/\'\'}"

                sqDb "INSERT INTO discord_logs (message_id, channel_id, author_username, content, discord_timestamp, request_timestamp, request_epoch, duration_seconds, http_status_code, request_json, response_json) VALUES ('${msgId}', '${channelId}', '${authorName}', '${safeContent}', '${discordTs}', '$(date '+%Y-%m-%d %H:%M:%S')', $(date +%s), ${durationSeconds}, ${httpCode}, '${safeRequest}', '${safeResponse}');"
                # ----------------------------

            else
                printOutput "1" "Discord curl call returned non-zero exit code [${curlExitCode}]"
                
                # --- DB Logging (Failure) ---
                safeRequest="${startResponse//\'/\'\'}"
                sqDb "INSERT INTO discord_logs (request_timestamp, request_epoch, duration_seconds, request_json, finish_reason) VALUES ('$(date '+%Y-%m-%d %H:%M:%S')', $(date +%s), ${durationSeconds}, '${safeRequest}', 'CURL_ERROR_${curlExitCode}');"
                # ----------------------------

                exit 1
            fi
            
            # Write our Discord JSON
            startResponse="$(<"${discordJson}")"
            rm -f "${discordJson}"

            # 3. Extract the ID. In a Forum Channel, the starting message ID is the Thread ID.
            threadId="$(jq -r '.id' <<<"${startResponse}")"

            if [[ "${threadId}" == "null" || -z "${threadId}" ]]; then
                discordError="$(jq -r ".message" <<<"${startResponse}")"
                discordErrorCode="$(jq -r ".code" <<<"${startResponse}")"
                
                if [[ "${discordErrorCode}" == "220003" ]]; then
                    printOutput "2" "Failed to create thread -- Webhook is set to regular (non-Forum) text channel"
                else
                    printOutput "1" "Failed to create thread -- Discord returned error code ${discordErrorCode} [${discordError}]"
                fi
                # Fallback: Send to the main channel if thread creation fails
                TARGET_WEBHOOK="${DISCORD_WEBHOOK}"
            else
                TARGET_WEBHOOK="${DISCORD_WEBHOOK}?thread_id=${threadId}"
                printOutput "3" "Thread created successfully [ID: ${threadId}]. Sending paragraphs..."
                # Target the specific thread for subsequent messages
            fi

            # 4. Send the paragraphs to the new Thread
            for paragraph in "${paragraphs[@]}"; do
                if [[ -z "${paragraph}" ]]; then
                    continue
                fi

                discordMsgLimit="2000"
                if (( ${#paragraph} > discordMsgLimit )); then
                    printOutput "2" "Paragraph exceeds limit; splitting..."
                    temp_paragraph="${paragraph}"
                    while ((${#temp_paragraph} > 0)); do
                        # Use the TARGET_WEBHOOK (with threadId)
                        send_chunk "${TARGET_WEBHOOK}" "${temp_paragraph:0:discordMsgLimit}"
                        temp_paragraph="${temp_paragraph:discordMsgLimit}"
                        sleep 0.5
                    done
                else
                    # Use the TARGET_WEBHOOK (with threadId)
                    send_chunk "${TARGET_WEBHOOK}" "${paragraph}"
                fi
                sleep 1
            done
        fi
        
        # If we have a custom script defined, run it
        if [[ -n "${CUSTOM_SCRIPT}" ]]; then
            printOutput "3" "Executing custom script: [${CUSTOM_SCRIPT}]"
            
            # Execute the script, passing the recap file path as $1
            printOutput "4" "=== Custom script output start ==="
            
            while read -r line; do
                printOutput "4" "${line}"
            done < <(/app/"${CUSTOM_SCRIPT}" "${recapFile}")
            # Capture the exit code to log success or failure
            scriptExitCode="${?}"
            
            printOutput "4" "=== Custom script output end ==="
            
            if [[ "${scriptExitCode}" -eq "0" ]]; then
                printOutput "5" "Custom script [${CUSTOM_SCRIPT}] finished successfully"
            else
                printOutput "1" "Custom script [${CUSTOM_SCRIPT}] failed with exit code [${scriptExitCode}]"
            fi
        fi

        # Clean up the original zip file after processing.
        origZip="${baseDir}/$(<"${workDir}/.source")"
        if [[ -f "${origZip}" ]]; then
            printOutput "3" "Processing complete for this session. Removing original zip file [${origZip}]"
            rm -f "${origZip}"
        fi
        
        # Remove flac files (If necessary)
        if [[ "${KEEP_AUDIO}" == "false" ]]; then
             printOutput "3" "Deleting source audio files"
             find "${workDir}" -maxdepth 1 -name "*.flac" -delete
        fi
        
        # Write our completion file
        date > "${workDir}/.complete"
        
        unset sessionLog
    done

    printOutput "3" "All processing complete | Execution took $(timeDiff "${totalStartTime}")"
    
    if [[ -n "${shutdown_requested}" ]]; then
        break
    fi

    # Run sleep in the background and get its Process ID (PID)
    respawnTime="$(date +%s)"
    respawnTime="$(( respawnTime + RESPAWN_TIME ))"
    printOutput "3" "Sleeping for [${RESPAWN_TIME}] seconds, will respawn at $(date -d "@${respawnTime}")"
    sleep "${RESPAWN_TIME}" &
    sleep_pid="${!}"
    
    # Wait for the sleep command to finish (or be killed by the trap)
    wait "${sleep_pid}"
    unset sleep_pid
done

printOutput "3" "Shutting down Scribble"
exit 0
