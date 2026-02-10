# migrate_sessions.py
import logging
from sqlalchemy import text
from app import app, db
from models import Session, Campaign

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("migration")

def run_migration():
    with app.app_context():
        # 1. Add Column (Safe for SQLite & MariaDB)
        try:
            with db.engine.connect() as conn:
                # Check if column exists first (naive check)
                try:
                    # Try selecting the column. If it fails, we need to add it.
                    conn.execute(text("SELECT session_number FROM session LIMIT 1"))
                    logger.info("Column 'session_number' already exists.")
                except Exception:
                    logger.info("Adding 'session_number' column...")
                    conn.execute(text("ALTER TABLE session ADD COLUMN session_number INTEGER DEFAULT 0"))
                    conn.commit()
                    
                try:
                    # Check if column exists
                    conn.execute(text("SELECT is_default FROM campaign LIMIT 1"))
                    logger.info("Column 'is_default' already exists.")
                except Exception:
                    logger.info("Adding 'is_default' column to Campaign...")
                    conn.execute(text("ALTER TABLE campaign ADD COLUMN is_default BOOLEAN DEFAULT 0"))
                    conn.commit()
        except Exception as e:
            logger.error(f"Error checking/adding column: {e}")

        # 2. Backfill Session Numbers
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