from flask import Flask, request, jsonify, render_template, send_from_directory
from openai import OpenAI
import os
import base64
from datetime import datetime, timedelta
import json
from werkzeug.utils import secure_filename
import sqlite3
import logging
from functools import wraps
import time
import uuid
from celery import Celery
import redis

# ログ設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Flaskアプリ
app = Flask(__name__, static_folder='static', template_folder='templates')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'your-secret-key-here')

# Redis設定
REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
redis_client = redis.from_url(REDIS_URL, decode_responses=True)

# Celery設定
celery = Celery(
    app.name,
    broker=REDIS_URL,
    result_backend=REDIS_URL
)
celery.conf.update(
    broker_connection_retry_on_startup=True,
)

# OpenAIクライアント
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# レート制限用のデコレーター
def rate_limit(max_calls=10, period=60):
    calls = {}
    
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            user_id = request.form.get('user_id', request.args.get('user_id', 'default_user'))
            now = time.time()
            
            # ユーザーごとの呼び出し履歴を初期化
            if user_id not in calls:
                calls[user_id] = []
            
            # 期限切れの呼び出しを削除
            calls[user_id] = [call_time for call_time in calls[user_id] if call_time > now - period]
            
            # レート制限チェック
            if len(calls[user_id]) >= max_calls:
                return jsonify({"error": f"{period}秒間に{max_calls}回までしかリクエストできません"}), 429
            
            calls[user_id].append(now)
            return f(*args, **kwargs)
        return wrapper
    return decorator

# データベース関連
DATABASE_PATH = os.path.join('/var/data', 'history.db')

def get_db_connection():
    """データベース接続を取得する"""
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """データベースのテーブルを初期化"""
    with app.app_context():
        conn = get_db_connection()
        with conn:
            # 履歴テーブル
            conn.execute('''
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                school_id TEXT,
                image_base64 TEXT NOT NULL,
                explanation TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            ''')
            
            # タスクステータステーブル
            conn.execute('''
            CREATE TABLE IF NOT EXISTS task_status (
                task_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                status TEXT NOT NULL,
                result TEXT,
                error_message TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            ''')
            
            # インデックスの作成
            conn.execute('CREATE INDEX IF NOT EXISTS idx_history_user_timestamp ON history(user_id, timestamp DESC)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_task_status_user ON task_status(user_id, created_at DESC)')
            
        conn.close()

