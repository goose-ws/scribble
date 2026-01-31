import os
import json
import time
import base64
import requests
import logging
import pytz
from datetime import datetime
from database import db
from models import LLMLog, DiscordLog, Session

# --- HELPER FUNCTIONS ---

def calculate_cost(prompt_tokens, completion_tokens, config):
    try:
        # Robust conversion that handles empty strings or bad text
        input_cost_str = str(config.get('llm_input_cost', '0')).strip()
        output_cost_str = str(config.get('llm_output_cost', '0')).strip()
        
        # Helper to safely parse float
        def safe_float(val):
            try: return float(val)
            except ValueError: return 0.0

        input_cost_per_m = safe_float(input_cost_str)
        output_cost_per_m = safe_float(output_cost_str)
        
        cost = (prompt_tokens * input_cost_per_m / 1_000_000) + \
               (completion_tokens * output_cost_per_m / 1_000_000)
        return round(cost, 6)
    except Exception as e:
        logging.error(f"Cost Calculation Error: {e}")
        return 0.0

def format_duration(seconds):
    """Formats seconds to matching bash style (e.g. 12.345s)"""
    return f"{seconds:.3f}s"

def log_llm_request(provider, model, usage, timing, req_data, res_data, status, finish_reason, config):
    req_str = json.dumps(req_data)
    res_str = json.dumps(res_data)
    
    if config.get('db_space_saver'):
        if 'contents' in req_data: # Gemini
            try:
                for c in req_data['contents']:
                    for p in c.get('parts', []):
                        if 'inlineData' in p: p['inlineData']['data'] = "[TRUNCATED]"
                req_str = json.dumps(req_data)
            except: pass

    log_entry = LLMLog(
        provider=provider,
        model_name=model,
        prompt_tokens=usage.get('prompt', 0),
        completion_tokens=usage.get('completion', 0),
        total_tokens=usage.get('total', 0),
        cost=usage.get('cost', 0.0),
        duration_seconds=timing,
        http_status=status,
        finish_reason=finish_reason,
        request_json=req_str,
        response_json=res_str
    )
    db.session.add(log_entry)
    db.session.commit()

# --- PROVIDER FUNCTIONS ---
# All providers now return Tuple: (text_content, stats_dict)

def send_google(prompt, transcript_path, config):
    api_key = config['llm_api_key']
    model = config['llm_model']
    
    with open(transcript_path, "rb") as f:
        encoded_file = base64.b64encode(f.read()).decode('utf-8')
    
    payload = {
        "contents": [{
            "parts": [
                {"inlineData": {"mimeType": "text/plain", "data": encoded_file}},
                {"text": prompt}
            ]
        }]
    }

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?key={api_key}"
    
    start_time = time.time()
    response = requests.post(url, json=payload)
    duration = time.time() - start_time
    
    try:
        res_json = response.json()
    except:
        res_json = {"error": response.text}

    try:
        last_chunk = res_json[-1] if isinstance(res_json, list) else res_json
        meta = last_chunk.get('usageMetadata', {})
        
        usage = {
            'prompt': meta.get('promptTokenCount', 0),
            'completion': meta.get('candidatesTokenCount', 0) + meta.get('thoughtsTokenCount', 0),
            'total': meta.get('totalTokenCount', 0)
        }
        usage['cost'] = calculate_cost(usage['prompt'], usage['completion'], config)
        
        full_text = ""
        if isinstance(res_json, list):
            for chunk in res_json:
                if 'candidates' in chunk:
                    for part in chunk['candidates'][0]['content']['parts']:
                        full_text += part.get('text', '')
        
        finish_reason = last_chunk.get('candidates', [{}])[0].get('finishReason', 'UNKNOWN')
        
    except Exception as e:
        logging.error(f"Error parsing Gemini response: {e}")
        usage = {'prompt': 0, 'completion': 0, 'total': 0, 'cost': 0}
        full_text = ""
        finish_reason = "PARSE_ERROR"

    log_llm_request('Google', model, usage, duration, payload, res_json, response.status_code, finish_reason, config)
    
    if response.status_code != 200 or not full_text:
        raise Exception(f"Gemini API Error: {response.text}")
    
    stats = {
        'provider': 'Google',
        'model': model,
        'duration': duration,
        'tokens': usage
    }
    return full_text, stats

