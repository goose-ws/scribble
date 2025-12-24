import os
import re
import math
import requests
import secrets
import time
import sqlite3
import json
from flask import Flask, request, render_template, redirect, url_for, session, flash, send_from_directory, jsonify, abort
from werkzeug.utils import secure_filename

app = Flask(__name__)

# --- Configuration ---
DB_PATH = "/app/api.db"
PROMPT_FILE_PATH = "/app/prompt.txt"
SAVE_PATH = "/app/Sessions"
TEMP_PATH = "/app/.incomplete_uploads" 
ALLOWED_EXTENSIONS = {'zip'} 

os.makedirs(SAVE_PATH, exist_ok=True)
os.makedirs(TEMP_PATH, exist_ok=True)

# --- Secret Key and Password ---
app.secret_key = os.environ.get('WEB_COOKIE_KEY')
if not app.secret_key:
    app.secret_key = os.urandom(24)
    print("---------------------------------------------------", flush=True)
    print("---                 !!! WARNING !!!               ---", flush=True)
    print("---   WEB_COOKIE_KEY environment variable not   ---", flush=True)
    print("---   set. A temporary key has been generated.     ---", flush=True)
    print("---    LOGIN SESSIONS WILL NOT BE PERSISTENT      ---", flush=True)
    print("---------------------------------------------------", flush=True)

APP_PASSWORD = os.environ.get('WEB_PASSWORD')
if not APP_PASSWORD:
    APP_PASSWORD = secrets.token_urlsafe(12)
    print("---------------------------------------------------", flush=True)
    print("---         WEB UPLOADER PASSWORD NOT SET         ---", flush=True)
    print(f"---    Generated Password: {APP_PASSWORD}    ---", flush=True)
    print("---------------------------------------------------", flush=True)

# --- Helper Functions ---
def is_user_logged_in():
    return session.get('logged_in')

