import json
import os
import secrets
import logging
import bcrypt

CONFIG_PATH = '/data/config.json'

# Default Configuration
DEFAULT_CONFIG = {
    # System
    "device": "cuda",
    "webui_password": "",
    "flask_secret_key": "",
    "archive_zip": False,
    "db_space_saver": True,
    "dark_mode": False,
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
    "whisper_initial_prompt": "",
    "hf_token": "",
    # VAD
    "vad_method": "silero",
    "vad_onset": 0.5,
    "vad_min_speech_ms": 0.363,
    # LLM
    "llm_provider": "Google",
    "llm_model": "gemini-flash-latest",
    "google_api_key": "",
    "anthropic_api_key": "",
    "openai_api_key": "",
    "ollama_url": "",
    # Per-provider token costs (per million tokens)
    "llm_costs": {
        "Google":    {"input": 0.0, "output": 0.0},
        "OpenAI":    {"input": 0.0, "output": 0.0},
        "Anthropic": {"input": 0.0, "output": 0.0},
        "Ollama":    {"input": 0.0, "output": 0.0}
    }
}

_config_cache = None

def load_config():
    """Load config from disk, or create with defaults if missing."""
    global _config_cache
    if _config_cache is not None:
        return _config_cache

    config = DEFAULT_CONFIG.copy()
    # Deep-copy nested dicts so defaults are never mutated
    config['llm_costs'] = {k: v.copy() for k, v in DEFAULT_CONFIG['llm_costs'].items()}

    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r') as f:
                saved_config = json.load(f)

            # ------------------------------------------------------------------
            # Forward-migrations from older config formats
            # ------------------------------------------------------------------

            # 1. Single llm_api_key -> per-provider key
            if saved_config.get('llm_api_key'):
                old_provider = saved_config.get('llm_provider', 'Google')
                key_map = {
                    'Google':    'google_api_key',
                    'OpenAI':    'openai_api_key',
                    'Anthropic': 'anthropic_api_key',
                }
                target = key_map.get(old_provider)
                if target and not saved_config.get(target):
                    saved_config[target] = saved_config['llm_api_key']
                    logging.info(f"Config migration: llm_api_key -> {target}")

            # 2. Flat llm_input_cost / llm_output_cost -> llm_costs dict
            if 'llm_input_cost' in saved_config or 'llm_output_cost' in saved_config:
                old_provider = saved_config.get('llm_provider', 'Google')
                if 'llm_costs' not in saved_config:
                    saved_config['llm_costs'] = {
                        k: v.copy() for k, v in DEFAULT_CONFIG['llm_costs'].items()
                    }
                if old_provider in saved_config['llm_costs']:
                    if 'llm_input_cost' in saved_config:
                        saved_config['llm_costs'][old_provider]['input'] = float(
                            saved_config['llm_input_cost']
                        )
                    if 'llm_output_cost' in saved_config:
                        saved_config['llm_costs'][old_provider]['output'] = float(
                            saved_config['llm_output_cost']
                        )
                    logging.info(
                        f"Config migration: llm_input/output_cost -> llm_costs[{old_provider}]"
                    )

            # ------------------------------------------------------------------
            # Merge llm_costs carefully: saved values win, but any provider
            # missing from the saved dict gets the default (handles new providers
            # added in future versions).
            # ------------------------------------------------------------------
            if 'llm_costs' in saved_config:
                merged_costs = {k: v.copy() for k, v in DEFAULT_CONFIG['llm_costs'].items()}
                for provider, costs in saved_config['llm_costs'].items():
                    if provider in merged_costs:
                        merged_costs[provider].update(costs)
                    else:
                        merged_costs[provider] = costs.copy()
                config['llm_costs'] = merged_costs
                del saved_config['llm_costs']

            # Apply remaining saved values (flat keys only at this point)
            config.update(saved_config)

            # Remove stale keys no longer in DEFAULT_CONFIG (keeps the file clean)
            stale = [k for k in list(config.keys()) if k not in DEFAULT_CONFIG]
            if stale:
                for k in stale:
                    del config[k]
                save_config(config)

        except Exception as e:
            logging.error(f"Error loading config.json: {e}")

    # Security: generate WebUI password on first run
    if not config['webui_password']:
        temp_pass = secrets.token_urlsafe(12)
        config['webui_password'] = bcrypt.hashpw(temp_pass.encode(), bcrypt.gensalt()).decode()
        logging.warning("=" * 50)
        logging.warning(f"FIRST RUN - TEMPORARY PASSWORD: {temp_pass}")
        logging.warning("=" * 50)
        save_config(config)

    # Security: generate Flask secret key if missing
    if not config['flask_secret_key']:
        config['flask_secret_key'] = secrets.token_hex(32)
        save_config(config)
        
    # 3. vad_offset -> vad_min_speech_ms
    if 'vad_offset' in saved_config and 'vad_min_speech_ms' not in saved_config:
        saved_config['vad_min_speech_ms'] = saved_config.pop('vad_offset')
        logging.info("Config migration: vad_offset -> vad_min_speech_ms")

    _config_cache = config
    return config

