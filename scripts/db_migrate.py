import os
import sys
import json
import getpass
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.orm import sessionmaker
from config import load_config

# Import models to register them with SQLAlchemy metadata
from app import app
from database import db
from models import Campaign, Session, Job, Transcript, LLMLog, DiscordLog

def get_db_uri(db_type, address=None, name=None, user=None, password=None):
    if db_type == 'sqlite':
        return 'sqlite:////data/scribble.db'
    elif db_type == 'postgres':
        return f"postgresql://{user}:{password}@{address}/{name}"
    elif db_type == 'mariadb':
        return f"mysql+pymysql://{user}:{password}@{address}/{name}"
    else:
        raise ValueError("Unknown DB Type")

def prompt_for_dest(current_type):
    print("\n--- Destination Database ---")
    options = ['sqlite', 'postgres', 'mariadb']
    if current_type in options:
        options.remove(current_type)
    
    print(f"Current Database: {current_type}")
    print("Migrate to:")
    for i, opt in enumerate(options):
        print(f"  {i+1}) {opt}")
    
    choice = input("Select destination (number): ")
    try:
        dest_type = options[int(choice)-1]
    except:
        print("Invalid selection.")
        sys.exit(1)
        
    if dest_type == 'sqlite':
        return 'sqlite', None, None, None, None
    
    print(f"\n--- {dest_type.upper()} Configuration ---")
    address = input("Address (host:port) [e.g. 192.168.1.5:5432]: ")
    name = input("Database Name [scribble]: ") or "scribble"
    user = input("Username: ")
    password = getpass.getpass("Password: ")
    
    return dest_type, address, name, user, password

def migrate():
    # 1. Load Source Config
    config = load_config()
    src_type = config.get('db_type', 'sqlite')
    
    # Construct Source URI
    if src_type == 'sqlite':
        src_uri = 'sqlite:////data/scribble.db'
    else:
        src_uri = get_db_uri(src_type, config.get('db_address'), config.get('db_name'), config.get('db_username'), config.get('db_password'))
    
    print(f"Loaded Source Config: {src_type}")
    
    # 2. Get Destination Config
    dest_type, d_addr, d_name, d_user, d_pass = prompt_for_dest(src_type)
    dest_uri = get_db_uri(dest_type, d_addr, d_name, d_user, d_pass)
    
    # 3. Setup Engines
    print("\nConnecting to databases...")
    try:
        src_engine = create_engine(src_uri)
        dest_engine = create_engine(dest_uri)
        
        # Test Connections
        with src_engine.connect() as conn: pass
        with dest_engine.connect() as conn: pass
        print("Connections successful.")
    except Exception as e:
        print(f"Connection Error: {e}")
        sys.exit(1)

    # 4. Create Schema on Destination
    print("Creating schema on destination...")
    # We use the metadata from the imported 'db' object which has all our models
    db.metadata.create_all(dest_engine)

    # 5. Data Migration
    # Order matters for Foreign Keys!
    # Campaign -> Session -> [Job, Transcript]
    tables = [
        Campaign,
        Session, 
        Job, 
        Transcript,
        LLMLog, 
        DiscordLog
    ]
    
    SessionSrc = sessionmaker(bind=src_engine)
    SessionDest = sessionmaker(bind=dest_engine)
    
    src_session = SessionSrc()
    dest_session = SessionDest()
    
    try:
        for model in tables:
            table_name = model.__tablename__
            print(f"Migrating {table_name}...", end=" ", flush=True)
            
            # Fetch all records
            records = src_session.query(model).all()
            count = 0
            
            for record in records:
                # Detach record from source session to allow inserting into dest
                src_session.expunge(record)
                
                # Merge checks primary key and inserts/updates
                # We use merge to be safe, though add() would work for fresh DB
                dest_session.merge(record)
                count += 1
                
            dest_session.commit()
            print(f"Done ({count} records).")
            
        # 6. Reset Sequences (Postgres Only)
        # If we insert ID 5 explicitly, Postgres auto-increment counter might still be at 1.
        if dest_type == 'postgres':
            print("Resetting Postgres sequences...")
            with dest_engine.connect() as conn:
                for model in tables:
                    table_name = model.__tablename__
                    # Assumes standard 'id' column
                    sql = text(f"SELECT setval(pg_get_serial_sequence('{table_name}', 'id'), coalesce(max(id),0) + 1, false) FROM {table_name};")
                    try:
                        conn.execute(sql)
                        conn.commit()
                    except Exception as e:
                        print(f"Warning resetting sequence for {table_name}: {e}")

        print("\nSUCCESS! Migration complete.")
        print("Don't forget to update your settings in the WebUI or config.json to point to the new database!")

    except Exception as e:
        print(f"\nMIGRATION FAILED: {e}")
        dest_session.rollback()
    finally:
        src_session.close()
        dest_session.close()

if __name__ == "__main__":
    # Ensure we are in app context so db.metadata is populated
    with app.app_context():
        migrate()