def verify_csrf():
    """Checks the CSRF token on POST requests."""
    token = request.form.get('csrf_token')
    if not token or token != session.get('csrf_token'):
        abort(403, description="CSRF Validation Failed. Please refresh and try again.")

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def download_file_from_url(url, save_dir, temp_dir):
    try:
        filename = secure_filename(url.split('/')[-1] or "downloaded_file.zip")
        if not allowed_file(filename):
            return "Error: URL must point to a .zip file."
        
        temp_file_path = os.path.join(temp_dir, filename)
        final_file_path = os.path.join(save_dir, filename)

        # Stream to temp folder first
        with requests.get(url, stream=True) as r:
            r.raise_for_status()
            with open(temp_file_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
        
        # Atomic Move to final destination
        os.rename(temp_file_path, final_file_path)
        return f"Successfully downloaded '{filename}'."
    except requests.exceptions.RequestException as e:
        return f"Error downloading from URL: {e}"
    except Exception as e:
        return f"System Error: {e}"

def safe_backup(path):
    """Renames a file or directory to path.<timestamp>.old"""
    if os.path.exists(path):
        timestamp = int(time.time())
        backup_name = f"{path}.{timestamp}.old"
        try:
            os.rename(path, backup_name)
            print(f"Backed up {path} to {backup_name}")
        except OSError as e:
            print(f"Error backing up {path}: {e}")

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

@app.context_processor
def inject_csrf_token():
    return dict(csrf_token=session.get('csrf_token'))

# --- Routes ---
@app.route('/', methods=['GET', 'POST'])
def upload_page():
    if not is_user_logged_in():
        return redirect(url_for('login'))
        
    if request.method == 'POST':
        verify_csrf() # <--- SECURITY CHECK
        
        # Handle File Upload
        file = request.files.get('file')
        if file and file.filename:
            if allowed_file(file.filename):
                filename = secure_filename(file.filename)
                
                temp_file = os.path.join(TEMP_PATH, filename)
                final_file = os.path.join(SAVE_PATH, filename)
                
                try:
                    # 1. Save to hidden temp folder
                    file.save(temp_file)
                    
                    # 2. Atomic Move to real folder
                    os.rename(temp_file, final_file)
                    
                    flash(f"File '{filename}' uploaded successfully!", 'success')
                except Exception as e:
                    flash(f"Upload failed: {str(e)}", 'error')
            else:
                flash("Invalid file type. Please upload a .zip file.", 'error')
            return redirect(url_for('upload_page'))
        
        # Handle URL Download
        url = request.form.get('url')
        if url:
            # We updated the helper to use TEMP_PATH too
            message = download_file_from_url(url, SAVE_PATH, TEMP_PATH)
            flash(message, 'success' if "Error" not in message else 'error')
            return redirect(url_for('upload_page'))
            
        flash("No file or URL provided.", 'error')
    return render_template('upload.html')

@app.route('/api/status')
def api_status():
    if not is_user_logged_in():
        return jsonify({'error': 'Unauthorized'}), 401
    data = scan_sessions()
    return jsonify(data)

@app.route('/files/<path:session_name>/<path:filename>')
def serve_session_file(session_name, filename):
    """Allows authenticated users to download/view files from a session."""
    if not is_user_logged_in():
        return redirect(url_for('login'))
    
    # 1. Secure the Session Directory Name
    safe_session = secure_filename(session_name)
    target_dir = os.path.join(SAVE_PATH, safe_session)
    
    # 2. Secure the File Path
    if ".." in filename or filename.startswith("/"):
        abort(404)

    # 3. Generate response and override mimetype for logs
    response = send_from_directory(target_dir, filename)
    
    if filename.endswith('.log'):
        response.headers['Content-Type'] = 'text/plain; charset=utf-8'
    
    return response

def scan_sessions():
    if not os.path.exists(SAVE_PATH):
        return []

    sessions = []
    date_folder_pattern = re.compile(r'^\d{4}-\d{2}-\d{2}$')

    for item_name in sorted(os.listdir(SAVE_PATH), reverse=True): 
        item_path = os.path.join(SAVE_PATH, item_name)

        if os.path.isfile(item_path) and item_name.lower().endswith('.zip'):
            sessions.append({
                'name': item_name, 
                'status': 'Waiting', 
                'progress': 0, 
                'files': []
            })
            continue

        if os.path.isdir(item_path) and date_folder_pattern.match(item_name):
            session_dir = item_path
            
            # --- FILE SCANNING ---
            file_list = []
            if os.path.exists(session_dir):
                for root, dirs, files in os.walk(session_dir):
                    for f in files:
                        if f.endswith(('.txt', '.json', '.log')):
                            full_path = os.path.join(root, f)
                            rel_path = os.path.relpath(full_path, session_dir)
                            file_list.append(rel_path)
            file_list.sort()
            
            # --- Progress Calculation ---
            top_level_files = os.listdir(session_dir)
            status = 'In Progress'
            progress = 10
            
            has_session_transcript = 'session_transcript.txt' in top_level_files
            has_recap = 'session_recap.txt' in top_level_files
            flac_count = sum(1 for f in top_level_files if f.endswith('.flac'))
            
            transcript_count = 0
            transcript_path = os.path.join(session_dir, 'transcripts')
            if os.path.exists(transcript_path):
                transcript_count = len([f for f in os.listdir(transcript_path) if f.endswith('_transcript.txt')])

            if has_recap:
                status = 'Complete'
                progress = 100
            elif has_session_transcript:
                status = 'Summarizing'
                progress = 90
            elif flac_count > 0:
                if transcript_count >= flac_count:
                    progress = 80
                else:
                    ratio = transcript_count / flac_count
                    progress = 20 + int(ratio * 60)
            elif 'info.txt' in top_level_files:
                progress = 10
            
            sessions.append({
                'name': item_name, 
                'status': status, 
                'progress': progress,
                'files': file_list
            })
            
    return sessions

@app.route('/api/action/<action_type>/<session_name>', methods=['POST'])
def session_action(action_type, session_name):
    if not is_user_logged_in():
        return jsonify({'error': 'Unauthorized'}), 401

    verify_csrf() 
    
    safe_session = secure_filename(session_name)
    session_dir = os.path.join(SAVE_PATH, safe_session)
    
    if not os.path.exists(session_dir):
        return jsonify({'error': 'Session not found'}), 404

    # Path to the completion marker
    complete_marker = os.path.join(session_dir, '.complete')

    try:
        # Common Logic: specific retry actions must always clear the ".complete" flag
        # so scribble.bash knows to re-enter the directory.
        if action_type in ['retry_whisper', 'retry_transcript', 'retry_llm']:
            if os.path.exists(complete_marker):
                os.remove(complete_marker)

        if action_type == 'retry_whisper':
            # 1. Backup Transcript Directory
            # This forces scribble.bash to see "transcripts" is missing and run whisperx again
            safe_backup(os.path.join(session_dir, 'transcripts'))
            
            # 2. Backup downstream files so they regenerate too
            safe_backup(os.path.join(session_dir, 'session_transcript.txt'))
            safe_backup(os.path.join(session_dir, 'session_recap.txt')) # Added generic name support
            
            return jsonify({'status': 'success', 'message': 'Transcripts backed up. Whisper will re-run.'})
                
        elif action_type == 'retry_transcript':
            # 1. Backup the combined transcript
            # This forces scribble.bash to rebuild it from the individual JSONs/TXTs
            safe_backup(os.path.join(session_dir, 'session_transcript.txt'))
            
            # 2. Backup the recap since it depends on the transcript
            safe_backup(os.path.join(session_dir, 'session_recap.txt'))
            
            return jsonify({'status': 'success', 'message': 'Session transcript backed up. It will be rebuilt.'})

        elif action_type == 'retry_llm':
            # 1. Backup the recap
            # This forces scribble.bash to resend the prompt + transcript to the LLM
            safe_backup(os.path.join(session_dir, 'session_recap.txt'))
            
            return jsonify({'status': 'success', 'message': 'Recap backed up. LLM generation will re-run.'})
            
        return jsonify({'error': 'Invalid action type'}), 400
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/status')
def status_page():
    if not is_user_logged_in():
        return redirect(url_for('login'))
    return render_template('status.html')
    
@app.route('/stats')
def stats_page():
    if not is_user_logged_in():
        return redirect(url_for('login'))
    
    conn = get_db_connection()

    # 1. Summary Cards (Totals)
    summary_query = """
        SELECT 'Gemini' as provider, SUM(cost) as total_cost, SUM(total_token_count) as total_tokens, COUNT(*) as request_count FROM gemini_logs
        UNION ALL
        SELECT 'OpenAI', SUM(cost), SUM(total_token_count), COUNT(*) FROM openai_logs
        UNION ALL
        SELECT 'Anthropic', SUM(cost), SUM(total_token_count), COUNT(*) FROM anthropic_logs
        UNION ALL
        SELECT 'Ollama', SUM(cost), SUM(total_token_count), COUNT(*) FROM ollama_logs
    """
    try:
        summary_rows = conn.execute(summary_query).fetchall()
    except sqlite3.OperationalError:
        summary_rows = []

    # 2. Recent Activity Log (The Missing Piece!)
    history_query = """
        SELECT * FROM (
            SELECT 'Gemini' as provider, model_name, request_timestamp, duration_seconds, total_token_count, cost, finish_reason FROM gemini_logs
            UNION ALL
            SELECT 'OpenAI', model_name, request_timestamp, duration_seconds, total_token_count, cost, finish_reason FROM openai_logs
            UNION ALL
            SELECT 'Anthropic', model_name, request_timestamp, duration_seconds, total_token_count, cost, finish_reason FROM anthropic_logs
            UNION ALL
            SELECT 'Ollama', model_name, request_timestamp, duration_seconds, total_token_count, cost, finish_reason FROM ollama_logs
        ) 
        ORDER BY request_timestamp DESC 
        LIMIT 50
    """
    try:
        recent_logs = conn.execute(history_query).fetchall()
    except sqlite3.OperationalError:
        recent_logs = []

    # 3. Time Series Data (RPM, TPM, RPD)
    minute_query = """
        SELECT provider, 
               strftime('%Y-%m-%d %H:%M', request_timestamp) as time_bucket, 
               COUNT(*) as req_count, 
               SUM(prompt_token_count) as input_tokens
        FROM (
            SELECT 'Gemini' as provider, request_timestamp, prompt_token_count FROM gemini_logs
            UNION ALL
            SELECT 'OpenAI' as provider, request_timestamp, prompt_token_count FROM openai_logs
            UNION ALL
            SELECT 'Anthropic' as provider, request_timestamp, prompt_token_count FROM anthropic_logs
            UNION ALL
            SELECT 'Ollama' as provider, request_timestamp, prompt_token_count FROM ollama_logs
        )
        WHERE request_timestamp >= datetime('now', '-60 minutes', 'localtime')
        GROUP BY provider, time_bucket
        ORDER BY time_bucket ASC
    """

    day_query = """
        SELECT provider, 
               strftime('%Y-%m-%d', request_timestamp) as time_bucket, 
               COUNT(*) as req_count
        FROM (
            SELECT 'Gemini' as provider, request_timestamp FROM gemini_logs
            UNION ALL
            SELECT 'OpenAI' as provider, request_timestamp FROM openai_logs
            UNION ALL
            SELECT 'Anthropic' as provider, request_timestamp FROM anthropic_logs
            UNION ALL
            SELECT 'Ollama' as provider, request_timestamp FROM ollama_logs
        )
        WHERE request_timestamp >= datetime('now', '-30 days', 'localtime')
        GROUP BY provider, time_bucket
        ORDER BY time_bucket ASC
    """

    try:
        minute_rows = conn.execute(minute_query).fetchall()
        day_rows = conn.execute(day_query).fetchall()
    except sqlite3.OperationalError:
        minute_rows = []
        day_rows = []

    conn.close()

    # 4. Structure Data for Chart.js
    def structure_chart_data(rows, value_key):
        data = {'labels': [], 'datasets': {}}
        all_times = sorted(list(set(r['time_bucket'] for r in rows)))
        data['labels'] = all_times
        
        providers = set(r['provider'] for r in rows)
        for p in providers:
            data['datasets'][p] = [0] * len(all_times)

        for r in rows:
            time_idx = all_times.index(r['time_bucket'])
            provider = r['provider']
            val = r[value_key] or 0
            data['datasets'][provider][time_idx] = val
        return data

    rpm_data = structure_chart_data(minute_rows, 'req_count')
    tpm_data = structure_chart_data(minute_rows, 'input_tokens')
    rpd_data = structure_chart_data(day_rows, 'req_count')

    grand_total_cost = sum(row['total_cost'] or 0.0 for row in summary_rows)
    grand_total_tokens = sum(row['total_tokens'] or 0 for row in summary_rows)

    return render_template('statistics.html', 
                           summary=summary_rows, 
                           recent_logs=recent_logs,  # <-- Added back
                           grand_total_cost=grand_total_cost,
                           grand_total_tokens=grand_total_tokens,
                           rpm_data=rpm_data,
                           tpm_data=tpm_data,
                           rpd_data=rpd_data)

@app.route('/prompt', methods=['GET'])
def edit_prompt():
    if not is_user_logged_in(): return redirect(url_for('login'))
    content = ""
    if os.path.exists(PROMPT_FILE_PATH):
        with open(PROMPT_FILE_PATH, 'r', encoding='utf-8') as f:
            content = f.read()
    return render_template('prompt_editor.html', prompt_content=content)

@app.route('/save_prompt', methods=['POST'])
def save_prompt():
    if not is_user_logged_in(): return redirect(url_for('login'))
    verify_csrf() # <--- SECURITY CHECK
    
    new_content = request.form.get('prompt_text', '').replace('\r\n', '\n')
    with open(PROMPT_FILE_PATH, 'w', encoding='utf-8') as f:
        f.write(new_content)
    return redirect(url_for('edit_prompt'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get('password') == APP_PASSWORD:
            session['logged_in'] = True
            # Generate new CSRF token on login
            session['csrf_token'] = secrets.token_hex(16)
            return redirect(url_for('upload_page'))
        else:
            flash("Invalid password.", 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear() # Clear everything, including the token
    return redirect(url_for('login'))