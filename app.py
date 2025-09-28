import os
import re
import math
import requests
import secrets
from flask import Flask, request, render_template, redirect, url_for, session, flash
from werkzeug.utils import secure_filename

app = Flask(__name__)

# --- Secret Key and Password (Unchanged) ---
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

# --- Configuration ---
SAVE_PATH = "/app/Sessions"
ALLOWED_EXTENSIONS = {'zip'} # Allowed file extensions

# --- Helper Functions ---
def is_user_logged_in():
    return session.get('logged_in')

# --- Validate file extensions ---
def allowed_file(filename):
    """Check if the uploaded file has a .zip extension."""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def download_file_from_url(url, save_dir):
    try:
        filename = secure_filename(url.split('/')[-1] or "downloaded_file")
        if not allowed_file(filename):
            return "Error: URL must point to a .zip file."

        with requests.get(url, stream=True) as r:
            r.raise_for_status()
            with open(os.path.join(save_dir, filename), 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
        return f"Successfully downloaded '{filename}' from URL."
    except requests.exceptions.RequestException as e:
        return f"Error downloading from URL: {e}"

# --- Routes ---
@app.route('/', methods=['GET', 'POST'])
def upload_page():
    if not is_user_logged_in():
        return redirect(url_for('login'))
    if request.method == 'POST':
        os.makedirs(SAVE_PATH, exist_ok=True)
        # --- Handle Direct File Upload ---
        file = request.files.get('file')
        if file and file.filename:
            # TWEAK 1: Validate file extension before saving
            if allowed_file(file.filename):
                filename = secure_filename(file.filename)
                file.save(os.path.join(SAVE_PATH, filename))
                flash(f"File '{filename}' uploaded successfully!", 'success')
            else:
                flash("Invalid file type. Please upload a .zip file.", 'error')
            return redirect(url_for('upload_page'))
        # --- Handle URL Download ---
        url = request.form.get('url')
        if url:
            message = download_file_from_url(url, SAVE_PATH)
            flash(message, 'success' if "Error" not in message else 'error')
            return redirect(url_for('upload_page'))
        flash("No file or URL provided.", 'error')
    return render_template('upload.html')

# --- Status page ---
@app.route('/status')
def status_page():
    """Scans the sessions directory and reports the status of each item."""
    if not is_user_logged_in():
        return redirect(url_for('login'))

    if not os.path.exists(SAVE_PATH):
        return render_template('status.html', sessions=[])

    sessions = []
    # Use a regex to identify date-formatted directories
    date_folder_pattern = re.compile(r'^\d{4}-\d{2}-\d{2}$')

    for item_name in sorted(os.listdir(SAVE_PATH)):
        item_path = os.path.join(SAVE_PATH, item_name)

        # Case 1: ZIP file is waiting to be processed
        if os.path.isfile(item_path) and item_name.lower().endswith('.zip'):
            sessions.append({'name': item_name, 'status': 'Waiting', 'progress': 0})
            continue

        # Case 2: Date folder is being processed or is complete
        if os.path.isdir(item_path) and date_folder_pattern.match(item_name):
            session_dir = item_path
            
            # Check for completion state
            if os.path.exists(os.path.join(session_dir, 'gemini_recap.txt')):
                sessions.append({'name': item_name, 'status': 'Complete', 'progress': 100})
                continue
            
            # If not complete, calculate progress
            flac_files = [f for f in os.listdir(session_dir) if f.lower().endswith('.flac')]
            if not flac_files:
                continue # Skip empty or malformed directories

            usernames = [os.path.splitext(f)[0].split('-', 1)[-1] for f in flac_files]
            
            steps_total = (len(usernames) * 3) + 2
            steps_done = 0

            for user in usernames:
                if os.path.exists(os.path.join(session_dir, 'progress', f'{user}.txt')):
                    steps_done += 1
                if os.path.exists(os.path.join(session_dir, 'transcripts', f'{user}_transcript.json')):
                    steps_done += 1
                if os.path.exists(os.path.join(session_dir, 'transcripts', f'{user}_transcript.txt')):
                    steps_done += 1
            
            if os.path.exists(os.path.join(session_dir, 'session_transcript.txt')):
                steps_done += 1
            
            progress = math.floor((steps_done / steps_total) * 100) if steps_total > 0 else 0
            sessions.append({'name': item_name, 'status': 'In Progress', 'progress': progress})

    return render_template('status.html', sessions=sessions)

# --- Other Routes ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get('password') == APP_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('upload_page'))
        else:
            flash("Invalid password.", 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))