# Celeryタスク: 画像解析の非同期処理
@celery.task(bind=True, max_retries=3)
def analyze_image_task(self, task_id, user_id, school_id, base64_image):
    """画像解析を非同期で実行するタスク"""
    try:
        update_task_status(task_id, 'processing')
        
        prompt = """
        この画像に写っている問題を分析して、中学生から高校生の学習者に適した教育的な指導をしてください。

        【絶対に守ること】
        - 計算しなくていいから、解き方の手順だけ教えてください
        - 日本の中学生や高校生の知識の範囲内で説明してください

        【数式の表記ルール（MathJax対応）】
        インライン数式は $ $ で囲む、ディスプレイ数式は $$ $$ で囲む

        - 分数：$\\frac{分子}{分母}$ 例：$\\frac{x}{2}$
        - 累乗：$x^2$, $x^{10}$, $a^{n+1}$
        - 平方根：$\\sqrt{2}$, $\\sqrt{x+1}$, $\\sqrt[3]{8}$（3乗根）
        - ギリシャ文字：$\\alpha$, $\\beta$, $\\gamma$, $\\theta$, $\\pi$, $\\omega$
        - 三角関数：$\\sin \\theta$, $\\cos \\theta$, $\\tan \\theta$
        - 対数：$\\log_2 x$, $\\ln x$
        - 総和：$\\sum_{i=1}^{n} i^2$
        - 積分：$\\int_0^1 x^2 dx$
        - ベクトル：$\\vec{AB}$ または $\\overrightarrow{AB}$
        - 極限：$\\lim_{x \\to \\infty} \\frac{1}{x}$
        - 行列：$\\begin{pmatrix} a & b \\\\ c & d \\end{pmatrix}$
        - 不等号：$\\leq$, $\\geq$, $\\neq$

        【表示形式】
        - 考え方と手順のみ表示
        - 重要な数式は $$...$$ で中央揃え表示

        まず画像の内容を詳しく分析し、問題文を正確に読み取ってから指導を開始してください。
        """
        
        gpt_response = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/jpeg;base64,{base64_image}",
                        "detail": "auto"
                    }}
                ]}
            ],
            max_tokens=1500,
            temperature=0.7,
            timeout=60
        )
        
        explanation_text = gpt_response.choices[0].message.content.strip()
        
        conn = get_db_connection()
        with conn:
            conn.execute(
                "INSERT INTO history (user_id, school_id, image_base64, explanation, timestamp) VALUES (?, ?, ?, ?, ?)",
                (user_id, school_id, base64_image, explanation_text, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            )
        conn.close()
        
        update_task_status(task_id, 'completed', result=explanation_text)
        
        redis_client.setex(f"task_result:{task_id}", 3600, json.dumps({
            "status": "completed",
            "result": explanation_text
        }))
        
        logger.info(f"Successfully processed image for user: {user_id}, task: {task_id}")
        return {"success": True, "explanation": explanation_text}
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error in analyze_image_task: {error_msg}")
        update_task_status(task_id, 'failed', error_message=error_msg)
        redis_client.setex(f"task_result:{task_id}", 3600, json.dumps({
            "status": "failed",
            "error": error_msg
        }))
        raise self.retry(exc=e, countdown=60)

def update_task_status(task_id, status, result=None, error_message=None):
    """タスクのステータスを更新"""
    try:
        conn = get_db_connection()
        with conn:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if result:
                conn.execute(
                    "UPDATE task_status SET status = ?, result = ?, updated_at = ? WHERE task_id = ?",
                    (status, result, now_str, task_id)
                )
            elif error_message:
                conn.execute(
                    "UPDATE task_status SET status = ?, error_message = ?, updated_at = ? WHERE task_id = ?",
                    (status, error_message, now_str, task_id)
                )
            else:
                conn.execute(
                    "UPDATE task_status SET status = ?, updated_at = ? WHERE task_id = ?",
                    (status, now_str, task_id)
                )
        conn.close()
    except Exception as e:
        logger.error(f"Error updating task status: {str(e)}")

# ルートページ
@app.route('/')
def index():
    return render_template('main.html')

# Service Worker, Manifest, 静的ファイル
@app.route('/sw.js')
def service_worker():
    return send_from_directory('static', 'sw.js', mimetype='application/javascript')

@app.route('/manifest.json')
def manifest():
    return send_from_directory('static', 'manifest.json', mimetype='application/manifest+json')

@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory('static', filename)

# 画像アップロード
@app.route('/upload', methods=['POST'])
@rate_limit(max_calls=5, period=60)
def upload():
    try:
        school_id = request.form.get('school_id', 'default_school')
        user_id = request.form.get('user_id', 'default_user')
        
        if 'file' not in request.files:
            return jsonify({"error": "ファイルがありません"}), 400
        
        file = request.files['file']
        if file.filename == '':
            return jsonify({"error": "ファイルが選択されていません"}), 400
        
        allowed_extensions = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
        if '.' in file.filename and file.filename.rsplit('.', 1)[1].lower() not in allowed_extensions:
            return jsonify({"error": f"許可されていないファイル形式です。"}), 400
        
        image_data = file.read()
        if len(image_data) > 16 * 1024 * 1024:
            return jsonify({"error": "ファイルサイズが大きすぎます"}), 413
        
        base64_image = base64.b64encode(image_data).decode('utf-8')
        task_id = str(uuid.uuid4())
        
        conn = get_db_connection()
        with conn:
            conn.execute(
                "INSERT INTO task_status (task_id, user_id, status) VALUES (?, ?, ?)",
                (task_id, user_id, 'pending')
            )
        conn.close()
        
        analyze_image_task.apply_async(args=[task_id, user_id, school_id, base64_image], task_id=task_id)
        
        logger.info(f"Task created for user: {user_id}, task_id: {task_id}")
        
        return jsonify({
            "success": True,
            "task_id": task_id,
            "message": "画像の解析を開始しました。結果の取得にはタスクIDを使用してください。"
        })
        
    except Exception as e:
        logger.error(f"Error in upload: {str(e)}")
        return jsonify({"error": "画像のアップロードに失敗しました。"}), 500

# タスクステータス確認
@app.route('/task/<task_id>', methods=['GET'])
def get_task_status(task_id):
    try:
        redis_result = redis_client.get(f"task_result:{task_id}")
        if redis_result:
            return jsonify(json.loads(redis_result))
        
        conn = get_db_connection()
        task = conn.execute("SELECT * FROM task_status WHERE task_id = ?", (task_id,)).fetchone()
        conn.close()
        
        if not task:
            return jsonify({"error": "タスクが見つかりません"}), 404
        
        response = dict(task)
        if task['status'] == 'completed' and task['result']:
            response['result'] = task['result']
        elif task['status'] == 'failed' and task['error_message']:
            response['error'] = task['error_message']
        
        return jsonify(response)
        
    except Exception as e:
        logger.error(f"Error in get_task_status: {str(e)}")
        return jsonify({"error": "タスクステータスの取得に失敗しました"}), 500

# 履歴取得
@app.route('/history', methods=['GET'])
@rate_limit(max_calls=20, period=60)
def get_history():
    try:
        user_id = request.args.get('user_id', 'default_user')
        limit = min(int(request.args.get('limit', 20)), 100)
        offset = int(request.args.get('offset', 0))
        
        conn = get_db_connection()
        history_rows = conn.execute(
            "SELECT * FROM history WHERE user_id = ? ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            (user_id, limit, offset)
        ).fetchall()
        history = [dict(row) for row in history_rows]
        
        total_count = conn.execute("SELECT COUNT(*) as total FROM history WHERE user_id = ?", (user_id,)).fetchone()['total']
        conn.close()
        
        return jsonify({"history": history, "total": total_count, "limit": limit, "offset": offset})
        
    except Exception as e:
        logger.error(f"Error in history: {str(e)}")
        return jsonify({"error": "履歴の取得に失敗しました"}), 500

# ヘルスチェック
@app.route('/health', methods=['GET'])
def health_check():
    try:
        conn = get_db_connection()
        conn.execute("SELECT 1")
        conn.close()
        db_status = "healthy"
    except:
        db_status = "unhealthy"
    
    try:
        redis_client.ping()
        redis_status = "healthy"
    except:
        redis_status = "unhealthy"
        
    status = "healthy" if db_status == "healthy" and redis_status == "healthy" else "degraded"
    
    return jsonify({
        "status": status,
        "components": {"database": db_status, "redis": redis_status},
        "timestamp": datetime.now().isoformat()
    })

# エラーハンドラー
@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({"error": "ファイルサイズが大きすぎます。16MB以下のファイルを選択してください。"}), 413

@app.errorhandler(429)
def too_many_requests(error):
    return jsonify({"error": "リクエストが多すぎます。しばらく待ってから再度お試しください。"}), 429

@app.errorhandler(500)
def internal_error(error):
    return jsonify({"error": "サーバーエラーが発生しました。しばらくしてからもう一度お試しください。"}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)