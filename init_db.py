import os
from app import app, init_db

print("Starting database initialization...")

# Flaskのアプリケーションコンテキスト内で実行する
with app.app_context():
    # ディレクトリが存在することを確認・作成するロジックを削除します。
    # 永続ディスクがマウントされていれば、/var/data は既に存在し、書き込み可能であるべきです。
    
    print("Initializing the database tables...")
    init_db()
    print("Database initialization complete.")