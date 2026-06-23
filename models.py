from database import db
from datetime import datetime
from sqlalchemy.dialects.mysql import LONGTEXT


def LargeText():
    return db.Text().with_variant(LONGTEXT, "mysql").with_variant(LONGTEXT, "mariadb")


class Campaign(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    is_default = db.Column(db.Boolean, default=False)
    discord_webhook = db.Column(db.String(255), nullable=True)
    system_prompt = db.Column(LargeText(), nullable=True)
    script_paths = db.Column(db.String(500), default="")
    recap_context_enabled = db.Column(db.Boolean, default=False)
    recap_context_count = db.Column(db.Integer, default=3)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    whisper_model = db.Column(db.String(50), nullable=True)
    whisper_threads = db.Column(db.Integer, nullable=True)
    whisper_batch_size = db.Column(db.Integer, nullable=True)
    whisper_beam_size = db.Column(db.Integer, nullable=True)
    whisper_compute_type = db.Column(db.String(20), nullable=True)
    whisper_language = db.Column(db.String(10), nullable=True)
    whisper_initial_prompt = db.Column(db.String(500), nullable=True)
    whisper_condition_on_previous_text = db.Column(db.Boolean, nullable=True)
    whisper_compression_ratio_threshold = db.Column(db.Float, nullable=True)
    whisper_no_speech_threshold = db.Column(db.Float, nullable=True)

    vad_method = db.Column(db.String(20), nullable=True)
    vad_onset = db.Column(db.Float, nullable=True)
    vad_offset = db.Column(db.Float, nullable=True)
    vad_min_silence_ms = db.Column(db.Float, nullable=True)
    vad_max_speech_s = db.Column(db.Float, nullable=True)

    llm_provider = db.Column(db.String(50), nullable=True)
    llm_model = db.Column(db.String(100), nullable=True)
    llm_input_cost = db.Column(db.Float, nullable=True)
    llm_output_cost = db.Column(db.Float, nullable=True)

    username_map = db.Column(db.Text, nullable=True)
    transcript_remove_timestamps = db.Column(db.Boolean, default=False)
    transcript_consolidate_lines = db.Column(db.Boolean, default=False)

    sessions = db.relationship('Session', backref='campaign', lazy=True)


class Session(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id'), nullable=False)
    session_number = db.Column(db.Integer, default=0)
    session_date = db.Column(db.DateTime, nullable=False)
    local_time_str = db.Column(db.String(50), nullable=True)
    original_filename = db.Column(db.String(255), nullable=False)
    directory_path = db.Column(db.String(500), nullable=False)
    status = db.Column(db.String(50), default="Uploaded")
    summary_text = db.Column(LargeText(), nullable=True)
    transcript_text = db.Column(LargeText(), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Session-level overrides (None = inherit from campaign)
    session_prompt = db.Column(LargeText(), nullable=True)
    session_username_map = db.Column(db.Text, nullable=True)
    session_remove_timestamps = db.Column(db.Boolean, nullable=True)
    session_consolidate_lines = db.Column(db.Boolean, nullable=True)

    jobs = db.relationship('Job', backref='session', lazy=True, cascade="all, delete-orphan")
    transcripts = db.relationship('Transcript', backref='session', lazy=True, cascade="all, delete-orphan")

    def effective_prompt(self):
        return self.session_prompt or (self.campaign.system_prompt if self.campaign else None)

    def effective_username_map(self):
        return self.session_username_map or (self.campaign.username_map if self.campaign else None)

    def effective_remove_timestamps(self):
        if self.session_remove_timestamps is not None:
            return self.session_remove_timestamps
        return self.campaign.transcript_remove_timestamps if self.campaign else False

    def effective_consolidate_lines(self):
        if self.session_consolidate_lines is not None:
            return self.session_consolidate_lines
        return self.campaign.transcript_consolidate_lines if self.campaign else False


class Transcript(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('session.id'), nullable=False)
    username = db.Column(db.String(100))
    filename = db.Column(db.String(255))
    content = db.Column(LargeText())
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Job(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('session.id'), nullable=False)
    step = db.Column(db.String(50), nullable=False)
    status = db.Column(db.String(20), default="pending")
    logs = db.Column(LargeText(), default="")
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
    request_json = db.Column(LargeText())
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