import os
import json
import time
import base64
import requests
import logging
import pytz
import re
from datetime import datetime
from database import db
from models import LLMLog, DiscordLog, Session
from string import Template

# --- HELPER FUNCTIONS ---

def calculate_cost(prompt_tokens, completion_tokens, config):
    try:
        input_cost_str = str(config.get('llm_input_cost', '0')).strip()
        output_cost_str = str(config.get('llm_output_cost', '0')).strip()
        
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

def clean_markdown(text):
    DIVIDER = "~~          ~~"

    text = re.sub(r'^#{4,}\s*', '### ', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*\*\*\*\s*$', DIVIDER, text, flags=re.MULTILINE)
    text = re.sub(r'\s*' + re.escape(DIVIDER) + r'\s*', f'\n\n{DIVIDER}\n\n', text)
    
    return text.strip()

def format_duration(seconds):
    return f"{seconds:.3f}s"

def log_llm_request(provider, model, usage, timing, req_data, res_data, status, finish_reason, config):
    req_str = json.dumps(req_data)
    res_str = json.dumps(res_data)
    
    if config.get('db_space_saver'):
        if 'contents' in req_data:
            try:
                for c in req_data['contents']:
                    for p in c.get('parts', []):
                        if 'inlineData' in p: p['inlineData']['data'] = "[TRUNCATED]"
                req_str = json.dumps(req_data)
            except: pass

        try:
            items = res_data if isinstance(res_data, list) else [res_data]
            
            data_modified = False
            for item in items:
                if 'candidates' in item:
                    for cand in item['candidates']:
                        if 'content' in cand and 'parts' in cand['content']:
                            for part in cand['content']['parts']:
                                if 'thought' in part:
                                    part['thought'] = "[TRUNCATED]"
                                    data_modified = True
            
            if data_modified:
                res_str = json.dumps(res_data)
        except Exception:
            pass

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

# --- RECAP CONTEXT BUILDER ---

def build_recap_context_file(session, config):
    import tempfile

    campaign = session.campaign
    if not campaign.recap_context_enabled:
        return None

    count = int(campaign.recap_context_count or 0)

    query = (
        Session.query
        .filter(
            Session.campaign_id == session.campaign_id,
            Session.id != session.id,
            Session.summary_text != None,
            Session.summary_text != ''
        )
        .order_by(Session.session_number.desc())
    )

    if count > 0:
        query = query.limit(count)

    prior_sessions = query.all()

    if not prior_sessions:
        return None

    prior_sessions = list(reversed(prior_sessions))

    lines = ["=== Previous Session Recaps ==="]
    for s in prior_sessions:
        date_str = s.session_date.strftime('%B %d, %Y')
        lines.append(f"\n--- Session {s.session_number} ({date_str}) ---\n")
        lines.append(s.summary_text.strip())

    context_text = "\n".join(lines)

    tmp = tempfile.NamedTemporaryFile(
        mode='w', suffix='.txt', prefix='recap_context_',
        delete=False, encoding='utf-8'
    )
    tmp.write(context_text)
    tmp.close()

    return tmp.name


# --- PROVIDER FUNCTIONS ---

def send_google(prompt, transcript_path, config, recap_context_path=None):
    api_key = config.get('google_api_key')
    if not api_key:
        raise Exception("Google API Key is missing. Please configure it in Settings.")
        
    model = config['active_llm_model']
    
    with open(transcript_path, "rb") as f:
        encoded_file = base64.b64encode(f.read()).decode('utf-8')

    parts = [{"inlineData": {"mimeType": "text/plain", "data": encoded_file}}]

    if recap_context_path:
        with open(recap_context_path, "rb") as f:
            encoded_recap = base64.b64encode(f.read()).decode('utf-8')
        parts.append({"inlineData": {"mimeType": "text/plain", "data": encoded_recap}})

    parts.append({"text": prompt})

    payload = {
        "contents": [{"parts": parts}]
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

def send_anthropic(prompt, transcript_path, config, recap_context_path=None):
    api_key = config.get('anthropic_api_key')
    if not api_key:
        raise Exception("Anthropic API Key is missing. Please configure it in Settings.")
        
    model = config['active_llm_model']
    
    files_url = "https://api.anthropic.com/v1/files"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "files-api-2025-04-14"
    }
    
    with open(transcript_path, 'rb') as f:
        file_response = requests.post(files_url, headers=headers,
                                      files={"file": (os.path.basename(transcript_path), f, "text/plain")})
    
    if file_response.status_code not in [200, 201]:
         raise Exception(f"Anthropic File Upload Failed: {file_response.text}")
         
    file_id = file_response.json().get('id')

    recap_file_id = None
    if recap_context_path:
        with open(recap_context_path, 'rb') as f:
            recap_response = requests.post(files_url, headers=headers,
                                           files={"file": ("previous_recaps.txt", f, "text/plain")})
        if recap_response.status_code in [200, 201]:
            recap_file_id = recap_response.json().get('id')
        else:
            logging.warning(f"Anthropic recap context upload failed: {recap_response.text}")

    content = [
        {"type": "text", "text": prompt},
        {"type": "document", "source": {"type": "file", "file_id": file_id}}
    ]
    if recap_file_id:
        content.append({"type": "document", "source": {"type": "file", "file_id": recap_file_id}})

    msg_url = "https://api.anthropic.com/v1/messages"
    payload = {
        "model": model,
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": content}]
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

def send_openai(prompt, transcript_path, config, recap_context_path=None):
    api_key = config.get('openai_api_key')
    if not api_key:
        raise Exception("OpenAI API Key is missing. Please configure it in Settings.")
        
    model = config['active_llm_model']
    
    with open(transcript_path, "rb") as f:
        encoded_file = base64.b64encode(f.read()).decode('utf-8')

    content = [
        {
            "type": "input_file",
            "filename": os.path.basename(transcript_path),
            "file_data": f"data:text/plain;base64,{encoded_file}"
        }
    ]

    if recap_context_path:
        with open(recap_context_path, "rb") as f:
            encoded_recap = base64.b64encode(f.read()).decode('utf-8')
        content.append({
            "type": "input_file",
            "filename": "previous_recaps.txt",
            "file_data": f"data:text/plain;base64,{encoded_recap}"
        })

    content.append({"type": "input_text", "text": prompt})

    payload = {
        "model": model,
        "input": [{"role": "user", "content": content}]
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

def send_ollama(prompt, transcript_path, config, recap_context_path=None):
    base_url = config.get('ollama_url') or os.environ.get("OLLAMA_URL", "http://ollama:11434")
    model = config['active_llm_model']
    
    if not base_url.startswith('http'):
        base_url = f"http://{base_url}"
    base_url = base_url.rstrip('/')
    
    url = f"{base_url}/v1/chat/completions"
    
    with open(transcript_path, "r", encoding="utf-8", errors="ignore") as f:
        file_content = f.read()

    recap_section = ""
    if recap_context_path:
        with open(recap_context_path, "r", encoding="utf-8", errors="ignore") as f:
            recap_content = f.read()
        recap_section = f"\n\n### previous_recaps.txt\n{recap_content}"

    combined_content = f"{prompt}\n\n### {os.path.basename(transcript_path)}\n{file_content}{recap_section}"
    
    payload = {
        "model": model,
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

    log_llm_request('Ollama', model, usage, duration, payload, res_json, response.status_code, finish_reason, config)
    
    stats = {
        'provider': 'Ollama',
        'model': model,
        'duration': duration,
        'tokens': usage
    }
    return full_text, stats

# --- DISCORD LOGIC ---
def send_discord_request(url, payload, session_id=None):
    start_time = time.time()
    try:
        response = requests.post(url, json=payload)
        duration = time.time() - start_time
        
        try:
            res_json = response.json()
        except:
            res_json = {"body": response.text}
            
        log = DiscordLog(
            session_id=session_id,
            message_id=str(res_json.get('id', '')),
            channel_id=str(res_json.get('channel_id', '')),
            content=payload.get('content', '')[:3000],
            duration_seconds=duration,
            http_status=response.status_code,
            request_json=json.dumps(payload),
            response_json=json.dumps(res_json)
        )
        db.session.add(log)
        db.session.commit()
        
        return response
    except Exception as e:
        logging.error(f"Discord Request Failed: {e}")
        class DummyResponse:
            status_code = 500
            text = str(e)
            def json(self): return {}
        return DummyResponse()

def send_discord(summary_text, webhook_url, title_date, session_id=None):
    if not webhook_url:
        return
        
    title = f"{title_date} Session Recap"
    
    start_payload = {
        "content": f"# {title}",
        "thread_name": title
    }
    
    res = send_discord_request(f"{webhook_url}?wait=true", start_payload, session_id)
    
    if res.status_code not in [200, 201, 204]:
        logging.error(f"Discord Thread Creation Failed: {res.text}")
        thread_webhook = webhook_url
    else:
        try:
            thread_id = res.json().get('id')
            thread_webhook = f"{webhook_url}?thread_id={thread_id}"
        except:
            thread_webhook = webhook_url

    paragraphs = summary_text.split('\n\n')
    
    for p in paragraphs:
        if not p.strip(): continue
        
        if len(p) > 1900:
            chunks = [p[i:i+1900] for i in range(0, len(p), 1900)]
            for chunk in chunks:
                send_discord_request(thread_webhook, {"content": chunk}, session_id)
                time.sleep(0.5)
        else:
            send_discord_request(thread_webhook, {"content": p}, session_id)
            time.sleep(1)

# --- MAIN ENGINE ENTRY ---
def run_summary(job, config, post_to_discord_enabled=True):
    session = Session.query.get(job.session_id)
    
    transcript_path = os.path.join(session.directory_path, "session_transcript.txt")
    
    if not os.path.exists(transcript_path):
        if session.transcript_text:
            os.makedirs(session.directory_path, exist_ok=True)
            with open(transcript_path, 'w', encoding='utf-8') as f:
                f.write(session.transcript_text)
            job.logs += "\n[System] Restored transcript file from database."
        else:
             raise Exception("Transcript file not found (Disk or DB).")

    prompt = session.campaign.system_prompt
    if not prompt:
        prompt = "Summarize this DnD session."
        
    template = Template(prompt)
    
    target_tz_str = os.environ.get('TZ', 'UTC')
    try:
        local_tz = pytz.timezone(target_tz_str)
        utc_dt = session.session_date.replace(tzinfo=pytz.utc)
        local_dt = utc_dt.astimezone(local_tz)
        formatted_date = local_dt.strftime("%B %-d, %Y")
    except Exception:
        formatted_date = session.session_date.strftime("%B %d, %Y")

    prompt = template.safe_substitute(
        campaignName=session.campaign.name,
        sessionNumber=session.session_number,
        sessionDate=formatted_date
    )
    
    # Allow Campaigns to Override Default LLM
    effective_provider = session.campaign.llm_provider or config.get('default_llm_provider', 'Google')
    effective_model = session.campaign.llm_model or config.get('default_llm_model', 'gemini-2.5-flash')
    
    config['active_llm_provider'] = effective_provider
    config['active_llm_model'] = effective_model
    
    provider = effective_provider
    job.logs += f"\nStarting Summary with {provider} ({effective_model})..."

    recap_context_path = None
    try:
        recap_context_path = build_recap_context_file(session, config)
        if recap_context_path:
            job.logs += "\nRecap context file built — attaching previous recaps."
    except Exception as e:
        logging.warning(f"Failed to build recap context: {e}")
        job.logs += f"\nWarning: Could not build recap context ({e}). Continuing without it."

    try:
        if provider == 'Google':
            summary, stats = send_google(prompt, transcript_path, config, recap_context_path)
        elif provider == 'Anthropic':
            summary, stats = send_anthropic(prompt, transcript_path, config, recap_context_path)
        elif provider == 'OpenAI':
            summary, stats = send_openai(prompt, transcript_path, config, recap_context_path)
        elif provider == 'Ollama':
            summary, stats = send_ollama(prompt, transcript_path, config, recap_context_path)
        else:
            raise Exception(f"Unknown Provider: {provider}")

        target_tz_str = os.environ.get('TZ', 'UTC')
        try:
            local_tz = pytz.timezone(target_tz_str)
            utc_dt = session.session_date.replace(tzinfo=pytz.utc)
            local_dt = utc_dt.astimezone(local_tz)
            formatted_date = local_dt.strftime("%B %-d, %Y")
        except Exception as e:
            logging.error(f"Timezone error: {e}")
            formatted_date = session.session_date.strftime("%B %d, %Y")

        tokens = stats['tokens']
        header = (
            f"## {formatted_date} Session Recap\n\n"
            f"🤖 LLM Provider: `{stats['provider']}`\n"
            f"📋 Model: `{stats['model']}`\n"
            f"⌚ API time: `{format_duration(stats['duration'])}`\n"
            f"🧾 Tokens: `{tokens['prompt']} in | {tokens['completion']} out | {tokens['total']} total`\n\n"
        )
        
        raw_content = header + summary
        final_content = clean_markdown(raw_content)

        recap_path = os.path.join(session.directory_path, "session_recap.txt")
        with open(recap_path, 'w', encoding='utf-8') as f:
            f.write(final_content)
        
        session.summary_text = final_content
        db.session.commit()
        
        job.logs += "\nSummary generated successfully."
        
        if post_to_discord_enabled:
            if session.campaign.discord_webhook:
                job.logs += "\nSending to Discord..."
                send_discord(final_content, session.campaign.discord_webhook, formatted_date, session.id)
                job.logs += " Sent."
            else:
                job.logs += "\nDiscord Webhook not configured. Skipping."
        else:
            job.logs += "\nDiscord posting skipped (Generation Only)."

    except Exception as e:
        job.logs += f"\nLLM Error: {str(e)}"
        raise e
    finally:
        if recap_context_path and os.path.exists(recap_context_path):
            try:
                os.unlink(recap_context_path)
            except Exception:
                pass

def run_discord_post(job, config):
    session = Session.query.get(job.session_id)
    
    if not session.summary_text:
        recap_path = os.path.join(session.directory_path, "session_recap.txt")
        if os.path.exists(recap_path):
            with open(recap_path, 'r', encoding='utf-8') as f:
                session.summary_text = f.read()
        else:
             raise Exception("No summary text found to post.")

    if not session.campaign.discord_webhook:
        raise Exception("No Discord Webhook configured for this campaign.")

    formatted_date = session.session_date.strftime("%B %-d, %Y")
    
    job.logs += f"\nPosting existing summary to Discord..."
    send_discord(session.summary_text, session.campaign.discord_webhook, formatted_date, session.id)
    job.logs += " Sent."