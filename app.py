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
import threading
from collections import defaultdict, deque
import psutil
import traceback
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
app.config['CELERY_BROKER_URL'] = REDIS_URL
app.config['CELERY_RESULT_BACKEND'] = REDIS_URL

celery = Celery(app.name, broker=app.config['CELERY_BROKER_URL'])
celery.conf.update(app.config)

# OpenAIクライアント
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# 監視用のメトリクス保存
class MetricsCollector:
    def __init__(self):
        self.request_count = defaultdict(int)
        self.error_count = defaultdict(int)
        self.response_times = defaultdict(lambda: deque(maxlen=100))
        self.api_calls = defaultdict(int)
        self.start_time = datetime.now()
        
    def record_request(self, endpoint):
        self.request_count[endpoint] += 1
        
    def record_error(self, endpoint):
        self.error_count[endpoint] += 1
        
    def record_response_time(self, endpoint, duration):
        self.response_times[endpoint].append(duration)
        
    def record_api_call(self, api_name):
        self.api_calls[api_name] += 1
        
    def get_metrics(self):
        uptime = (datetime.now() - self.start_time).total_seconds()
        
        # 平均レスポンスタイムの計算
        avg_response_times = {}
        for endpoint, times in self.response_times.items():
            if times:
                avg_response_times[endpoint] = sum(times) / len(times)
        
        # システムリソース情報
        cpu_percent = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()
        
        return {
            "uptime_seconds": uptime,
            "request_counts": dict(self.request_count),
            "error_counts": dict(self.error_count),
            "average_response_times": avg_response_times,
            "api_calls": dict(self.api_calls),
            "system": {
                "cpu_percent": cpu_percent,
                "memory_percent": memory.percent,
                "memory_available_mb": memory.available / 1024 / 1024
            },
            "timestamp": datetime.now().isoformat()
        }

metrics = MetricsCollector()

# レート制限用のデコレーター
def rate_limit(max_calls=10, period=60):
    calls = defaultdict(lambda: deque())
    
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            user_id = request.form.get('user_id', request.args.get('user_id', 'default_user'))
            now = time.time()
            
            # 期限切れの呼び出しを削除
            while calls[user_id] and calls[user_id][0] < now - period:
                calls[user_id].popleft()
            
            # レート制限チェック
            if len(calls[user_id]) >= max_calls:
                return jsonify({"error": f"{period}秒間に{max_calls}回までしかリクエストできません"}), 429
            
            calls[user_id].append(now)
            return f(*args, **kwargs)
        return wrapper
    return decorator

