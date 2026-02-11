# migrate_sessions.py
import logging
from sqlalchemy import text
from app import app, db
from models import Session, Campaign

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("migration")

def run_migration():
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

                # --- DiscordLog: session_id ---
                try:
                    conn.execute(text("SELECT session_id FROM discord_log LIMIT 1"))
                    logger.info("Column 'session_id' already exists in DiscordLog.")
                except Exception:
                    logger.info("Adding 'session_id' column to DiscordLog...")
                    # 1. Add the column
                    conn.execute(text("ALTER TABLE discord_log ADD COLUMN session_id INTEGER NULL"))
                    
                    # 2. Add the Foreign Key
                    # We wrap this in a try/except because if the table is MyISAM (rare) or 
                    # there are data inconsistencies, it might fail, but we still want the column.
                    try:
                        logger.info("Adding FK constraint to DiscordLog...")
                        conn.execute(text("ALTER TABLE discord_log ADD CONSTRAINT fk_discord_log_session FOREIGN KEY (session_id) REFERENCES session(id)"))
                    except Exception as e:
                        logger.warning(f"Could not add FK constraint (this is optional, app will still work): {e}")
                    
                    conn.commit()

        except Exception as e:
            logger.error(f"Error checking/adding columns: {e}")

        # 2. Data Backfills
        logger.info("Backfilling session numbers...")
        campaigns = Campaign.query.all()
        
        for camp in campaigns:
            # Get sessions ordered by date
            sessions = Session.query.filter_by(campaign_id=camp.id).order_by(Session.session_date).all()
            
            for idx, session_obj in enumerate(sessions):
                # Start at 1 (User can edit to 0 later if desired)
                new_num = idx + 1
                if session_obj.session_number != new_num:
                    session_obj.session_number = new_num
                    logger.info(f"Updated: {camp.name} - {session_obj.original_filename} -> Session #{new_num}")
        
        db.session.commit()
        logger.info("Migration Complete.")

if __name__ == "__main__":
    run_migration()