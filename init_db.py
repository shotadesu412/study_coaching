# この内容で新規ファイルを作成してください

import os
from app import app, init_db

print("Starting database initialization...")

# Flaskのアプリケーションコンテキスト内で実行する
with app.app_context():
    # 永続ディスクのディレクトリが存在することを確認・作成する
    db_dir = '/var/data'
    if not os.path.exists(db_dir):
        print(f"Directory {db_dir} not found, creating it.")
        os.makedirs(db_dir)
    
    print("Initializing the database tables...")
    init_db()
    print("Database initialization complete.")