import logging
import os
import zipfile
import shutil
import pytz
import re
from datetime import datetime, timedelta
from functools import wraps
from werkzeug.utils import secure_filename
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from sqlalchemy import func, case
from config import load_config, save_config
from database import init_db, db
from models import Campaign, Session, Job, Transcript, LLMLog
from worker import JobManager

# Configure Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__)
app_config = load_config()
app.secret_key = app_config.get('flask_secret_key', 'fallback_dev_key_if_config_fails')

APP_VERSION = '4.1.0'
@app.context_processor
def inject_version():
    return dict(app_version=APP_VERSION)

# Initialize Database
init_db(app)

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
            stats[key] = match.group(1)

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

    # Convert stored string "s1.sh,s2.sh" back to list for the UI
    current_scripts = campaign.script_paths.split(',') if campaign.script_paths else []

    if request.method == 'POST':
        campaign.name = request.form.get('name')
        campaign.discord_webhook = request.form.get('discord_webhook')
        campaign.system_prompt = request.form.get('system_prompt')

        # Handle Script Selection
        selected_scripts = request.form.getlist('scripts')
        campaign.script_paths = ",".join(selected_scripts)

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

@app.route('/upload', methods=['GET', 'POST'])
@login_required
def upload():
    if request.method == 'GET':
        campaigns = Campaign.query.all()
        return render_template('upload.html', campaigns=campaigns)

    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400

    file = request.files['file']
    campaign_id = request.form.get('campaign_id')

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

            # --- ABUSE PROTECTION START ---
            # 1. Check if it is actually a zip file
            if not zipfile.is_zipfile(zip_path):
                 raise Exception("Uploaded file is not a valid zip archive.")

            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                # 2. Check for internal corruption (CRC check)
                bad_file = zip_ref.testzip()
                if bad_file:
                     raise Exception(f"Zip file is corrupted (Error in {bad_file}).")

                # 3. Check for empty zip
                if len(zip_ref.infolist()) == 0:
                     raise Exception("Zip file is empty.")

                # Safe Extract
                zip_ref.extractall(upload_dir)
            # --- ABUSE PROTECTION END ---

            # Cleanup unwanted files
            raw_dat_path = os.path.join(upload_dir, 'raw.dat')
            if os.path.exists(raw_dat_path):
                os.remove(raw_dat_path)

            # Parse Date
            info_path = os.path.join(upload_dir, 'info.txt')
            session_date_utc, local_time_str = parse_session_date(info_path)

            # Create DB Records
            new_session = Session(
                campaign_id=campaign_id,
                session_date=session_date_utc,
                local_time_str=local_time_str,
                original_filename=filename,
                directory_path=upload_dir,
                status="Processing"
            )
            db.session.add(new_session)
            db.session.commit()

            initial_job = Job(
                session_id=new_session.id,
                step="transcribe",
                status="pending",
                logs="Job queued."
            )
            db.session.add(initial_job)
            db.session.commit()

            return jsonify({'success': True, 'redirect': url_for('dashboard')})

        except Exception as e:
            logging.error(f"Upload failed: {e}")
            # Immediate Cleanup of the bad upload folder
            if os.path.exists(upload_dir):
                shutil.rmtree(upload_dir)
            return jsonify({'error': str(e)}), 500

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

# In session_detail
@app.route('/session/<int:session_id>')
@login_required
def session_detail(session_id):
    session_obj = Session.query.get_or_404(session_id)
    jobs = Job.query.filter_by(session_id=session_obj.id).order_by(Job.created_at.asc()).all()

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
    transcribe_job = next((j for j in jobs if j.step == 'transcribe'), None)
    user_metrics = {}
    if transcribe_job:
        user_metrics = parse_transcription_metrics(transcribe_job.logs, user_transcripts)

    summarize_job = next((j for j in jobs if j.step == 'summarize'), None)
    integrations = {'discord_sent': False, 'scripts': []}
    if summarize_job:
        integrations = parse_integrations_status(summarize_job.logs)

    total_duration = "Pending..."
    if session_obj.created_at:
        start_time = session_obj.created_at

        if session_obj.status == 'Completed':
            # Use the timestamp of the last completed job
            last_job = Job.query.filter_by(session_id=session_obj.id).order_by(Job.updated_at.desc()).first()
            if last_job:
                delta = last_job.updated_at - start_time
                total_duration = str(delta).split('.')[0] # Remove microseconds
        elif session_obj.status == 'Processing':
            # Calculate time elapsed so far
            delta = datetime.utcnow() - start_time
            total_duration = str(delta).split('.')[0] + " (Running)"
        else:
            # Error state or just uploaded
            if jobs:
                last_job = jobs[-1]
                delta = last_job.updated_at - start_time
                total_duration = str(delta).split('.')[0]

    return render_template('session_detail.html',
                         recording_session=session_obj,
                         jobs=jobs,
                         transcript=transcript_text,
                         user_transcripts=user_transcripts,
                         llm_stats=llm_stats,
                         user_metrics=user_metrics,
                         integrations=integrations,
                         total_duration=total_duration) # Pass to template

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
