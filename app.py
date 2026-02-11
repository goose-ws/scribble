import logging
import os
import zipfile
import shutil
import pytz
import re
import requests
import time
import markdown
from datetime import datetime, timedelta
from functools import wraps
from werkzeug.utils import secure_filename
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session, send_file, Response
from sqlalchemy import func, case
from config import load_config, save_config
from database import init_db, db
from models import Campaign, Session, Job, Transcript, LLMLog, DiscordLog
from worker import JobManager
from io import BytesIO
from xhtml2pdf import pisa

# Configure Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__)
app_config = load_config()
app.secret_key = app_config.get('flask_secret_key', 'fallback_dev_key_if_config_fails')

APP_VERSION = '4.1.5'
@app.context_processor
def inject_version():
    return dict(app_version=APP_VERSION)

# Initialize Database
init_db(app)

@app.context_processor
def utility_processor():
    """Inject smart path check into templates."""
    # [CHANGE] Add filename argument with default None
    def folder_exists_check(path, filename=None):
        # 1. Happy path: The original folder exists
        if os.path.exists(path):
            return True
            
        # 2. Fallback: Check if it was archived to /data/archive
        try:
            archive_dir = '/data/archive'
            
            # Check A: Does a file match the directory ID? (Old logic)
            session_name = os.path.basename(path.rstrip('/'))
            if os.path.exists(os.path.join(archive_dir, session_name + ".flac.zip")): return True
            if os.path.exists(os.path.join(archive_dir, session_name + ".zip")): return True

            # [CHANGE] Check B: Does a file match the Original Filename?
            if filename:
                # Direct match
                if os.path.exists(os.path.join(archive_dir, filename)): return True
                
                # Suffix match (Handles date prefixes like "2025-12-19_craig...")
                if os.path.exists(archive_dir):
                    for f in os.listdir(archive_dir):
                        if f.endswith(filename):
                            return True
                
        except Exception:
            return False
            
        return False
    
    return dict(folder_exists_check=folder_exists_check)

# --- AUTH DECORATOR ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

@app.context_processor
def inject_config():
    return dict(
        config=load_config(),
        system_mode=os.environ.get('SCRIBBLE_MODE', 'standard')
    )

# --- Update Checker Logic ---
LATEST_VERSION_CACHE = None
LAST_CHECK_TIME = 0
CHECK_INTERVAL = 3600  # Check once per hour

def get_remote_version():
    """Fetches the APP_VERSION string from the main GitHub repo."""
    global LATEST_VERSION_CACHE, LAST_CHECK_TIME
    
    # Return cached version if valid
    if LATEST_VERSION_CACHE and (time.time() - LAST_CHECK_TIME < CHECK_INTERVAL):
        return LATEST_VERSION_CACHE

    try:
        url = 'https://raw.githubusercontent.com/goose-ws/scribble/refs/heads/main/app.py'
        # Set a short timeout so we don't hang the page if GitHub is slow
        resp = requests.get(url, timeout=3)
        
        if resp.status_code == 200:
            # Regex to find: APP_VERSION = 'x.x.x'
            match = re.search(r"APP_VERSION\s*=\s*['\"]([\d\.]+)['\"]", resp.text)
            if match:
                LATEST_VERSION_CACHE = match.group(1)
                LAST_CHECK_TIME = time.time()
                return LATEST_VERSION_CACHE
    except Exception:
        pass # Fail silently if offline or GitHub is down
    
    return None

@app.context_processor
def inject_update_status():
    """Makes 'update_available' and 'latest_version' variables available to all templates."""
    remote_ver = get_remote_version()
    is_update = False
    
    # Simple check: If remote exists and doesn't match local
    # (We assume 'APP_VERSION' is defined globally in your app.py)
    if remote_ver and remote_ver != APP_VERSION:
        is_update = True
        
    return dict(update_available=is_update, latest_version=remote_ver)

def parse_llm_stats(summary_text):
    """Extracts stats from the markdown header we generated."""
    stats = {}
    if not summary_text:
        return stats

    # Regex patterns matching your bash/python format
    patterns = {
        'provider': r'🤖 LLM Provider: `(.*?)`',
        'model': r'📋 Model: `(.*?)`',
        'api_time': r'⌚ API time: `(.*?)`',
        'tokens': r'🧾 Tokens: `(.*?)`'
    }

    for key, pattern in patterns.items():
        match = re.search(pattern, summary_text)
        if match:
            val = match.group(1)
            # [CHANGE] Add commas to token numbers for display
            if key == 'tokens':
                try:
                    # Finds sequences of digits and applies comma formatting
                    val = re.sub(r'\d+', lambda m: "{:,}".format(int(m.group(0))), val)
                except Exception:
                    pass
            stats[key] = val

    return stats