def send_anthropic(prompt, transcript_path, config):
    api_key = config['llm_api_key']
    model = config['llm_model']
    
    files_url = "https://api.anthropic.com/v1/files"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "files-api-2025-04-14"
    }
    
    with open(transcript_path, 'rb') as f:
        file_response = requests.post(files_url, headers=headers, files={"file": f})
    
    if file_response.status_code not in [200, 201]:
         raise Exception(f"Anthropic File Upload Failed: {file_response.text}")
         
    file_id = file_response.json().get('id')
    
    msg_url = "https://api.anthropic.com/v1/messages"
    payload = {
        "model": model,
        "max_tokens": 4096,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "document", "source": {"type": "file", "file_id": file_id}}
                ]
            }
        ]
    }
    
    start_time = time.time()
    response = requests.post(msg_url, headers=headers, json=payload)
    duration = time.time() - start_time
    
    res_json = response.json()
    
    usage = {
        'prompt': res_json.get('usage', {}).get('input_tokens', 0),
        'completion': res_json.get('usage', {}).get('output_tokens', 0),
    }
    usage['total'] = usage['prompt'] + usage['completion']
    usage['cost'] = calculate_cost(usage['prompt'], usage['completion'], config)
    
    content_list = res_json.get('content', [])
    full_text = "".join([c['text'] for c in content_list if c['type'] == 'text'])
    finish_reason = res_json.get('stop_reason', 'unknown')

    log_llm_request('Anthropic', model, usage, duration, payload, res_json, response.status_code, finish_reason, config)

    if response.status_code != 200:
        raise Exception(f"Anthropic API Error: {response.text}")

    stats = {
        'provider': 'Anthropic',
        'model': model,
        'duration': duration,
        'tokens': usage
    }
    return full_text, stats

def send_openai(prompt, transcript_path, config):
    api_key = config['llm_api_key']
    model = config['llm_model']
    
    with open(transcript_path, "rb") as f:
        encoded_file = base64.b64encode(f.read()).decode('utf-8')
    
    payload = {
        "model": model,
        "input": [{
            "role": "user",
            "content": [
                {
                    "type": "input_file",
                    "filename": os.path.basename(transcript_path),
                    "file_data": f"data:text/plain;base64,{encoded_file}"
                },
                {
                    "type": "input_text",
                    "text": prompt
                }
            ]
        }]
    }

    url = "https://api.openai.com/v1/responses"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    start_time = time.time()
    response = requests.post(url, headers=headers, json=payload)
    duration = time.time() - start_time
    
    res_json = response.json()
    
    usage_data = res_json.get('usage', {})
    usage = {
        'prompt': usage_data.get('prompt_tokens', 0),
        'completion': usage_data.get('completion_tokens', 0),
        'total': usage_data.get('total_tokens', 0)
    }
    usage['cost'] = calculate_cost(usage['prompt'], usage['completion'], config)
    
    full_text = res_json.get('output_text', '')
    if not full_text:
        full_text = res_json.get('choices', [{}])[0].get('message', {}).get('content', '')

    finish_reason = res_json.get('choices', [{}])[0].get('finish_reason', 'unknown')

    log_llm_request('OpenAI', model, usage, duration, payload, res_json, response.status_code, finish_reason, config)
    
    if response.status_code != 200:
        raise Exception(f"OpenAI API Error: {response.text}")
        
    stats = {
        'provider': 'OpenAI',
        'model': model,
        'duration': duration,
        'tokens': usage
    }
    return full_text, stats

def send_ollama(prompt, transcript_path, config):
    base_url = config.get('ollama_url') or os.environ.get("OLLAMA_URL", "http://ollama:11434")
    
    # Clean up URL (handle trailing slashes or missing http)
    if not base_url.startswith('http'):
        base_url = f"http://{base_url}"
    base_url = base_url.rstrip('/')
    
    url = f"{base_url}/v1/chat/completions"
    
    with open(transcript_path, "r", encoding="utf-8", errors="ignore") as f:
        file_content = f.read()

    combined_content = f"{prompt}\n\n### {os.path.basename(transcript_path)}\n{file_content}"
    
    payload = {
        "model": config['llm_model'],
        "messages": [{"role": "user", "content": combined_content}],
        "stream": False
    }

    start_time = time.time()
    response = requests.post(url, json=payload)
    duration = time.time() - start_time
    
    res_json = response.json()
    
    usage_data = res_json.get('usage', {})
    usage = {
        'prompt': usage_data.get('prompt_tokens', 0),
        'completion': usage_data.get('completion_tokens', 0),
        'total': usage_data.get('total_tokens', 0)
    }
    
    full_text = res_json.get('choices', [{}])[0].get('message', {}).get('content', '')
    finish_reason = res_json.get('choices', [{}])[0].get('finish_reason', 'unknown')

    log_llm_request('Ollama', config['llm_model'], usage, duration, payload, res_json, response.status_code, finish_reason, config)
    
    stats = {
        'provider': 'Ollama',
        'model': config['llm_model'],
        'duration': duration,
        'tokens': usage
    }
    return full_text, stats

