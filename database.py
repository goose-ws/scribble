import os
import urllib.parse
from flask_sqlalchemy import SQLAlchemy
from config import load_config

db = SQLAlchemy()

def init_db(app):
    config = load_config()
    
    db_type = config.get('db_type', 'sqlite')
    
    if db_type == 'sqlite':
        # Default internal path
        db_uri = 'sqlite:////data/scribble.db'
    
    elif db_type == 'postgres':
        # postgresql://user:password@host/dbname
        user = config.get('db_username')
        # URL encode the password to handle special chars like @ safely
        pw = urllib.parse.quote_plus(config.get('db_password'))
        host = config.get('db_address')
        dbname = config.get('db_name')
        
        db_uri = f"postgresql://{user}:{pw}@{host}/{dbname}"
        
    elif db_type == 'mariadb':
        # mysql+pymysql://user:password@host/dbname
        user = config.get('db_username')
        # URL encode the password to handle special chars like @ safely
        pw = urllib.parse.quote_plus(config.get('db_password'))
        host = config.get('db_address')
        dbname = config.get('db_name')
        
        db_uri = f"mysql+pymysql://{user}:{pw}@{host}/{dbname}"
        
    else:
        # Fallback
        db_uri = 'sqlite:////data/scribble.db'
    
    app.config['SQLALCHEMY_DATABASE_URI'] = db_uri
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    
    db.init_app(app)
    
    with app.app_context():
        # This creates tables if they don't exist
        db.create_all()