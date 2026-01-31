import json
import os
import secrets
import logging

CONFIG_PATH = '/data/config.json'

# Default Configuration
DEFAULT_CONFIG = {
    # System
    "device": "cuda",
    "webui_password": "",
    "flask_secret_key": "", # NEW: Secure session storage
    "archive_zip": False,
    "db_space_saver": True,
    
    # Database
    "db_type": "sqlite",
    "db_address": "",
    "db_name": "scribble",
    "db_username": "",
    "db_password": "",

    # Whisper
    "whisper_model": "small",
    "whisper_threads": 0,
    "whisper_batch_size": 16,
    "whisper_beam_size": 5,
    "whisper_compute_type": "int8",
    "whisper_language": "en",
    "hf_token": "",

    # VAD
    "vad_method": "silero",
    "vad_onset": 0.5,
    "vad_offset": 0.363,

    # LLM
    "llm_provider": "Google",
    "llm_model": "gemini-2.5-flash",
    "llm_api_key": "",
    "llm_input_cost": 0.0,
    "llm_output_cost": 0.0
}

def load_config():
    """Load config from disk, or create with defaults if missing."""
    if not os.path.exists('/data'):
        os.makedirs('/data', exist_ok=True)
    
    config = DEFAULT_CONFIG.copy()
    
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r') as f:
                saved_config = json.load(f)
                config.update(saved_config)
            
            # Cleanup deprecated keys
            keys_to_remove = [k for k in config if k not in DEFAULT_CONFIG]
            if keys_to_remove:
                for k in keys_to_remove:
                    del config[k]
                save_config(config)
                
        except Exception as e:
            logging.error(f"Error loading config.json: {e}")

    # Security: Generate WebUI Password if missing
    if not config['webui_password']:
        temp_pass = secrets.token_urlsafe(12)
        config['webui_password'] = temp_pass
        logging.warning("="*50)
        logging.warning(f"FIRST RUN - TEMPORARY PASSWORD: {temp_pass}")
        logging.warning("="*50)
        save_config(config)

    # Security: Generate Flask Secret Key if missing (NEW)
    if not config['flask_secret_key']:
        new_secret = secrets.token_hex(32)
        config['flask_secret_key'] = new_secret
        save_config(config)
        
    return config

def save_config(config):
    """Save the current config to disk."""
    try:
        with open(CONFIG_PATH, 'w') as f:
            json.dump(config, f, indent=4)
    except Exception as e:
        logging.error(f"Error saving config.json: {e}")