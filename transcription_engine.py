import os
import glob
import logging
import threading
from datetime import datetime
from faster_whisper import WhisperModel
from database import db
from models import Session, Job, Transcript
from sqlalchemy import text

# --- Custom Log Handler ---
class DBLogHandler(logging.Handler):
    def __init__(self, job_id, app):
        super().__init__()
        self.job_id = job_id
        self.app = app

    def emit(self, record):
        log_entry = self.format(record)
        with self.app.app_context():
            try:
                job = Job.query.get(self.job_id)
                if job:
                    timestamp = datetime.now().strftime('%H:%M:%S')
                    job.logs += f"\n[{timestamp}] {log_entry}"
                    db.session.commit()
            except Exception:
                pass

# Helper to format seconds
def format_timestamp(seconds):
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return "{:02d}:{:02d}:{:02d}".format(int(h), int(m), int(s))

def run_transcription(job, config, app):
    session = Session.query.get(job.session_id)
    if not session:
        raise Exception("Session not found")
    
    # 1. Setup Logging
    db_handler = DBLogHandler(job.id, app)
    db_handler.setLevel(logging.INFO)
    fw_logger = logging.getLogger("faster_whisper")
    fw_logger.addHandler(db_handler)
    fw_logger.setLevel(logging.INFO)
    
    job.logs += f"\nStarting transcription for: {session.original_filename}"
    db.session.commit()
    
    target_user = getattr(job, 'target_user', None)
    
    # 2. Configure Environment
    hf_token = config.get('hf_token')
    if hf_token: os.environ["HF_TOKEN"] = hf_token
    
    # 3. Initialize Whisper
    device = config.get('device', 'cuda')
    compute_type = config.get('whisper_compute_type', 'int8')
    model_size = config.get('whisper_model', 'small')
    
    try:
        model = WhisperModel(model_size, device=device, compute_type=compute_type)
    except Exception as e:
        job.logs += f"\nFATAL: Failed to load model. {str(e)}"
        db.session.commit()
        raise e

    # 4. Find FLAC files
    flac_files = glob.glob(os.path.join(session.directory_path, "*.flac"))
    if not flac_files:
        raise Exception("No .flac files found.")
    
    job.logs += f"\nFound {len(flac_files)} files to transcribe."
    db.session.commit()
        
    master_transcript = []
    
    # 5. Process each file
    for i, file_path in enumerate(flac_files):
        filename = os.path.basename(file_path)
        try:
            username = filename.split('-', 1)[1].rsplit('.', 1)[0]
        except:
            username = "Unknown"
        
        if target_user and username != target_user:
            continue
        
        timestamp = datetime.now().strftime('%H:%M:%S')
        job.logs += f"\n[{timestamp}] Transcribing: {filename} (User: {username})"
        db.session.commit()
        
        user_transcript_lines = []
        
        try:
            segments, info = model.transcribe(
                file_path, 
                beam_size=config.get('whisper_beam_size', 5),
                language=config.get('whisper_language', 'en'),
                vad_filter=True,
                vad_parameters=dict(
                    min_silence_duration_ms=500,
                    threshold=config.get('vad_onset', 0.5)
                )
            )
            
            for segment in segments:
                start_fmt = format_timestamp(segment.start)
                text = segment.text.strip()
                if text:
                    line = f"[{start_fmt}] {username}: {text}"
                    master_transcript.append((segment.start, line))
                    user_transcript_lines.append(line)
            
            user_full_text = "\n".join(user_transcript_lines)

            # Save to Disk
            transcript_dir = os.path.join(session.directory_path, "transcripts")
            os.makedirs(transcript_dir, exist_ok=True)
            
            user_file_path = os.path.join(transcript_dir, f"{username}_transcript.txt")
            with open(user_file_path, 'w', encoding='utf-8') as f:
                f.write(user_full_text)
                
            # Save to DB
            existing_transcript = Transcript.query.filter_by(session_id=session.id, username=username).first()
            if existing_transcript:
                existing_transcript.content = user_full_text
            else:
                new_transcript = Transcript(
                    session_id=session.id,
                    username=username,
                    filename=filename,
                    content=user_full_text
                )
                db.session.add(new_transcript)
                
            timestamp = datetime.now().strftime('%H:%M:%S')
            job.logs += f"\n[{timestamp}] - Completed {filename}: {len(user_transcript_lines)} lines saved."
            db.session.commit()
            
        except Exception as e:
            job.logs += f"\nERROR processing file {filename}: {e}"
            db.session.commit()
            continue

    # 6. Cleanup & Save Master
    fw_logger.removeHandler(db_handler)
            
    master_transcript.sort(key=lambda x: x[0]) 
    final_text = "\n".join([x[1] for x in master_transcript])
    
    transcript_path = os.path.join(session.directory_path, "session_transcript.txt")
    with open(transcript_path, 'w', encoding='utf-8') as f:
        f.write(final_text)
        
    job.logs += f"\nMaster transcript saved to: {transcript_path}"
    
    session.transcript_text = final_text
    db.session.commit()
    
    return True
    