def parse_transcription_metrics(job_logs, transcripts):
    """
    Parses Job logs to find how long each file took and counts words.
    Returns: {'username': {'duration': '12s', 'words': 1234}}
    """
    metrics = {}
    if not job_logs:
        return metrics

    # Regex to capture timestamps from log lines
    # Format: [HH:MM:SS] Transcribing: file (User: username)
    start_pattern = r'\[(\d{2}:\d{2}:\d{2})\] Transcribing: .*? \(User: (.*?)\)'
    # Format: [HH:MM:SS] - Completed file...
    end_pattern = r'\[(\d{2}:\d{2}:\d{2})\] - Completed (.*?)[:\.]'

    # Store starts temporarily
    starts = {}

    for line in job_logs.split('\n'):
        # Check Start
        start_match = re.search(start_pattern, line)
        if start_match:
            time_str, username = start_match.groups()
            starts[username] = datetime.strptime(time_str, '%H:%M:%S')
            if username not in metrics:
                metrics[username] = {'duration': '?', 'words': 0}
            continue

        # Check End
        # Note: The log doesn't list username on completion line, but we process sequentially.
        # We assume the last started user is the one finishing.
        end_match = re.search(end_pattern, line)
        if end_match and starts:
            # Get the most recently started user (simple stack logic)
            # Since your worker is sequential, the last added key in 'starts' is usually the active one
            # But let's rely on the filename matching if possible.
            # Simpler approach for sequential logs:
            current_user = list(starts.keys())[-1] # Get last key

            time_str = end_match.group(1)
            end_time = datetime.strptime(time_str, '%H:%M:%S')
            start_time = starts[current_user]

            # Handle day rollover if needed, though unlikely for single file
            delta = end_time - start_time
            metrics[current_user]['duration'] = str(delta)

            # Cleanup
            del starts[current_user]

    # Calculate Word Counts from actual content
    for username, content in transcripts.items():
        if username not in metrics:
            metrics[username] = {'duration': 'N/A'}
        metrics[username]['words'] = len(content.split())

    return metrics

def parse_integrations_status(job_logs):
    """Checks logs for Discord and Custom Script success messages."""
    status = {
        'discord_sent': False,
        'scripts': []
    }
    if not job_logs:
        return status

    if "Sending to Discord... Sent." in job_logs:
        status['discord_sent'] = True

    # Script logs: "Finished: script_name (Success)" or "Failed: script_name"
    script_pattern = r'(Finished|Failed): (.*?) \((.*?)\)'
    for line in job_logs.split('\n'):
        match = re.search(script_pattern, line)
        if match:
            state, name, outcome = match.groups()
            status['scripts'].append({
                'name': name,
                'success': state == 'Finished',
                'detail': outcome
            })

    return status