# --- DISCORD LOGIC ---

def send_discord(summary_text, webhook_url, title_date):
    if not webhook_url:
        return
        
    title = f"{title_date} Session Recap"
    
    # 1. Create Thread
    start_payload = {
        "content": f"# {title}",
        "thread_name": title
    }
    
    res = requests.post(f"{webhook_url}?wait=true", json=start_payload)
    
    if res.status_code not in [200, 201, 204]:
        logging.error(f"Discord Thread Creation Failed: {res.text}")
        thread_webhook = webhook_url
    else:
        thread_id = res.json().get('id')
        thread_webhook = f"{webhook_url}?thread_id={thread_id}"

    # 2. Chunk and Send
    paragraphs = summary_text.split('\n\n')
    
    for p in paragraphs:
        if not p.strip(): continue
        
        if len(p) > 1900:
            chunks = [p[i:i+1900] for i in range(0, len(p), 1900)]
            for chunk in chunks:
                requests.post(thread_webhook, json={"content": chunk})
                time.sleep(0.5)
        else:
            requests.post(thread_webhook, json={"content": p})
            time.sleep(1)

# --- MAIN ENGINE ENTRY ---

def run_summary(job, config):
    session = Session.query.get(job.session_id)
    
    transcript_path = os.path.join(session.directory_path, "session_transcript.txt")
    if not os.path.exists(transcript_path):
         raise Exception("Transcript file not found.")

    prompt = session.campaign.system_prompt
    if not prompt:
        prompt = "Summarize this DnD session."
        
    provider = config.get('llm_provider', 'Google')
    job.logs += f"\nStarting Summary with {provider}..."
    
    try:
        # 1. Get Summary and Stats
        if provider == 'Google':
            summary, stats = send_google(prompt, transcript_path, config)
        elif provider == 'Anthropic':
            summary, stats = send_anthropic(prompt, transcript_path, config)
        elif provider == 'OpenAI':
            summary, stats = send_openai(prompt, transcript_path, config)
        elif provider == 'Ollama':
            summary, stats = send_ollama(prompt, transcript_path, config)
        else:
            raise Exception(f"Unknown Provider: {provider}")

        # 2. Format Date (LOCAL TIME)
        # Using pytz to ensure correct local date based on TZ env var
        target_tz_str = os.environ.get('TZ', 'UTC')
        try:
            local_tz = pytz.timezone(target_tz_str)
            # Ensure session_date is UTC aware
            utc_dt = session.session_date.replace(tzinfo=pytz.utc)
            local_dt = utc_dt.astimezone(local_tz)
            formatted_date = local_dt.strftime("%B %-d, %Y")
        except Exception as e:
            logging.error(f"Timezone error: {e}")
            formatted_date = session.session_date.strftime("%B %d, %Y")

        # 3. Construct Header (Matching Bash Logic)
        tokens = stats['tokens']
        header = (
            f"## {formatted_date} Session Recap\n\n"
            f"🤖 LLM Provider: `{stats['provider']}`\n"
            f"📋 Model: `{stats['model']}`\n"
            f"⌚ API time: `{format_duration(stats['duration'])}`\n"
            f"🧾 Tokens: `{tokens['prompt']} in | {tokens['completion']} out | {tokens['total']} total`\n\n"
        )
        
        final_content = header + summary

        # 4. Save to Disk
        recap_path = os.path.join(session.directory_path, "session_recap.txt")
        with open(recap_path, 'w', encoding='utf-8') as f:
            f.write(final_content)
        
        session.summary_text = final_content
        db.session.commit()
        
        job.logs += "\nSummary generated successfully."
        
        # 5. Send to Discord
        if session.campaign.discord_webhook:
            job.logs += "\nSending to Discord..."
            send_discord(final_content, session.campaign.discord_webhook, formatted_date)
            job.logs += " Sent."

    except Exception as e:
        job.logs += f"\nLLM Error: {str(e)}"
        raise e