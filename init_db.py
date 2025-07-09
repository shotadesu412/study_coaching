import os
from app import app, init_db

print("Starting database initialization...")

# Flaskのアプリケーションコンテキスト内で実行する
with app.app_context():
    print("Initializing the database tables on PostgreSQL...")
    init_db()
    print("Database initialization complete.")
