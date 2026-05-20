import logging
from sqlalchemy import text
from app import app, db
from models import Session, Campaign
from app import app, db, APP_VERSION

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("migration")

def run_migration():
    from config import load_config, save_config

    # Version check — skip migrations if already at current version
    config = load_config()
    last_version = config.get('last_migrated_version')
    if last_version == APP_VERSION:
        logger.info(f"Schema already at v{APP_VERSION} — skipping migrations.")
        return

    logger.info(f"Schema version: {last_version or 'unknown'} → {APP_VERSION}. Running migrations...")

    with app.app_context():

        # 1. Schema Migrations (Columns)
        try:
            with db.engine.connect() as conn:

                # --- Session: session_number ---
                try:
                    conn.execute(text("SELECT session_number FROM session LIMIT 1"))
                    logger.info("Column 'session_number' already exists in Session.")
                except Exception:
                    logger.info("Adding 'session_number' column to Session...")
                    conn.execute(text("ALTER TABLE session ADD COLUMN session_number INTEGER DEFAULT 0"))
                    conn.commit()

                # --- Campaign: is_default ---
                try:
                    conn.execute(text("SELECT is_default FROM campaign LIMIT 1"))
                    logger.info("Column 'is_default' already exists in Campaign.")
                except Exception:
                    logger.info("Adding 'is_default' column to Campaign...")
                    conn.execute(text("ALTER TABLE campaign ADD COLUMN is_default BOOLEAN DEFAULT 0"))
                    conn.commit()

                # --- Campaign: recap_context_enabled ---
                try:
                    conn.execute(text("SELECT recap_context_enabled FROM campaign LIMIT 1"))
                    logger.info("Column 'recap_context_enabled' already exists in Campaign.")
                except Exception:
                    logger.info("Adding 'recap_context_enabled' column to Campaign...")
                    conn.execute(text("ALTER TABLE campaign ADD COLUMN recap_context_enabled BOOLEAN DEFAULT 0"))
                    conn.commit()

                # --- Campaign: recap_context_count ---
                try:
                    conn.execute(text("SELECT recap_context_count FROM campaign LIMIT 1"))
                    logger.info("Column 'recap_context_count' already exists in Campaign.")
                except Exception:
                    logger.info("Adding 'recap_context_count' column to Campaign...")
                    conn.execute(text("ALTER TABLE campaign ADD COLUMN recap_context_count INTEGER DEFAULT 3"))
                    conn.commit()

                # --- Campaign: llm_provider ---
                try:
                    conn.execute(text("SELECT llm_provider FROM campaign LIMIT 1"))
                    logger.info("Column 'llm_provider' already exists in Campaign.")
                except Exception:
                    logger.info("Adding 'llm_provider' column to Campaign...")
                    conn.execute(text("ALTER TABLE campaign ADD COLUMN llm_provider VARCHAR(50) NULL"))
                    conn.commit()

                # --- Campaign: llm_model ---
                try:
                    conn.execute(text("SELECT llm_model FROM campaign LIMIT 1"))
                    logger.info("Column 'llm_model' already exists in Campaign.")
                except Exception:
                    logger.info("Adding 'llm_model' column to Campaign...")
                    conn.execute(text("ALTER TABLE campaign ADD COLUMN llm_model VARCHAR(100) NULL"))
                    conn.commit()

                # --- Campaign: llm_input_cost ---
                try:
                    conn.execute(text("SELECT llm_input_cost FROM campaign LIMIT 1"))
                    logger.info("Column 'llm_input_cost' already exists in Campaign.")
                except Exception:
                    logger.info("Adding 'llm_input_cost' column to Campaign...")
                    conn.execute(text("ALTER TABLE campaign ADD COLUMN llm_input_cost FLOAT NULL"))
                    conn.commit()

                # --- Campaign: llm_output_cost ---
                try:
                    conn.execute(text("SELECT llm_output_cost FROM campaign LIMIT 1"))
                    logger.info("Column 'llm_output_cost' already exists in Campaign.")
                except Exception:
                    logger.info("Adding 'llm_output_cost' column to Campaign...")
                    conn.execute(text("ALTER TABLE campaign ADD COLUMN llm_output_cost FLOAT NULL"))
                    conn.commit()

                # --- Campaign: whisper_model ---
                try:
                    conn.execute(text("SELECT whisper_model FROM campaign LIMIT 1"))
                    logger.info("Column 'whisper_model' already exists in Campaign.")
                except Exception:
                    logger.info("Adding 'whisper_model' column to Campaign...")
                    conn.execute(text("ALTER TABLE campaign ADD COLUMN whisper_model VARCHAR(50) NULL"))
                    conn.commit()

                # --- Campaign: whisper_threads ---
                try:
                    conn.execute(text("SELECT whisper_threads FROM campaign LIMIT 1"))
                    logger.info("Column 'whisper_threads' already exists in Campaign.")
                except Exception:
                    logger.info("Adding 'whisper_threads' column to Campaign...")
                    conn.execute(text("ALTER TABLE campaign ADD COLUMN whisper_threads INTEGER NULL"))
                    conn.commit()

                # --- Campaign: whisper_batch_size ---
                try:
                    conn.execute(text("SELECT whisper_batch_size FROM campaign LIMIT 1"))
                    logger.info("Column 'whisper_batch_size' already exists in Campaign.")
                except Exception:
                    logger.info("Adding 'whisper_batch_size' column to Campaign...")
                    conn.execute(text("ALTER TABLE campaign ADD COLUMN whisper_batch_size INTEGER NULL"))
                    conn.commit()

                # --- Campaign: whisper_beam_size ---
                try:
                    conn.execute(text("SELECT whisper_beam_size FROM campaign LIMIT 1"))
                    logger.info("Column 'whisper_beam_size' already exists in Campaign.")
                except Exception:
                    logger.info("Adding 'whisper_beam_size' column to Campaign...")
                    conn.execute(text("ALTER TABLE campaign ADD COLUMN whisper_beam_size INTEGER NULL"))
                    conn.commit()

                # --- Campaign: whisper_compute_type ---
                try:
                    conn.execute(text("SELECT whisper_compute_type FROM campaign LIMIT 1"))
                    logger.info("Column 'whisper_compute_type' already exists in Campaign.")
                except Exception:
                    logger.info("Adding 'whisper_compute_type' column to Campaign...")
                    conn.execute(text("ALTER TABLE campaign ADD COLUMN whisper_compute_type VARCHAR(20) NULL"))
                    conn.commit()

                # --- Campaign: whisper_language ---
                try:
                    conn.execute(text("SELECT whisper_language FROM campaign LIMIT 1"))
                    logger.info("Column 'whisper_language' already exists in Campaign.")
                except Exception:
                    logger.info("Adding 'whisper_language' column to Campaign...")
                    conn.execute(text("ALTER TABLE campaign ADD COLUMN whisper_language VARCHAR(10) NULL"))
                    conn.commit()

                # --- Campaign: whisper_initial_prompt ---
                try:
                    conn.execute(text("SELECT whisper_initial_prompt FROM campaign LIMIT 1"))
                    logger.info("Column 'whisper_initial_prompt' already exists in Campaign.")
                except Exception:
                    logger.info("Adding 'whisper_initial_prompt' column to Campaign...")
                    conn.execute(text("ALTER TABLE campaign ADD COLUMN whisper_initial_prompt VARCHAR(500) NULL"))
                    conn.commit()

                # --- Campaign: vad_method ---
                try:
                    conn.execute(text("SELECT vad_method FROM campaign LIMIT 1"))
                    logger.info("Column 'vad_method' already exists in Campaign.")
                except Exception:
                    logger.info("Adding 'vad_method' column to Campaign...")
                    conn.execute(text("ALTER TABLE campaign ADD COLUMN vad_method VARCHAR(20) NULL"))
                    conn.commit()

                # --- Campaign: vad_onset ---
                try:
                    conn.execute(text("SELECT vad_onset FROM campaign LIMIT 1"))
                    logger.info("Column 'vad_onset' already exists in Campaign.")
                except Exception:
                    logger.info("Adding 'vad_onset' column to Campaign...")
                    conn.execute(text("ALTER TABLE campaign ADD COLUMN vad_onset FLOAT NULL"))
                    conn.commit()

                # --- Campaign: vad_offset ---
                try:
                    conn.execute(text("SELECT vad_offset FROM campaign LIMIT 1"))
                    logger.info("Column 'vad_offset' already exists in Campaign.")
                except Exception:
                    logger.info("Adding 'vad_offset' column to Campaign...")
                    conn.execute(text("ALTER TABLE campaign ADD COLUMN vad_offset FLOAT NULL"))
                    conn.commit()

                # --- Campaign: username_map ---
                try:
                    conn.execute(text("SELECT username_map FROM campaign LIMIT 1"))
                    logger.info("Column 'username_map' already exists in Campaign.")
                except Exception:
                    logger.info("Adding 'username_map' column to Campaign...")
                    conn.execute(text("ALTER TABLE campaign ADD COLUMN username_map TEXT NULL"))
                    conn.commit()

                # --- Campaign: transcript_remove_timestamps ---
                try:
                    conn.execute(text("SELECT transcript_remove_timestamps FROM campaign LIMIT 1"))
                    logger.info("Column 'transcript_remove_timestamps' already exists in Campaign.")
                except Exception:
                    logger.info("Adding 'transcript_remove_timestamps' column to Campaign...")
                    conn.execute(text("ALTER TABLE campaign ADD COLUMN transcript_remove_timestamps BOOLEAN DEFAULT 0"))
                    conn.commit()

                # --- Campaign: transcript_consolidate_lines ---
                try:
                    conn.execute(text("SELECT transcript_consolidate_lines FROM campaign LIMIT 1"))
                    logger.info("Column 'transcript_consolidate_lines' already exists in Campaign.")
                except Exception:
                    logger.info("Adding 'transcript_consolidate_lines' column to Campaign...")
                    conn.execute(text("ALTER TABLE campaign ADD COLUMN transcript_consolidate_lines BOOLEAN DEFAULT 0"))
                    conn.commit()

                # --- DiscordLog: session_id ---
                try:
                    conn.execute(text("SELECT session_id FROM discord_log LIMIT 1"))
                    logger.info("Column 'session_id' already exists in DiscordLog.")
                except Exception:
                    logger.info("Adding 'session_id' column to DiscordLog...")
                    conn.execute(text("ALTER TABLE discord_log ADD COLUMN session_id INTEGER NULL"))
                    try:
                        conn.execute(text(
                            "ALTER TABLE discord_log ADD CONSTRAINT fk_discord_log_session "
                            "FOREIGN KEY (session_id) REFERENCES session(id)"
                        ))
                    except Exception as e:
                        logger.warning(f"Could not add FK constraint: {e}")
                    conn.commit()
                    
                # --- Session: session_prompt ---
                try:
                    conn.execute(text("SELECT session_prompt FROM session LIMIT 1"))
                    logger.info("Column 'session_prompt' already exists in Session.")
                except Exception:
                    logger.info("Adding 'session_prompt' column to Session...")
                    conn.execute(text("ALTER TABLE session ADD COLUMN session_prompt LONGTEXT NULL"))
                    conn.commit()

                # --- Session: session_username_map ---
                try:
                    conn.execute(text("SELECT session_username_map FROM session LIMIT 1"))
                    logger.info("Column 'session_username_map' already exists in Session.")
                except Exception:
                    logger.info("Adding 'session_username_map' column to Session...")
                    conn.execute(text("ALTER TABLE session ADD COLUMN session_username_map TEXT NULL"))
                    conn.commit()

                # --- Session: session_remove_timestamps ---
                try:
                    conn.execute(text("SELECT session_remove_timestamps FROM session LIMIT 1"))
                    logger.info("Column 'session_remove_timestamps' already exists in Session.")
                except Exception:
                    logger.info("Adding 'session_remove_timestamps' column to Session...")
                    conn.execute(text("ALTER TABLE session ADD COLUMN session_remove_timestamps BOOLEAN NULL"))
                    conn.commit()

                # --- Session: session_consolidate_lines ---
                try:
                    conn.execute(text("SELECT session_consolidate_lines FROM session LIMIT 1"))
                    logger.info("Column 'session_consolidate_lines' already exists in Session.")
                except Exception:
                    logger.info("Adding 'session_consolidate_lines' column to Session...")
                    conn.execute(text("ALTER TABLE session ADD COLUMN session_consolidate_lines BOOLEAN NULL"))
                    conn.commit()

        except Exception as e:
            logger.error(f"Error checking/adding columns: {e}")

        # 2. Data Backfills
        logger.info("Backfilling session numbers...")
        campaigns = Campaign.query.all()
        for camp in campaigns:
            sessions = Session.query.filter_by(campaign_id=camp.id).order_by(Session.session_date).all()
            dated = [s for s in sessions if s.session_date is not None]
            undated = [s for s in sessions if s.session_date is None]

            for session_obj in undated:
                session_obj.session_number = 0

            for idx, session_obj in enumerate(dated, start=1):
                session_obj.session_number = idx
                logger.info(f"Updated: {camp.name} - {session_obj.original_filename} -> Session #{idx}")

        db.session.commit()
        config['last_migrated_version'] = APP_VERSION
        save_config(config)
        logger.info(f"Migration complete. Version stored as {APP_VERSION}.")

if __name__ == "__main__":
    run_migration()