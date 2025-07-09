# init_db.py (改良版)

import os
import time
import logging
from app import app, init_db

# psycopg2.OperationalError をインポートして、特定のエラーを捕捉する
try:
    from psycopg2 import OperationalError
except ImportError:
    # psycopg2がインストールされていない場合のフォールバック
    OperationalError = Exception

# ログ設定
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def wait_for_db_and_initialize():
    """
    データベースが利用可能になるまで待機し、その後テーブルを初期化する。
    """
    max_retries = 15
    retry_interval = 5  # 5秒ごと

    logger.info("データベースの準備を待っています...")

    for attempt in range(max_retries):
        try:
            # appコンテキスト内でデータベース初期化を実行
            with app.app_context():
                init_db()
            logger.info("データベースの初期化に成功しました。")
            return # 成功したら関数を抜ける
        except OperationalError as e:
            logger.warning(f"データベース接続に失敗しました (試行 {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                logger.info(f"{retry_interval}秒後に再試行します。")
                time.sleep(retry_interval)
            else:
                logger.error("データベースへの接続に何度も失敗しました。デプロイを中止します。")
                # ゼロ以外のステータスコードで終了し、Renderにデプロイの失敗を伝える
                exit(1)
        except Exception as e:
            logger.error(f"予期せぬエラーが発生しました: {e}")
            exit(1)

if __name__ == "__main__":
    wait_for_db_and_initialize()