def save_config(config):
    """Save the current config to disk."""
    global _config_cache
    _config_cache = config  # update cache immediately
    try:
        with open(CONFIG_PATH, 'w') as f:
            json.dump(config, f, indent=4)
    except Exception as e:
        logging.error(f"Error saving config.json: {e}")

def get_effective_config(global_config, campaign):
    """
    Merge global config with campaign-level overrides.

    Returns a new dict — global_config is never mutated.
    Any campaign field that is None means "inherit from global".

    The returned config always has flat `llm_input_cost` and `llm_output_cost`
    keys resolved from the effective provider's llm_costs entry, so existing
    code in llm_engine.py that calls calculate_cost() continues to work without
    changes.
    """
    effective = global_config.copy()
    # Deep-copy nested dicts so callers cannot accidentally mutate global state
    if 'llm_costs' in effective:
        effective['llm_costs'] = {
            k: v.copy() for k, v in effective['llm_costs'].items()
        }

    if campaign is None:
        _resolve_effective_costs(effective)
        return effective

    # --- Whisper overrides ---
    for field in (
        'whisper_model', 'whisper_threads', 'whisper_batch_size',
        'whisper_beam_size', 'whisper_compute_type', 'whisper_language',
    ):
        val = getattr(campaign, field, None)
        if val is not None:
            effective[field] = val

    # --- VAD overrides ---
    for field in ('vad_method', 'vad_onset'):
        val = getattr(campaign, field, None)
        if val is not None:
            effective[field] = val

    # vad_offset column maps to the renamed config key
    val = getattr(campaign, 'vad_offset', None)
    if val is not None:
        effective['vad_min_speech_ms'] = val

    # --- LLM overrides ---
    if campaign.llm_provider:
        effective['llm_provider'] = campaign.llm_provider
    if campaign.llm_model:
        effective['llm_model'] = campaign.llm_model

    # --- Cost resolution (provider defaults first, then campaign override) ---
    _resolve_effective_costs(effective)
    if campaign.llm_input_cost is not None:
        effective['llm_input_cost'] = campaign.llm_input_cost
    if campaign.llm_output_cost is not None:
        effective['llm_output_cost'] = campaign.llm_output_cost

    return effective

def _resolve_effective_costs(effective):
    """
    Populate flat llm_input_cost / llm_output_cost on `effective` from the
    llm_costs dict based on the currently active llm_provider.
    Called internally by get_effective_config before campaign cost overrides
    are applied.
    """
    provider = effective.get('llm_provider', 'Google')
    provider_costs = effective.get('llm_costs', {}).get(provider, {})
    effective['llm_input_cost'] = provider_costs.get('input', 0.0)
    effective['llm_output_cost'] = provider_costs.get('output', 0.0)