# --- LOGIN ROUTES ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        password = request.form.get('password')
        config = load_config()
        if password == config.get('webui_password'):
            session['logged_in'] = True
            flash('Logged in successfully.', 'success')
            next_page = request.args.get('next')
            return redirect(next_page or url_for('dashboard'))
        else:
            flash('Invalid password.', 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    flash('Logged out.', 'info')
    return redirect(url_for('login'))

# --- PROTECTED ROUTES ---

@app.route('/')
@login_required
def dashboard():
    recent_sessions = Session.query.order_by(Session.created_at.desc()).limit(10).all()
    return render_template('dashboard.html', sessions=recent_sessions)

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    config = load_config()
    if request.method == 'POST':
        # Update config dictionary from form data
        for key, value in request.form.items():
            if key in config:
                if isinstance(config[key], bool):
                    config[key] = True if value == 'on' else False
                elif isinstance(config[key], int):
                    try: config[key] = int(value)
                    except: pass
                elif isinstance(config[key], float):
                    try: config[key] = float(value)
                    except: pass
                else:
                    config[key] = value

        config['archive_zip'] = 'archive_zip' in request.form
        config['db_space_saver'] = 'db_space_saver' in request.form

        save_config(config)
        flash('Settings saved successfully!', 'success')
        return redirect(url_for('settings'))

    if request.method == 'POST':
        name = request.form.get('name')
        if not name:
            flash('Campaign Name is required.', 'error')
            return redirect(url_for('campaigns'))

        # Handle Script Selection
        selected_scripts = request.form.getlist('scripts')
        script_paths_str = ",".join(selected_scripts)
        
        # [NEW] Handle Default Logic
        is_default = 'is_default' in request.form
        if is_default:
            # Unset default for all existing campaigns
            Campaign.query.update({Campaign.is_default: False})

        new_campaign = Campaign(
            name=name,
            discord_webhook=request.form.get('discord_webhook'),
            system_prompt=request.form.get('system_prompt'),
            script_paths=script_paths_str,
            is_default=is_default # <--- Save status
        )
        try:
            db.session.add(new_campaign)
            db.session.commit()
            flash(f'Campaign "{name}" created!', 'success')
        except Exception as e:
            db.session.rollback()
            flash(f'Error creating campaign: {e}', 'error')

        return redirect(url_for('campaigns'))

    return render_template('settings.html', config=config)

@app.route('/campaigns', methods=['GET', 'POST'])
@login_required
def campaigns():
    # Get list of available scripts
    scripts_dir = '/data/scripts'
    if not os.path.exists(scripts_dir):
        os.makedirs(scripts_dir)

    # List only files, not directories
    available_scripts = [f for f in os.listdir(scripts_dir)
                        if os.path.isfile(os.path.join(scripts_dir, f))]

    if request.method == 'POST':
        name = request.form.get('name')
        if not name:
            flash('Campaign Name is required.', 'error')
            return redirect(url_for('campaigns'))

        # Handle Script Selection (Checkbox list)
        selected_scripts = request.form.getlist('scripts')
        # Join into a comma-separated string for storage
        script_paths_str = ",".join(selected_scripts)

        new_campaign = Campaign(
            name=name,
            discord_webhook=request.form.get('discord_webhook'),
            system_prompt=request.form.get('system_prompt'),
            script_paths=script_paths_str
        )
        try:
            db.session.add(new_campaign)
            db.session.commit()
            flash(f'Campaign "{name}" created!', 'success')
        except Exception as e:
            db.session.rollback()
            flash(f'Error creating campaign: {e}', 'error')

        return redirect(url_for('campaigns'))

    all_campaigns = Campaign.query.all()
    return render_template('campaigns.html',
                         campaigns=all_campaigns,
                         available_scripts=available_scripts)

@app.route('/campaigns/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_campaign(id):
    campaign = Campaign.query.get_or_404(id)

    # Get list of available scripts
    scripts_dir = '/data/scripts'
    if not os.path.exists(scripts_dir):
        os.makedirs(scripts_dir)

    available_scripts = [f for f in os.listdir(scripts_dir)
                        if os.path.isfile(os.path.join(scripts_dir, f))]

    current_scripts = campaign.script_paths.split(',') if campaign.script_paths else []

    if request.method == 'POST':
        campaign.name = request.form.get('name')
        campaign.discord_webhook = request.form.get('discord_webhook')
        campaign.system_prompt = request.form.get('system_prompt')

        # Handle Script Selection
        selected_scripts = request.form.getlist('scripts')
        campaign.script_paths = ",".join(selected_scripts)
        
        # [FIXED] Robust Default Logic
        should_be_default = 'is_default' in request.form
        
        if should_be_default:
            # 1. Clear 'is_default' on ALL OTHER campaigns (exclude current ID)
            # This prevents the bulk update from interfering with our current object
            Campaign.query.filter(Campaign.id != campaign.id).update({Campaign.is_default: False})
            
            # 2. Set this campaign to True
            campaign.is_default = True
        else:
            # If unchecked, just set to False
            campaign.is_default = False

        try:
            db.session.commit()
            flash(f'Campaign "{campaign.name}" updated!', 'success')
            return redirect(url_for('campaigns'))
        except Exception as e:
            db.session.rollback()
            flash(f'Error updating campaign: {e}', 'error')

    return render_template('edit_campaign.html',
                         campaign=campaign,
                         available_scripts=available_scripts,
                         current_scripts=current_scripts)

# Ensure this route exists as well for the "Star" icon in the list view
@app.route('/campaigns/set_default/<int:campaign_id>')
@login_required
def set_default_campaign(campaign_id):
    # 1. Unset all
    Campaign.query.update({Campaign.is_default: False})
    
    # 2. Set new default
    camp = Campaign.query.get_or_404(campaign_id)
    camp.is_default = True
    
    db.session.commit()
    flash(f'"{camp.name}" is now the default campaign.', 'success')
    return redirect(url_for('campaigns'))

@app.route('/campaigns/delete/<int:id>')
@login_required
def delete_campaign(id):
    campaign = Campaign.query.get_or_404(id)
    try:
        db.session.delete(campaign)
        db.session.commit()
        flash(f'Campaign "{campaign.name}" deleted.', 'info')
    except Exception as e:
        flash(f'Error deleting campaign: {e}', 'error')
    return redirect(url_for('campaigns'))

# --- UPLOAD & PROCESSING LOGIC ---

def parse_session_date(info_path):
    utc_date = datetime.utcnow()
    try:
        with open(info_path, 'r') as f:
            for line in f:
                if "Start time:" in line:
                    time_str = line.split("Start time:", 1)[1].strip()
                    time_str = time_str.replace('Z', '+00:00')
                    utc_date = datetime.fromisoformat(time_str)
                    break
    except Exception as e:
        logging.error(f"Error parsing info.txt: {e}")
        return utc_date, "Unknown Date"

    target_tz = os.environ.get('TZ', 'UTC')
    try:
        local_tz = pytz.timezone(target_tz)
        local_date = utc_date.astimezone(local_tz)
        return utc_date, local_date.strftime('%Y-%m-%d %H:%M:%S')
    except Exception as e:
        logging.error(f"Timezone conversion error: {e}")
        return utc_date, str(utc_date)

# --- CAMPAIGN ROUTES ---

@app.route('/campaigns/<int:campaign_id>')
@login_required
def campaign_detail(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    sessions = Session.query.filter_by(campaign_id=campaign.id).order_by(Session.session_number.desc(), Session.created_at.desc()).all()
    
    total_words = 0
    total_in = 0
    total_out = 0
    total_combined = 0
    total_cost = 0.0
    discord_count = 0
    discord_errors = 0
    
    # Get all logs linked to these sessions
    session_ids = [s.id for s in sessions]
    if session_ids:
        logs = DiscordLog.query.filter(DiscordLog.session_id.in_(session_ids)).all()
        discord_count = len(logs)
        # Count non-success statuses (200, 201, 204 are good)
        discord_errors = sum(1 for log in logs if log.http_status not in [200, 201, 204])
    
    for s in sessions:
        # Words (Approximation from transcript)
        if s.transcript_text:
            total_words += len(s.transcript_text.split())
            
        # Tokens
        # Format usually: "54,452 in | 2,188 out | 56,640 total"
        stats = parse_llm_stats(s.summary_text)
        token_str = stats.get('tokens', '')
        
        if token_str:
            # 1. Try to parse full breakdown
            match = re.search(r'([\d,]+)\s+in\s*\|\s*([\d,]+)\s+out\s*\|\s*([\d,]+)\s+total', token_str)
            if match:
                try:
                    i = int(match.group(1).replace(',', ''))
                    o = int(match.group(2).replace(',', ''))
                    t = int(match.group(3).replace(',', ''))
                    
                    total_in += i
                    total_out += o
                    total_combined += t
                except: pass
            else:
                # 2. Fallback: Try to find just "X total"
                match_total = re.search(r'([\d,]+)\s+total', token_str)
                if match_total:
                    try:
                        t = int(match_total.group(1).replace(',', ''))
                        total_combined += t
                    except: pass
                else:
                    # 3. Fallback: Try raw number
                    try:
                        t = int(token_str.replace(',', ''))
                        total_combined += t
                    except: pass

    return render_template('campaign_detail.html', 
                           campaign=campaign, 
                           sessions=sessions,
                           total_words="{:,}".format(total_words),
                           total_in="{:,}".format(total_in),
                           total_out="{:,}".format(total_out),
                           total_combined="{:,}".format(total_combined),
                           total_cost="{:,.4f}".format(total_cost),
                           discord_count=discord_count,
                           discord_errors=discord_errors)

# In app.py

@app.route('/campaigns/<int:campaign_id>/download_pdf/<doc_type>')
@login_required
def download_campaign_pdf(campaign_id, doc_type):
    campaign = Campaign.query.get_or_404(campaign_id)
    sessions = Session.query.filter_by(campaign_id=campaign.id).order_by(Session.session_number.asc()).all()
    
    # CSS for the PDF
    html_content = f"""
    <html>
    <head>
        <style>
            @page {{ size: A4; margin: 2cm; }}
            body {{ font-family: Helvetica, sans-serif; font-size: 10pt; line-height: 1.4; }}
            h1 {{ color: #2c3e50; text-align: center; font-size: 24pt; margin-bottom: 20px; }}
            h2 {{ color: #2980b9; border-bottom: 1px solid #eee; padding-bottom: 10px; margin-top: 30px; page-break-after: avoid; }}
            .meta {{ color: #95a5a6; font-size: 9pt; font-style: italic; margin-bottom: 10px; }}
            .toc-entry {{ margin-bottom: 5px; font-size: 11pt; }}
            .toc-entry a {{ text-decoration: none; color: #2c3e50; }}
            .page-break {{ page-break-before: always; }}
            
            /* Transcript specific styles */
            .dialogue-line {{ margin-bottom: 8px; text-align: left; }}
            .speaker {{ font-weight: bold; color: #444; }}
            
            /* Recap specific styles */
            .recap-content {{ text-align: justify; }}
        </style>
    </head>
    <body>
    """
    
    # Title Page
    title_text = "Campaign Recap" if doc_type == 'recap' else "Campaign Transcripts"
    html_content += f"""
        <div style="text-align: center; margin-top: 200px;">
            <h1>{campaign.name}</h1>
            <h2>{title_text}</h2>
            <p>Generated on {datetime.now().strftime('%Y-%m-%d')}</p>
        </div>
        <div class="page-break"></div>
    """
    
    # Table of Contents
    html_content += "<h1>Table of Contents</h1>"
    for s in sessions:
        if doc_type == 'recap' and not s.summary_text: continue
        if doc_type == 'transcript' and not s.transcript_text: continue
        
        entry_title = f"Session {s.session_number}: {s.session_date.strftime('%Y-%m-%d')}"
        html_content += f"""
        <div class='toc-entry'>
            <a href='#session_{s.id}'>{entry_title}</a>
        </div>
        """
        
    html_content += "<div class='page-break'></div>"
    
    # Content Loop
    for s in sessions:
        date_str = s.session_date.strftime('%B %d, %Y')
        anchor = f'<a name="session_{s.id}"></a>' 
        
        if doc_type == 'recap':
            if not s.summary_text: continue
            
            # Markdown processing
            clean_summary = ""
            lines = s.summary_text.split('\n')
            header_ended = False
            content_lines = []
            for line in lines:
                if '##' in line and not header_ended: header_ended = True
                if header_ended: content_lines.append(line)
                elif not any(c in line for c in ['🤖', '📋', '⌚', '🧾']):
                     content_lines.append(line)
            
            md_text = "\n".join(content_lines)
            body_html = markdown.markdown(md_text)
            
            html_content += f"""
                {anchor}
                <h2>Session {s.session_number}</h2>
                <div class="meta">{date_str}</div>
                <div class="recap-content">{body_html}</div>
                <div class="page-break"></div>
            """
            
        elif doc_type == 'transcript':
            if not s.transcript_text: continue
            
            html_content += f"""
                {anchor}
                <h2>Session {s.session_number}</h2>
                <div class="meta">{date_str}</div>
                <div class="transcript-content">
            """
            
            raw_lines = s.transcript_text.split('\n')
            
            for line in raw_lines:
                line = line.strip()
                if not line: continue
                
                safe_line = line.replace('<', '&lt;').replace('>', '&gt;')
                
                # FIXED LOGIC:
                # 1. Find the closing bracket of the timestamp "]"
                # 2. Find the first colon ":" AFTER that bracket
                bracket_idx = safe_line.find(']')
                sep_idx = safe_line.find(':', bracket_idx) if bracket_idx != -1 else -1

                if bracket_idx != -1 and sep_idx != -1:
                    # We found a timestamp and a speaker separator
                    # safe_line[:sep_idx+1] includes the colon -> "[00:00:00] Speaker:"
                    formatted_line = f"<span class='speaker'>{safe_line[:sep_idx+1]}</span>{safe_line[sep_idx+1:]}"
                else:
                    # No timestamp/speaker structure found, print as is
                    formatted_line = safe_line

                html_content += f"<div class='dialogue-line'>{formatted_line}</div>"
            
            html_content += "</div><div class='page-break'></div>"

    html_content += "</body></html>"
    
    # Create PDF
    pdf_output = BytesIO()
    pisa_status = pisa.CreatePDF(html_content, dest=pdf_output)
    
    if pisa_status.err:
        return f"Error generating PDF: {pisa_status.err}", 500
        
    pdf_output.seek(0)
    filename = f"{campaign.name.replace(' ', '_')}_{doc_type}.pdf"
    
    return Response(
        pdf_output,
        mimetype='application/pdf',
        headers={"Content-disposition": f"attachment; filename={filename}"}
    )

@app.route('/upload', methods=['GET', 'POST'])
@login_required
def upload():
    if request.method == 'GET':
        campaigns = Campaign.query.all()
        
        # Prepare Data for Frontend
        default_campaign_id = None
        next_numbers = {}

        for c in campaigns:
            if c.is_default:
                default_campaign_id = c.id
            
            # Calculate next number for this campaign
            max_sess = db.session.query(func.max(Session.session_number)).filter_by(campaign_id=c.id).scalar()
            next_numbers[c.id] = 0 if max_sess is None else max_sess + 1

        return render_template('upload.html', 
                               campaigns=campaigns, 
                               default_campaign_id=default_campaign_id,
                               next_numbers=next_numbers)

    # --- POST (Upload Handling) ---
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400

    file = request.files['file']
    campaign_id = request.form.get('campaign_id')
    # [NEW] Get the manual session number
    manual_session_number = request.form.get('session_number')

    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400

    if not campaign_id:
        return jsonify({'error': 'No campaign selected'}), 400

    if file:
        filename = secure_filename(file.filename)
        upload_id = datetime.now().strftime('%Y%m%d_%H%M%S')
        upload_dir = os.path.join('/data/input', upload_id)
        os.makedirs(upload_dir, exist_ok=True)

        zip_path = os.path.join(upload_dir, filename)

        try:
            file.save(zip_path)
            
            # ... [Keep your existing ZIP validation code here] ...
            if not zipfile.is_zipfile(zip_path): raise Exception("Not a zip.")
            with zipfile.ZipFile(zip_path, 'r') as z: z.extractall(upload_dir)
            # ... [End validation] ...

            # Parse Date
            info_path = os.path.join(upload_dir, 'info.txt')
            session_date_utc, local_time_str = parse_session_date(info_path)

            # [NEW] Determine Session Number
            if manual_session_number:
                final_session_num = int(manual_session_number)
            else:
                # Fallback calculation
                max_sess = db.session.query(func.max(Session.session_number)).filter_by(campaign_id=campaign_id).scalar()
                final_session_num = 0 if max_sess is None else max_sess + 1

            # Create DB Records
            new_session = Session(
                campaign_id=campaign_id,
                session_number=final_session_num, # Use the determined number
                session_date=session_date_utc,
                local_time_str=local_time_str,
                original_filename=filename,
                directory_path=upload_dir,
                status="Processing"
            )
            
            # ... [Keep existing Job creation and return] ...
            db.session.add(new_session)
            db.session.commit()
            
            initial_job = Job(session_id=new_session.id, step="transcribe", status="pending", logs="Job queued.")
            db.session.add(initial_job)
            db.session.commit()

            return jsonify({'success': True, 'redirect': url_for('dashboard')})

        except Exception as e:
            logging.error(f"Upload failed: {e}")
            if os.path.exists(upload_dir): shutil.rmtree(upload_dir)
            return jsonify({'error': str(e)}), 500

@app.route('/session/<int:session_id>/update_number', methods=['POST'])
@login_required
def update_session_number(session_id):
    session_obj = Session.query.get_or_404(session_id)
    try:
        new_num = int(request.form.get('session_number'))
        session_obj.session_number = new_num
        db.session.commit()
        flash(f"Session number updated to {new_num}", "success")
    except ValueError:
        flash("Invalid number provided.", "error")
    
    return redirect(url_for('session_detail', session_id=session_id))

@app.route('/api/metrics')
@login_required
def api_metrics():
    period = request.args.get('period', '7d')
    now = datetime.utcnow()

    # Calculate Date Range
    if period == '24h':
        start_date = now - timedelta(hours=24)
    elif period == '7d':
        start_date = now - timedelta(days=7)
    elif period == '30d':
        start_date = now - timedelta(days=30)
    elif period == '365d':
        start_date = now - timedelta(days=365)
    elif period == 'all':
        start_date = datetime(1970, 1, 1)
    else:
        start_date = now - timedelta(days=7) # Default

    # 1. General Counts
    # Campaigns are static, not usually time-bound, but we show total
    total_campaigns = Campaign.query.count()

    # Recaps Generated (Sessions with summary_text in range)
    # Using Session.created_at for the time filter
    recaps_count = Session.query.filter(
        Session.summary_text != None,
        Session.created_at >= start_date
    ).count()

    # 2. LLM Metrics (Aggregated from LLMLog)
    # Filter logs by range
    logs_query = LLMLog.query.filter(LLMLog.request_timestamp >= start_date)

    # Aggregates
    stats = logs_query.with_entities(
        func.count(LLMLog.id).label('total_calls'),
        func.sum(LLMLog.total_tokens).label('total_tokens'),
        func.sum(LLMLog.cost).label('total_cost'),
        func.avg(LLMLog.duration_seconds).label('avg_latency')
    ).first()

    # 3. Breakdowns (for Charts)

    # By Provider
    provider_stats = logs_query.with_entities(
        LLMLog.provider,
        func.count(LLMLog.id).label('count'),
        func.sum(LLMLog.cost).label('cost')
    ).group_by(LLMLog.provider).all()

    # By Model
    model_stats = logs_query.with_entities(
        LLMLog.model_name,
        func.count(LLMLog.id).label('count'),
        func.avg(LLMLog.duration_seconds).label('avg_latency')
    ).group_by(LLMLog.model_name).all()

    # Format Data for JSON
    return jsonify({
        'campaigns': total_campaigns,
        'recaps': recaps_count,
        'calls': stats.total_calls or 0,
        'tokens': stats.total_tokens or 0,
        'cost': round(stats.total_cost or 0.0, 4),
        'avg_latency': round(stats.avg_latency or 0.0, 2),
        'providers': [{'name': p[0], 'count': p[1], 'cost': round(p[2] or 0, 4)} for p in provider_stats],
        'models': [{'name': m[0], 'count': m[1], 'latency': round(m[2] or 0, 2)} for m in model_stats]
    })

@app.route('/session/<int:session_id>')
@login_required
def session_detail(session_id):
    session_obj = Session.query.get_or_404(session_id)
    jobs = Job.query.filter_by(session_id=session_obj.id).order_by(Job.created_at.asc()).all()

    job_durations = {}
    for job in jobs:
        # 1. Completed/Error: Diff between Start and Finish
        if job.status in ['completed', 'error'] and job.updated_at and job.created_at:
            delta = job.updated_at - job.created_at
        # 2. Processing: Diff between Start and NOW
        elif job.status in ['processing', 'cancelling'] and job.created_at:
            delta = datetime.utcnow() - job.created_at
        # 3. Pending: Zero
        else:
            delta = timedelta(seconds=0)
            
        total_seconds = int(delta.total_seconds())
        m, s = divmod(total_seconds, 60)
        h, m = divmod(m, 60)
        job_durations[job.id] = "{:02d}:{:02d}:{:02d}".format(h, m, s)

    # 1. Fetch Transcripts
    transcript_text = session_obj.transcript_text
    if not transcript_text:
        transcript_path = os.path.join(session_obj.directory_path, "session_transcript.txt")
        if os.path.exists(transcript_path):
            with open(transcript_path, 'r', encoding='utf-8', errors='replace') as f:
                transcript_text = f.read()

    user_transcripts = {t.username: t.content for t in session_obj.transcripts}

    # --- METRICS CALCULATIONS ---
    llm_stats = parse_llm_stats(session_obj.summary_text)
    if 'tokens' not in llm_stats: llm_stats['tokens'] = None

    # Find the main transcription job to parse per-user audio durations
    # We prefer the latest full 'transcribe' job if multiple exist
    transcribe_jobs = [j for j in jobs if j.step == 'transcribe']
    transcribe_job = transcribe_jobs[-1] if transcribe_jobs else None
    
    user_metrics = {}
    if transcribe_job:
        user_metrics = parse_transcription_metrics(transcribe_job.logs, user_transcripts)

    summarize_job = next((j for j in jobs if j.step == 'summarize'), None)
    integrations = {'discord_sent': False, 'scripts': []}
    if summarize_job:
        integrations = parse_integrations_status(summarize_job.logs)

    # [CHANGED] Calculate Total Audio Duration (Sum of users)
    # Old logic calculated wall-clock time from session creation to last job (Session Lifespan)
    total_seconds = 0.0
    for stats in user_metrics.values():
        d_str = stats.get('duration', '0:00:00')
        try:
            # Parse "H:M:S" (e.g., "0:12:01" or "3:03:41")
            parts = d_str.split(':')
            if len(parts) == 3:
                h, m, s = map(float, parts)
                total_seconds += h*3600 + m*60 + s
        except:
            continue
            
    m, s = divmod(int(total_seconds), 60)
    h, m = divmod(m, 60)
    total_duration = "{:02d}:{:02d}:{:02d}".format(h, m, s)

    total_words = 0
    if transcript_text:
        total_words = len(transcript_text.split())
        total_words = "{:,}".format(total_words)

    return render_template('session_detail.html',
                         recording_session=session_obj,
                         jobs=jobs,
                         job_durations=job_durations,
                         transcript=transcript_text,
                         user_transcripts=user_transcripts,
                         llm_stats=llm_stats,
                         user_metrics=user_metrics,
                         integrations=integrations,
                         total_duration=total_duration,
                         total_words=total_words)

@app.route('/session/<int:session_id>/save_user_transcript', methods=['POST'])
@login_required
def save_user_transcript(session_id):
    session_obj = Session.query.get_or_404(session_id)
    username = request.form.get('username')
    new_content = request.form.get('content')
    
    transcript = Transcript.query.filter_by(session_id=session_id, username=username).first()
    if transcript:
        transcript.content = new_content
        db.session.commit()
        
        # Update disk file
        try:
            user_path = os.path.join(session_obj.directory_path, "transcripts", f"{username}_transcript.txt")
            with open(user_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
        except Exception as e:
            logging.error(f"Failed to save user transcript to disk: {e}")
            
        flash(f'Transcript for {username} updated.', 'success')
    else:
        flash('User transcript not found.', 'error')
        
    return redirect(url_for('session_detail', session_id=session_id))

@app.route('/session/<int:session_id>/save_master_transcript', methods=['POST'])
@login_required
def save_master_transcript(session_id):
    session_obj = Session.query.get_or_404(session_id)
    new_content = request.form.get('content')
    
    session_obj.transcript_text = new_content
    db.session.commit()
    
    # Update disk file
    try:
        path = os.path.join(session_obj.directory_path, "session_transcript.txt")
        with open(path, 'w', encoding='utf-8') as f:
            f.write(new_content)
    except Exception as e:
        logging.error(f"Failed to save master transcript to disk: {e}")

    flash('Master transcript updated.', 'success')
    return redirect(url_for('session_detail', session_id=session_id))

@app.route('/session/<int:session_id>/save_recap', methods=['POST'])
@login_required
def save_recap(session_id):
    session_obj = Session.query.get_or_404(session_id)
    new_content = request.form.get('content')
    
    session_obj.summary_text = new_content
    db.session.commit()
    
    # Update disk file
    try:
        path = os.path.join(session_obj.directory_path, "session_recap.txt")
        with open(path, 'w', encoding='utf-8') as f:
            f.write(new_content)
    except Exception as e:
        logging.error(f"Failed to save recap to disk: {e}")

    flash('Recap updated.', 'success')
    return redirect(url_for('session_detail', session_id=session_id))

@app.route('/session/<int:session_id>/download/<file_type>')
@login_required
def download_file(session_id, file_type):
    session_obj = Session.query.get_or_404(session_id)
    
    if file_type == 'recap':
        content = session_obj.summary_text
        filename = f"Recap_{session_obj.original_filename}.md"
        mimetype = "text/markdown"
    elif file_type == 'transcript':
        content = session_obj.transcript_text
        filename = f"Transcript_{session_obj.original_filename}.txt"
        mimetype = "text/plain"
    elif file_type.startswith('user_'):
        username = file_type.split('user_', 1)[1]
        t = Transcript.query.filter_by(session_id=session_id, username=username).first()
        if t:
            content = t.content
            filename = f"{username}_{session_obj.original_filename}.txt"
            mimetype = "text/plain"
        else:
            return "User transcript not found", 404
    else:
        return "Invalid file type", 400
        
    return Response(
        content,
        mimetype=mimetype,
        headers={"Content-disposition": f"attachment; filename={filename}"}
    )

@app.route('/session/<int:session_id>/action/<action_type>')
@login_required
def session_action(session_id, action_type):
    session_obj = Session.query.get_or_404(session_id)
    
    # --- Helper: Smart Restore ---
    def ensure_files_exist(target_user=None):
        logging.info(f"Checking files for Session {session_id} (User: {target_user})...")
        
        # 1. Check if FLACs already exist on disk
        if os.path.exists(session_obj.directory_path):
            flacs = [f for f in os.listdir(session_obj.directory_path) if f.endswith('.flac')]
            
            # If we need a specific user, check if THEIR file is there
            if target_user:
                if any(target_user in f for f in flacs):
                    logging.info("Target user file found on disk.")
                    return True
            # If we need the full session, check if ANY file is there (imperfect, but fast)
            elif flacs:
                logging.info("Session files found on disk.")
                return True
        
        # 2. Locate Archive
        archive_dir = '/data/archive'
        archive_path = os.path.join(archive_dir, session_obj.original_filename)
        
        # Handle the timestamp-prefixed filenames (e.g., 2026-02-10_filename.zip)
        if not os.path.exists(archive_path):
             for f in os.listdir(archive_dir):
                if f.endswith(session_obj.original_filename):
                    archive_path = os.path.join(archive_dir, f)
                    logging.info(f"Found archive match: {archive_path}")
                    break
        
        if os.path.exists(archive_path):
            try:
                os.makedirs(session_obj.directory_path, exist_ok=True)
                with zipfile.ZipFile(archive_path, 'r') as zip_ref:
                    # Smart Extract: Only extract what is needed
                    if target_user:
                        # Scan zip for the file matching this user
                        found_in_zip = False
                        for name in zip_ref.namelist():
                            if target_user in name and name.endswith('.flac'):
                                logging.info(f"Extracting single file: {name}")
                                zip_ref.extract(name, session_obj.directory_path)
                                found_in_zip = True
                                break
                        if not found_in_zip:
                            logging.warning(f"User {target_user} not found in zip. Extracting all.")
                            zip_ref.extractall(session_obj.directory_path)
                    else:
                        logging.info("Extracting full session.")
                        zip_ref.extractall(session_obj.directory_path)
                return True
            except Exception as e:
                logging.error(f"Archive restoration failed: {e}")
                flash(f"Error restoring from archive: {e}", "danger")
                return False
        
        logging.error(f"Archive not found for: {session_obj.original_filename}")
        return False

    # --- Action Logic ---

    # 1. Force Re-Transcribe (All or Single)
    if action_type == 'retranscribe' or action_type.startswith('retranscribe_user_'):
        
        target_user = None
        if action_type.startswith('retranscribe_user_'):
            target_user = action_type.split('retranscribe_user_', 1)[1]
            
        if not ensure_files_exist(target_user):
            flash('Error: Source files not found in /data/input or /data/archive.', 'danger')
            return redirect(url_for('session_detail', session_id=session_obj.id))
            
        if target_user:
            step_name = f"transcribe:{target_user}"
            flash(f'Re-transcription queued for user: {target_user}', 'success')
        else:
            step_name = "transcribe"
            flash('Full session re-transcription queued.', 'success')

        new_job = Job(session_id=session_obj.id, step=step_name, status='pending', logs="Queued by user...")
        db.session.add(new_job)
        session_obj.status = "Processing"
        db.session.commit()

    # 2. Rebuild Transcript (Merge Only)
    elif action_type == 'rebuild_transcript':
        try:
            transcripts = Transcript.query.filter_by(session_id=session_obj.id).all()
            all_lines = []
            ts_pattern = re.compile(r'^\[(\d{2}:\d{2}:\d{2})\]')
            
            for t in transcripts:
                if not t.content: continue
                for line in t.content.split('\n'):
                    match = ts_pattern.match(line)
                    if match:
                        ts_str = match.group(1)
                        h, m, s = map(int, ts_str.split(':'))
                        seconds = h*3600 + m*60 + s
                        all_lines.append((seconds, line))
                    else:
                        all_lines.append((999999, line))
            
            all_lines.sort(key=lambda x: x[0])
            final_text = "\n".join([x[1] for x in all_lines])
            
            session_obj.transcript_text = final_text
            db.session.commit()
            
            path = os.path.join(session_obj.directory_path, "session_transcript.txt")
            with open(path, 'w', encoding='utf-8') as f:
                f.write(final_text)
                
            flash('Master transcript rebuilt from user transcripts.', 'success')
            
        except Exception as e:
            flash(f'Rebuild failed: {str(e)}', 'danger')

    # 3. Rerun Scripts
    elif action_type == 'rerun_scripts':
        new_job = Job(session_id=session_obj.id, step='run_scripts', status='pending')
        new_job.logs = "Queued for manual script execution..."
        db.session.add(new_job)
        db.session.commit()
        flash('Scripts queued for execution.', 'success')

    # 4. Re-Generate Summary (LLM Only)
    elif action_type == 'regenerate_summary':
        transcript_path = os.path.join(session_obj.directory_path, "session_transcript.txt")
        if not os.path.exists(transcript_path) and not session_obj.transcript_text:
             flash('Error: No transcript found.', 'danger')
             return redirect(url_for('session_detail', session_id=session_obj.id))

        new_job = Job(session_id=session_obj.id, step='summarize_only', status='pending')
        new_job.logs = "Queued for Summary Re-generation"
        db.session.add(new_job)
        session_obj.status = "Processing"
        db.session.commit()
        flash('Summary re-generation queued.', 'success')

    # 5. Post to Discord (Discord Only)
    elif action_type == 'post_discord':
        if not session_obj.summary_text:
             flash('Error: No summary available to post.', 'danger')
             return redirect(url_for('session_detail', session_id=session_obj.id))
             
        new_job = Job(session_id=session_obj.id, step='post_discord', status='pending')
        new_job.logs = "Queued for Discord Posting..."
        db.session.add(new_job)
        session_obj.status = "Processing"
        db.session.commit()
        flash('Discord post queued.', 'success')

    return redirect(url_for('session_detail', session_id=session_obj.id))

# In session_status_api
@app.route('/session/<int:session_id>/status')
@login_required
def session_status_api(session_id):
    session_obj = Session.query.get_or_404(session_id)
    jobs = Job.query.filter_by(session_id=session_obj.id).order_by(Job.created_at.asc()).all()

    transcript_text = session_obj.transcript_text or ""

    user_transcripts = {}
    for t in session_obj.transcripts:
        user_transcripts[t.username] = t.content

    jobs_data = []
    for job in jobs:
        jobs_data.append({
            'id': job.id,
            'step': job.step,
            'status': job.status,
            'logs': job.logs,
            'updated_at': job.updated_at.strftime('%H:%M:%S')
        })

    return jsonify({
        'session_status': session_obj.status,
        'transcript': transcript_text,
        'transcript_ready': bool(transcript_text),
        'summary': session_obj.summary_text,
        'user_transcripts': user_transcripts,
        'jobs': jobs_data
    })

@app.route('/session/<int:session_id>/delete', methods=['POST'])
@login_required
def delete_session(session_id):
    session_obj = Session.query.get_or_404(session_id)

    # 1. Cleanup Disk
    try:
        if os.path.exists(session_obj.directory_path):
            shutil.rmtree(session_obj.directory_path)
    except Exception as e:
        logging.error(f"Error deleting directory for session {session_id}: {e}")

    # 2. Cleanup Database
    try:
        db.session.delete(session_obj)
        db.session.commit()
        flash(f'Session "{session_obj.original_filename}" deleted.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error deleting session from DB: {e}', 'error')

    return redirect(url_for('dashboard'))

@app.route('/job/<int:job_id>/retry')
@login_required
def retry_job(job_id):
    job = Job.query.get_or_404(job_id)

    # Reset job state
    job.status = 'pending'
    job.logs += f"\n\n--- Retry initiated by user at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---\n"

    # If the session was marked "Error" or "Completed", reset it to "Processing"
    job.session.status = "Processing"

    db.session.commit()
    flash(f'Job "{job.step}" queued for retry.', 'success')
    return redirect(url_for('session_detail', session_id=job.session.id))

if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
    job_manager = JobManager(app)
    job_manager.start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=13131, debug=True)