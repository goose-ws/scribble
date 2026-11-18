#!/usr/bin/env python3
"""
Scribble Password Reset Utility
Usage:
    docker exec -it <container> python /app/reset_password.py
    docker exec -it <container> python /app/reset_password.py <new_password>
"""

import sys
import json
import os
import getpass

CONFIG_PATH = os.environ.get('SCRIBBLE_CONFIG', '/data/config.json')

def main():
    # --- Load config ---
    if not os.path.exists(CONFIG_PATH):
        print(f"ERROR: Config file not found at {CONFIG_PATH}")
        print("Set the SCRIBBLE_CONFIG env var if your config is elsewhere.")
        sys.exit(1)

    try:
        with open(CONFIG_PATH, 'r') as f:
            config = json.load(f)
    except Exception as e:
        print(f"ERROR: Could not read config.json: {e}")
        sys.exit(1)

    # --- Get new password ---
    if len(sys.argv) >= 2:
        new_password = sys.argv[1]
        print("Using password supplied as argument.")
    else:
        print("Scribble Password Reset")
        print("-" * 30)
        try:
            new_password = getpass.getpass("Enter new password: ")
            confirm     = getpass.getpass("Confirm new password: ")
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            sys.exit(0)

        if new_password != confirm:
            print("ERROR: Passwords do not match.")
            sys.exit(1)

    if not new_password:
        print("ERROR: Password cannot be empty.")
        sys.exit(1)

    # --- Hash with bcrypt ---
    try:
        import bcrypt
    except ImportError:
        print("ERROR: bcrypt is not installed. Run: pip install bcrypt")
        sys.exit(1)

    hashed = bcrypt.hashpw(new_password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

    # --- Save ---
    config['webui_password'] = hashed
    try:
        with open(CONFIG_PATH, 'w') as f:
            json.dump(config, f, indent=4)
    except Exception as e:
        print(f"ERROR: Could not write config.json: {e}")
        sys.exit(1)

    print("Password reset successfully.")
    print("Restart the container (or wait for config cache to refresh) before logging in.")

if __name__ == '__main__':
    main()
