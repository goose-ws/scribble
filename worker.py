import time
import threading
import logging
import traceback
import shutil
import os
import subprocess
from database import db
from models import Job, Session
from config import load_config

logging.basicConfig(level=logging.INFO)

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
                    job = Job.query.filter_by(status='pending').order_by(Job.created_at.asc()).first()
                    if job:
                        self.process_job(job)
                    else:
                        time.sleep(2)
            except Exception as e:
                logging.error(f"Job Manager Loop Error: {e}")
                time.sleep(5)

    def process_job(self, job):
        logging.info(f"Processing Job #{job.id} [{job.step}] for Session #{job.session_id}")
        
        job.status = 'processing'
        db.session.commit()
        
        try:
            config = load_config()
            
            if job.step == 'transcribe':
                from transcription_engine import run_transcription
                run_transcription(job, config, self.app)
                
                # Check if next step already exists (avoid duplicates on retry)
                existing_next = Job.query.filter_by(session_id=job.session_id, step='summarize').first()
                if not existing_next:
                    new_job = Job(session_id=job.session_id, step='summarize', status='pending')
                    db.session.add(new_job)
                    
            elif job.step == 'summarize':
                # Standard Auto-Flow: Generate + Post
                from llm_engine import run_summary
                run_summary(job, config, post_to_discord_enabled=True)

            elif job.step == 'summarize_only':
                # Manual Re-Gen: Generate ONLY (Skip Discord)
                from llm_engine import run_summary
                run_summary(job, config, post_to_discord_enabled=False)

            elif job.step == 'post_discord':
                # Manual Post: Discord ONLY
                from llm_engine import run_discord_post
                run_discord_post(job, config)
                
                # Exit early (Skip scripts/cleanup for just a discord post)
                job.status = 'completed'
                db.session.commit()
                return

            # --- SHARED SCRIPT & CLEANUP LOGIC ---
            # Applies to 'summarize' and 'summarize_only'
            if job.step in ['summarize', 'summarize_only']:
                
                # Fetch session to access campaign scripts and paths
                session = Session.query.get(job.session_id)
                recap_path = os.path.join(session.directory_path, "session_recap.txt")
                transcript_path = os.path.join(session.directory_path, "session_transcript.txt")
                
                # 1. Execute Campaign Scripts
                if session.campaign.script_paths:
                    scripts = session.campaign.script_paths.split(',')
                    job.logs += f"\n\n--- Executing {len(scripts)} Campaign Scripts ---"
                    
                    for script_name in scripts:
                        script_name = script_name.strip()
                        if not script_name: continue
                        
                        script_full_path = os.path.join('/data/scripts', script_name)
                        
                        if os.path.exists(script_full_path):
                            # Ensure executable
                            os.chmod(script_full_path, 0o755)
                            
                            job.logs += f"\nRunning: {script_name}"
                            db.session.commit()
                            
                            try:
                                # Pass recap path as $1, transcript path as $2
                                result = subprocess.run(
                                    [script_full_path, recap_path, transcript_path],
                                    capture_output=True,
                                    text=True,
                                    timeout=300 # 5 minute timeout
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
                            job.logs += f"\nSkipping: {script_name} (File not found)"
                        
                        db.session.commit()

                # 2. Update Status
                session.status = "Completed"
                job.status = 'completed'
                db.session.commit()
                
                # 3. Cleanup Logic
                try:
                    # Archive Zip
                    if config.get('archive_zip'):
                        zip_path = os.path.join(session.directory_path, session.original_filename)
                        archive_dir = '/data/archive'
                        if not os.path.exists(archive_dir):
                            os.makedirs(archive_dir)
                            
                        date_prefix = session.session_date.strftime('%Y-%m-%d_')
                        archive_filename = date_prefix + session.original_filename
                        dest_path = os.path.join(archive_dir, archive_filename)
                        
                        if os.path.exists(zip_path):
                            shutil.move(zip_path, dest_path)
                            job.logs += f"\nArchived zip to: {dest_path}"
                    
                    # Delete Working Directory (Optional Space Saver)
                    # Note: If you plan to re-generate often, you might want to disable this 
                    # or rely on 'Re-Transcribe' to restore files.
                    if config.get('db_space_saver') and os.path.exists(session.directory_path):
                        # We only delete if space saver is ON, otherwise we keep files for re-generation
                        # (Adjust this logic based on your preference)
                         # shutil.rmtree(session.directory_path)
                         # job.logs += f"\nCleaned up working directory."
                         pass
                        
                    db.session.commit()
                    
                except Exception as e:
                    job.logs += f"\nCleanup Warning: {str(e)}"
                    db.session.commit()

            # Fallback status update
            if job.status == 'processing':
                job.status = 'completed'
            
            db.session.commit()

        except Exception as e:
            logging.error(f"Job Failed: {e}")
            job.status = 'error'
            job.logs += f"\nCRITICAL ERROR: {str(e)}\n{traceback.format_exc()}"
            db.session.commit()