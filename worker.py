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

    def process_job(self, job):
        """
        Executes the specific task based on job.step
        """
        logging.info(f"Processing Job {job.id}: {job.step} for Session {job.session_id}")
        
        try:
            config = load_config()
            session = Session.query.get(job.session_id)
            
            # --- 1. TRANSCRIPTION ---
            if job.step == 'transcribe' or job.step.startswith('transcribe:'):
                from transcription_engine import run_transcription
                
                target_user = None
                if ':' in job.step:
                    target_user = job.step.split(':', 1)[1]
                
                # Attach target_user to job object for the engine to find
                job.target_user = target_user 
                
                run_transcription(job, config, self.app)
                
                # Only chain 'summarize' if it was a FULL transcribe (not a single user repair)
                if not target_user:
                    existing_next = Job.query.filter_by(session_id=job.session_id, step='summarize').first()
                    if not existing_next:
                        new_job = Job(session_id=job.session_id, step='summarize', status='pending')
                        db.session.add(new_job)
                        db.session.commit()
                
                job.status = 'completed'
                db.session.commit()
                return

            # --- 2. SUMMARIZATION (Auto & Manual) ---
            elif job.step in ['summarize', 'summarize_only']:
                from llm_engine import run_summary
                
                # Determine Discord behavior
                should_post = (job.step == 'summarize')
                
                # Run LLM Summary (Note: run_summary handles the DB save and Discord post internally)
                run_summary(job, config, post_to_discord_enabled=should_post)
                
                # Run Campaign Scripts
                self.run_campaign_scripts(job, config)
                
                session.status = "Completed"
                job.status = 'completed'
                db.session.commit()
                return

            # --- 3. RUN SCRIPTS ONLY (Manual Trigger) ---
            elif job.step == 'run_scripts':
                self.run_campaign_scripts(job, config)
                
                job.status = 'completed'
                db.session.commit()
                return

            # --- 4. DISCORD POST ONLY (Manual Trigger) ---
            elif job.step == 'post_discord':
                from llm_engine import run_discord_post
                
                run_discord_post(job, config)
                
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

    def run_campaign_scripts(self, job, config):
        """
        Helper to run bash scripts attached to a campaign.
        Arguments passed to script: $1 = recap_path, $2 = transcript_path
        """
        session = Session.query.get(job.session_id)
        if not session.campaign or not session.campaign.script_paths:
            return

        recap_path = os.path.join(session.directory_path, "session_recap.txt")
        transcript_path = os.path.join(session.directory_path, "session_transcript.txt")
        
        scripts = session.campaign.script_paths.split(',')
        job.logs += f"\n\n--- Executing {len(scripts)} Campaign Scripts ---"
        
        for script_name in scripts:
            script_name = script_name.strip()
            if not script_name: continue
            
            # Sanitize script name to prevent directory traversal
            script_name = os.path.basename(script_name)
            script_full_path = os.path.join('/data/scripts', script_name)
            
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