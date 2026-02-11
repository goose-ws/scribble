import time
import threading
import logging
import traceback
import shutil
import os
import subprocess
import requests
from database import db
from models import Job, Session
from config import load_config

# Configure Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class JobManager(threading.Thread):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.daemon = True
        self.running = True

    def reset_stuck_jobs(self):
        """Resets any jobs stuck in 'processing' state back to 'pending' on startup."""
        with self.app.app_context():
            stuck_jobs = Job.query.filter_by(status='processing').all()
            if stuck_jobs:
                logging.warning(f"Found {len(stuck_jobs)} stuck jobs. Resetting to pending...")
                for job in stuck_jobs:
                    job.status = 'pending'
                    job.logs += "\n\n[System Restart] Job was interrupted. Resetting to pending..."
                db.session.commit()

    def run(self):
        logging.info("Job Manager Started...")
        
        # Run Reset Logic Once on Boot
        self.reset_stuck_jobs()
        
        while self.running:
            try:
                with self.app.app_context():
                    # Get the oldest pending job
                    job = Job.query.filter_by(status='pending').order_by(Job.created_at.asc()).first()
                    if job:
                        # Mark as processing immediately
                        job.status = 'processing'
                        db.session.commit()
                        self.process_job(job)
                    else:
                        time.sleep(2)
            except Exception as e:
                logging.error(f"Worker Loop Error: {e}")
                time.sleep(5)

    def run_campaign_scripts(self, job, config):
        """
        Helper to run bash scripts attached to a campaign.
        Arguments passed to script: $1 = recap_path, $2 = transcript_path
        """
        session = job.session
        script_dir = "/data/scripts"
        
        # Use script_paths (String) instead of scripts (Relationship)
        if not session.campaign or not session.campaign.script_paths:
            return

        # Parse the comma-separated string
        script_names = [s.strip() for s in session.campaign.script_paths.split(',') if s.strip()]

        if not script_names:
            return

        # --- 1. PREPARE TEMP FILES ---
        job.logs += "\n[System] Staging temporary files for scripts..."
        
        # [FIX] Ensure the directory exists before writing to it
        os.makedirs(session.directory_path, exist_ok=True)
        
        transcript_path = os.path.join(session.directory_path, "session_transcript.txt")
        recap_path = os.path.join(session.directory_path, "session_recap.txt")
        files_to_cleanup = []

        try:
            # Stage Transcript
            if session.transcript_text:
                with open(transcript_path, 'w', encoding='utf-8') as f:
                    f.write(session.transcript_text)
                files_to_cleanup.append(transcript_path)
            
            # Stage Recap
            if session.summary_text:
                with open(recap_path, 'w', encoding='utf-8') as f:
                    f.write(session.summary_text)
                files_to_cleanup.append(recap_path)
        except Exception as e:
            job.logs += f"\n[Error] Failed to stage files: {e}"
            return

        job.logs += f"\n--- Executing {len(script_names)} Campaign Scripts ---"

        # --- 2. RUN SCRIPTS ---
        for script_name in script_names:
            script_name = os.path.basename(script_name)
            script_full_path = os.path.join(script_dir, script_name)
            
            if os.path.exists(script_full_path):
                # Ensure executable
                try:
                    os.chmod(script_full_path, 0o755)
                except Exception as e:
                    job.logs += f"\nWarning: Could not chmod script: {e}"

                job.logs += f"\nRunning: {script_name}"
                db.session.commit()
                
                try:
                    # Pass recap path as $1, transcript path as $2
                    result = subprocess.run(
                        [script_full_path, recap_path, transcript_path],
                        capture_output=True,
                        text=True,
                        timeout=300
                    )
                    
                    if result.stdout:
                        job.logs += f"\n[STDOUT]: {result.stdout}"
                    if result.stderr:
                        job.logs += f"\n[STDERR]: {result.stderr}"
                        
                    if result.returncode == 0:
                        job.logs += f"\nFinished: {script_name} (Success)"
                    else:
                        job.logs += f"\nFailed: {script_name} (Exit Code {result.returncode})"
                        
                except Exception as e:
                    job.logs += f"\nScript execution error: {str(e)}"
            else:
                job.logs += f"\nSkipping: {script_name} (File not found at {script_full_path})"
            
            db.session.commit()

        # --- 3. CLEANUP TEXT FILES ---
        job.logs += "\n[Cleanup] Removing temporary script inputs..."
        for f_path in files_to_cleanup:
            try:
                if os.path.exists(f_path):
                    os.remove(f_path)
            except Exception as e:
                job.logs += f"\n[Warning] Failed to delete {os.path.basename(f_path)}: {e}"

    def process_job(self, job):
        """
        Executes the specific task based on job.step
        NOTE: This runs inside the 'with self.app.app_context():' block from run()
        """
        logging.info(f"Processing Job {job.id}: {job.step} for Session {job.session_id}")
        
        try:
            config = load_config()
            session = job.session # Access directly from job object (already attached)
            
            target_user = None

            # --- 1. TRANSCRIPTION ---
            if job.step == 'transcribe' or job.step.startswith('transcribe:'):
                from transcription_engine import run_transcription
                
                if job.step.startswith('transcribe:'):
                    target_user = job.step.split('transcribe:', 1)[1]
                    job.logs += f"\nTarget User: {target_user}"
                
                job.target_user = target_user 
                
                # Run Transcription
                run_transcription(job, config, self.app)
                
                # [CLEANUP] Remove .flac files if Archive exists
                archive_name = session.original_filename
                archive_exists = False
                if os.path.exists(os.path.join('/data/archive', archive_name)): archive_exists = True
                if not archive_exists:
                    for f in os.listdir('/data/archive'):
                        if f.endswith(archive_name): 
                            archive_exists = True
                            break
                
                if archive_exists:
                    job.logs += "\n[Cleanup] Removing source audio files..."
                    try:
                        folder = session.directory_path
                        for f in os.listdir(folder):
                            if f.endswith('.flac'):
                                os.remove(os.path.join(folder, f))
                    except Exception as e:
                         job.logs += f"\n[Cleanup Warning] {e}"

                # Trigger Summarize ONLY if it was a FULL transcription
                if not target_user:
                    new_job = Job(session_id=session.id, step='summarize', status='pending')
                    new_job.logs = "Queued automatically after transcription."
                    db.session.add(new_job)
                    session.status = "Analyzing"
                else:
                    session.status = "Ready"
                    job.status = 'completed'

                db.session.commit()
                return

            # --- 2. SUMMARIZATION (Auto & Manual) ---
            elif job.step in ['summarize', 'summarize_only']:
                from llm_engine import run_summary
                
                should_post = (job.step == 'summarize')
                
                # Run LLM (Generates DB content)
                run_summary(job, config, post_to_discord_enabled=should_post)
                
                # Run Scripts (Only for full summarize)
                if job.step == 'summarize':
                    self.run_campaign_scripts(job, config)
                else:
                    job.logs += "\nScript execution skipped (Recap Generation Only)."
                
                session.status = "Completed"
                job.status = 'completed'
                db.session.commit()
                return

            # --- 3. MANUAL SCRIPT RUN ---
            elif job.step == 'run_scripts':
                self.run_campaign_scripts(job, config)
                job.status = 'completed'
                db.session.commit()
                return

            # --- 4. DISCORD POSTING ---
            elif job.step == 'post_discord':
                from llm_engine import send_to_discord
                if session.summary_text:
                    if send_to_discord(session.summary_text, config):
                        job.logs += "\nPosted to Discord successfully."
                    else:
                        job.logs += "\nFailed to post to Discord."
                else:
                    job.logs += "\nNo summary text found to post."
                
                job.status = 'completed'
                db.session.commit()
                return

        except Exception as e:
            logging.error(f"Job Failed: {e}")
            job.status = 'error'
            job.logs += f"\nCRITICAL ERROR: {str(e)}\n{traceback.format_exc()}"
            if 'session' in locals() and session:
                session.status = "Error"
            db.session.commit()