# パフォーマンス監視用のデコレーター
def monitor_performance(endpoint_name):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            start_time = time.time()
            metrics.record_request(endpoint_name)
            
            try:
                result = f(*args, **kwargs)
                return result
            except Exception as e:
                metrics.record_error(endpoint_name)
                logger.error(f"Error in {endpoint_name}: {str(e)}\n{traceback.format_exc()}")
                raise
            finally:
                duration = time.time() - start_time
                metrics.record_response_time(endpoint_name, duration)
                logger.info(f"{endpoint_name} - Response time: {duration:.3f}s")
                
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
            
            # 監視ログテーブル
            conn.execute('''
            CREATE TABLE IF NOT EXISTS monitoring_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                metrics TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            ''')
            
            # エラーログテーブル
            conn.execute('''
            CREATE TABLE IF NOT EXISTS error_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint TEXT NOT NULL,
                error_message TEXT NOT NULL,
                stack_trace TEXT,
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
            conn.execute('CREATE INDEX IF NOT EXISTS idx_monitoring_timestamp ON monitoring_logs(timestamp DESC)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_error_timestamp ON error_logs(timestamp DESC)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_task_status_user ON task_status(user_id, created_at DESC)')
            
        conn.close()

# アプリケーション起動時にデータベースを初期化する
init_db()

# Celeryタスク: 画像解析の非同期処理
@celery.task(bind=True, max_retries=3)
def analyze_image_task(self, task_id, user_id, school_id, base64_image):
    """画像解析を非同期で実行するタスク"""
    try:
        # タスクステータスを更新
        update_task_status(task_id, 'processing')
        
        # GPT Vision APIで画像解析
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

        # API呼び出しの記録
        metrics.record_api_call('openai_vision')
        
        gpt_response = client.chat.completions.create(
            model="gpt-4.1 mini",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": prompt
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{base64_image}",
                                "detail": "auto"
                            }
                        }
                    ]
                }
            ],
            max_tokens=1500,
            temperature=0.7,
            timeout=60  # タイムアウトを60秒に延長
        )
        
        explanation_text = gpt_response.choices[0].message.content.strip()
        
        # データベースに保存
        conn = get_db_connection()
        with conn:
            conn.execute(
                "INSERT INTO history (user_id, school_id, image_base64, explanation, timestamp) VALUES (?, ?, ?, ?, ?)",
                (user_id, school_id, base64_image, explanation_text, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            )
        conn.close()
        
        # タスクステータスを完了に更新
        update_task_status(task_id, 'completed', result=explanation_text)
        
        # Redisにも結果を保存（TTL: 1時間）
        redis_client.setex(f"task_result:{task_id}", 3600, json.dumps({
            "status": "completed",
            "result": explanation_text
        }))
        
        logger.info(f"Successfully processed image for user: {user_id}, task: {task_id}")
        
        return {"success": True, "explanation": explanation_text}
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error in analyze_image_task: {error_msg}\n{traceback.format_exc()}")
        
        # エラーログをDBに保存
        try:
            conn = get_db_connection()
            with conn:
                conn.execute(
                    "INSERT INTO error_logs (endpoint, error_message, stack_trace) VALUES (?, ?, ?)",
                    ('analyze_image_task', error_msg, traceback.format_exc())
                )
            conn.close()
        except:
            pass
        
        # タスクステータスをエラーに更新
        update_task_status(task_id, 'failed', error_message=error_msg)
        
        # Redisにもエラーを保存
        redis_client.setex(f"task_result:{task_id}", 3600, json.dumps({
            "status": "failed",
            "error": error_msg
        }))
        
        # リトライ
        raise self.retry(exc=e, countdown=60)

def update_task_status(task_id, status, result=None, error_message=None):
    """タスクのステータスを更新"""
    try:
        conn = get_db_connection()
        with conn:
            if result:
                conn.execute(
                    "UPDATE task_status SET status = ?, result = ?, updated_at = ? WHERE task_id = ?",
                    (status, result, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), task_id)
                )
            elif error_message:
                conn.execute(
                    "UPDATE task_status SET status = ?, error_message = ?, updated_at = ? WHERE task_id = ?",
                    (status, error_message, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), task_id)
                )
            else:
                conn.execute(
                    "UPDATE task_status SET status = ?, updated_at = ? WHERE task_id = ?",
                    (status, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), task_id)
                )
        conn.close()
    except Exception as e:
        logger.error(f"Error updating task status: {str(e)}")

# 定期的なメトリクス保存
def save_metrics_periodically():
    while True:
        try:
            time.sleep(300)  # 5分ごと
            metrics_data = metrics.get_metrics()
            
            conn = get_db_connection()
            with conn:
                conn.execute(
                    "INSERT INTO monitoring_logs (metrics) VALUES (?)",
                    (json.dumps(metrics_data),)
                )
            conn.close()
            
            logger.info("Metrics saved to database")
        except Exception as e:
            logger.error(f"Error saving metrics: {str(e)}")

# バックグラウンドスレッドでメトリクス保存を開始
threading.Thread(target=save_metrics_periodically, daemon=True).start()

# ルートページ
@app.route('/')
@monitor_performance('index')
def index():
    return render_template('main.html')

# Service Worker
@app.route('/sw.js')
def service_worker():
    return send_from_directory('static', 'sw.js', mimetype='application/javascript')

# Manifest
@app.route('/manifest.json')
def manifest():
    return send_from_directory('static', 'manifest.json', mimetype='application/manifest+json')

# 静的ファイル配信
@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory('static', filename)

# 画像アップロード（非同期処理版）
@app.route('/upload', methods=['POST'])
@monitor_performance('upload')
@rate_limit(max_calls=5, period=60)  # 1分間に5回まで
def upload():
    try:
        # フォームデータ取得
        school_id = request.form.get('school_id', 'default_school')
        user_id = request.form.get('user_id', 'default_user')
        
        # バリデーション
        if 'file' not in request.files:
            return jsonify({"error": "ファイルがありません"}), 400
        
        file = request.files['file']
        if file.filename == '':
            return jsonify({"error": "ファイルが選択されていません"}), 400
        
        # ファイル形式の確認
        allowed_extensions = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
        if '.' in file.filename:
            ext = file.filename.rsplit('.', 1)[1].lower()
            if ext not in allowed_extensions:
                return jsonify({"error": f"許可されていないファイル形式です。{', '.join(allowed_extensions)}のみ対応しています"}), 400
        
        # 画像データを読み込み
        image_data = file.read()
        
        # ファイルサイズの再確認
        if len(image_data) > 16 * 1024 * 1024:
            return jsonify({"error": "ファイルサイズが大きすぎます"}), 413
        
        # base64エンコード
        base64_image = base64.b64encode(image_data).decode('utf-8')
        
        # タスクIDを生成
        task_id = str(uuid.uuid4())
        
        # タスクステータスをDBに保存
        conn = get_db_connection()
        with conn:
            conn.execute(
                "INSERT INTO task_status (task_id, user_id, status) VALUES (?, ?, ?)",
                (task_id, user_id, 'pending')
            )
        conn.close()
        
        # 非同期タスクを起動
        analyze_image_task.apply_async(
            args=[task_id, user_id, school_id, base64_image],
            task_id=task_id
        )
        
        logger.info(f"Task created for user: {user_id}, task_id: {task_id}")
        
        return jsonify({
            "success": True,
            "task_id": task_id,
            "message": "画像の解析を開始しました。結果の取得にはタスクIDを使用してください。"
        })
        
    except Exception as e:
        logger.error(f"Error in upload: {str(e)}\n{traceback.format_exc()}")
        
        # エラーログをDBに保存
        try:
            conn = get_db_connection()
            with conn:
                conn.execute(
                    "INSERT INTO error_logs (endpoint, error_message, stack_trace) VALUES (?, ?, ?)",
                    ('upload', str(e), traceback.format_exc())
                )
            conn.close()
        except:
            pass
        
        return jsonify({
            "error": "画像のアップロードに失敗しました。もう一度お試しください。",
            "details": str(e) if app.debug else None
        }), 500

# タスクステータス確認
@app.route('/task/<task_id>', methods=['GET'])
@monitor_performance('task_status')
def get_task_status(task_id):
    try:
        # まずRedisから確認（高速）
        redis_result = redis_client.get(f"task_result:{task_id}")
        if redis_result:
            return jsonify(json.loads(redis_result))
        
        # Redisになければデータベースから確認
        conn = get_db_connection()
        cursor = conn.execute(
            "SELECT * FROM task_status WHERE task_id = ?",
            (task_id,)
        )
        task = cursor.fetchone()
        conn.close()
        
        if not task:
            return jsonify({"error": "タスクが見つかりません"}), 404
        
        response = {
            "task_id": task['task_id'],
            "status": task['status'],
            "created_at": task['created_at'],
            "updated_at": task['updated_at']
        }
        
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
@monitor_performance('history')
@rate_limit(max_calls=20, period=60)
def get_history():
    try:
        user_id = request.args.get('user_id', 'default_user')
        limit = min(int(request.args.get('limit', 20)), 100)  # 最大100件まで
        offset = int(request.args.get('offset', 0))
        
        conn = get_db_connection()
        history_cursor = conn.execute(
            "SELECT * FROM history WHERE user_id = ? ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            (user_id, limit, offset)
        )
        history = [dict(row) for row in history_cursor.fetchall()]
        
        # 総件数も取得
        count_cursor = conn.execute(
            "SELECT COUNT(*) as total FROM history WHERE user_id = ?",
            (user_id,)
        )
        total_count = count_cursor.fetchone()['total']
        
        conn.close()
        
        return jsonify({
            "history": history,
            "total": total_count,
            "limit": limit,
            "offset": offset
        })
        
    except Exception as e:
        logger.error(f"Error in history: {str(e)}")
        return jsonify({"error": "履歴の取得に失敗しました"}), 500

# 監視ダッシュボード
@app.route('/monitoring')
@monitor_performance('monitoring')
def monitoring_dashboard():
    # 管理者認証（本番環境では適切な認証を実装してください）
    auth_token = request.headers.get('Authorization')
    expected_token = os.getenv('MONITORING_TOKEN', 'your-monitoring-token')
    
    if auth_token != f"Bearer {expected_token}":
        return jsonify({"error": "Unauthorized"}), 401
    
    return render_template('monitoring.html')

# 監視API - 現在のメトリクス
@app.route('/api/metrics/current', methods=['GET'])
@monitor_performance('metrics_current')
def get_current_metrics():
    # 管理者認証
    auth_token = request.headers.get('Authorization')
    expected_token = os.getenv('MONITORING_TOKEN', 'your-monitoring-token')
    
    if auth_token != f"Bearer {expected_token}":
        return jsonify({"error": "Unauthorized"}), 401
    
    return jsonify(metrics.get_metrics())

# 監視API - 過去のメトリクス
@app.route('/api/metrics/history', methods=['GET'])
@monitor_performance('metrics_history')
def get_metrics_history():
    # 管理者認証
    auth_token = request.headers.get('Authorization')
    expected_token = os.getenv('MONITORING_TOKEN', 'your-monitoring-token')
    
    if auth_token != f"Bearer {expected_token}":
        return jsonify({"error": "Unauthorized"}), 401
    
    try:
        hours = int(request.args.get('hours', 24))
        
        conn = get_db_connection()
        since = datetime.now() - timedelta(hours=hours)
        
        cursor = conn.execute(
            "SELECT metrics, timestamp FROM monitoring_logs WHERE timestamp > ? ORDER BY timestamp DESC",
            (since.strftime("%Y-%m-%d %H:%M:%S"),)
        )
        
        metrics_history = []
        for row in cursor.fetchall():
            metrics_data = json.loads(row['metrics'])
            metrics_data['timestamp'] = row['timestamp']
            metrics_history.append(metrics_data)
        
        conn.close()
        
        return jsonify(metrics_history)
        
    except Exception as e:
        logger.error(f"Error in metrics history: {str(e)}")
        return jsonify({"error": "メトリクス履歴の取得に失敗しました"}), 500

# 監視API - エラーログ
@app.route('/api/errors', methods=['GET'])
@monitor_performance('errors')
def get_error_logs():
    # 管理者認証
    auth_token = request.headers.get('Authorization')
    expected_token = os.getenv('MONITORING_TOKEN', 'your-monitoring-token')
    
    if auth_token != f"Bearer {expected_token}":
        return jsonify({"error": "Unauthorized"}), 401
    
    try:
        limit = min(int(request.args.get('limit', 50)), 200)
        
        conn = get_db_connection()
        cursor = conn.execute(
            "SELECT * FROM error_logs ORDER BY timestamp DESC LIMIT ?",
            (limit,)
        )
        
        errors = [dict(row) for row in cursor.fetchall()]
        conn.close()
        
        return jsonify(errors)
        
    except Exception as e:
        logger.error(f"Error in error logs: {str(e)}")
        return jsonify({"error": "エラーログの取得に失敗しました"}), 500

# ヘルスチェック（詳細版）
@app.route('/health', methods=['GET'])
@monitor_performance('health')
def health_check():
    try:
        # データベース接続確認
        conn = get_db_connection()
        conn.execute("SELECT 1")
        conn.close()
        db_status = "healthy"
    except:
        db_status = "unhealthy"
    
    # Redis接続確認
    try:
        redis_client.ping()
        redis_status = "healthy"
    except:
        redis_status = "unhealthy"
    
    # OpenAI API確認（環境変数のみチェック）
    api_status = "configured" if os.getenv("OPENAI_API_KEY") else "not configured"
    
    # 現在のメトリクス
    current_metrics = metrics.get_metrics()
    
    overall_status = "healthy"
    if db_status == "unhealthy" or redis_status == "unhealthy":
        overall_status = "degraded"
    
    return jsonify({
        "status": overall_status,
        "message": "勉強サポートアプリは正常に動作しています",
        "components": {
            "database": db_status,
            "redis": redis_status,
            "openai_api": api_status,
            "uptime_seconds": current_metrics["uptime_seconds"]
        },
        "timestamp": datetime.now().isoformat()
    })

# データベースのクリーンアップ（古いデータの削除）
@app.route('/api/cleanup', methods=['POST'])
def cleanup_old_data():
    # 管理者認証
    auth_token = request.headers.get('Authorization')
    expected_token = os.getenv('MONITORING_TOKEN', 'your-monitoring-token')
    
    if auth_token != f"Bearer {expected_token}":
        return jsonify({"error": "Unauthorized"}), 401
    
    try:
        days = int(request.json.get('days', 30))
        cutoff_date = datetime.now() - timedelta(days=days)
        
        conn = get_db_connection()
        with conn:
            # 古い履歴を削除
            history_result = conn.execute(
                "DELETE FROM history WHERE timestamp < ?",
                (cutoff_date.strftime("%Y-%m-%d %H:%M:%S"),)
            )
            
            # 古い監視ログを削除
            monitoring_result = conn.execute(
                "DELETE FROM monitoring_logs WHERE timestamp < ?",
                (cutoff_date.strftime("%Y-%m-%d %H:%M:%S"),)
            )
            
            # 古いエラーログを削除
            error_result = conn.execute(
                "DELETE FROM error_logs WHERE timestamp < ?",
                (cutoff_date.strftime("%Y-%m-%d %H:%M:%S"),)
            )
            
            # 古いタスクステータスを削除
            task_result = conn.execute(
                "DELETE FROM task_status WHERE created_at < ?",
                (cutoff_date.strftime("%Y-%m-%d %H:%M:%S"),)
            )
        
        conn.close()
        
        return jsonify({
            "success": True,
            "deleted": {
                "history": history_result.rowcount,
                "monitoring_logs": monitoring_result.rowcount,
                "error_logs": error_result.rowcount,
                "task_status": task_result.rowcount
            }
        })
        
    except Exception as e:
        logger.error(f"Error in cleanup: {str(e)}")
        return jsonify({"error": "クリーンアップに失敗しました"}), 500

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
    # 必要なディレクトリの作成
    os.makedirs('static', exist_ok=True)
    os.makedirs('templates', exist_ok=True)
    
    # 開発用サーバー起動
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)