from database import db
from datetime import datetime
from sqlalchemy.dialects.mysql import LONGTEXT

# Helper to create a column that is TEXT in SQLite/Postgres but LONGTEXT in MariaDB/MySQL
def LargeText():
    return db.Text().with_variant(LONGTEXT, "mysql").with_variant(LONGTEXT, "mariadb")

class Campaign(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    is_default = db.Column(db.Boolean, default=False)
    discord_webhook = db.Column(db.String(255), nullable=True)
    system_prompt = db.Column(LargeText(), nullable=True) # Prompts can be long
    script_paths = db.Column(db.String(500), default="") 
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    sessions = db.relationship('Session', backref='campaign', lazy=True)

class Session(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id'), nullable=False)
    session_number = db.Column(db.Integer, default=1) 
    session_date = db.Column(db.DateTime, nullable=False)
    local_time_str = db.Column(db.String(50), nullable=True)
    original_filename = db.Column(db.String(255), nullable=False)
    directory_path = db.Column(db.String(500), nullable=False)
    status = db.Column(db.String(50), default="Uploaded")
    
    summary_text = db.Column(LargeText(), nullable=True)
    transcript_text = db.Column(LargeText(), nullable=True)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    jobs = db.relationship('Job', backref='session', lazy=True, cascade="all, delete-orphan")
    transcripts = db.relationship('Transcript', backref='session', lazy=True, cascade="all, delete-orphan")

class Transcript(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('session.id'), nullable=False)
    username = db.Column(db.String(100))
    filename = db.Column(db.String(255))
    content = db.Column(LargeText()) # Individual transcripts can be huge
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Job(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('session.id'), nullable=False)
    step = db.Column(db.String(50), nullable=False)
    status = db.Column(db.String(20), default="pending")
    logs = db.Column(LargeText(), default="") # Logs can grow large on errors
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class LLMLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    provider = db.Column(db.String(50))
    model_name = db.Column(db.String(100))
    prompt_tokens = db.Column(db.Integer, default=0)
    completion_tokens = db.Column(db.Integer, default=0)
    total_tokens = db.Column(db.Integer, default=0)
    cost = db.Column(db.Float, default=0.0)
    duration_seconds = db.Column(db.Float, default=0.0)
    request_timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    http_status = db.Column(db.Integer)
    finish_reason = db.Column(db.String(50))
    request_json = db.Column(LargeText()) # JSON payloads with base64 audio are huge
    response_json = db.Column(LargeText())

class DiscordLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('session.id'), nullable=True)
    message_id = db.Column(db.String(100))
    channel_id = db.Column(db.String(100))
    content = db.Column(LargeText())
    request_timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    duration_seconds = db.Column(db.Float)
    http_status = db.Column(db.Integer)
    request_json = db.Column(LargeText())
    response_json = db.Column(LargeText())