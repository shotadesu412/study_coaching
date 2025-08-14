from flask import Flask, request, jsonify, render_template, send_from_directory
from openai import OpenAI
import os
import base64
from datetime import datetime
import json
import uuid
import logging
from functools import wraps
import time
from celery import Celery
import redis
import psycopg2
import psycopg2.extras

# ... (既存のコードは変更なし) ...
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
            
            if user_id not in calls:
                calls[user_id] = []
            
            calls[user_id] = [call_time for call_time in calls[user_id] if call_time > now - period]
            
            if len(calls[user_id]) >= max_calls:
                return jsonify({"error": f"{period}秒間に{max_calls}回までしかリクエストできません"}), 429
            
            calls[user_id].append(now)
            return f(*args, **kwargs)
        return wrapper
    return decorator

# データベース関連
def get_db_connection():
    """データベース接続を取得する"""
    database_url = os.getenv('DATABASE_URL')
    if not database_url:
        raise ValueError("DATABASE_URL environment variable is not set.")
    conn = psycopg2.connect(database_url)
    return conn

def init_db():
    """データベースのテーブルを初期化"""
    with app.app_context():
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute('''
                    CREATE TABLE IF NOT EXISTS history (
                        id SERIAL PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
                        school_id VARCHAR(255),
                        image_base64 TEXT NOT NULL,
                        explanation TEXT NOT NULL,
                        timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    )
                    ''')
                    cur.execute('''
                    CREATE TABLE IF NOT EXISTS task_status (
                        task_id VARCHAR(255) PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
                        status VARCHAR(50) NOT NULL,
                        result TEXT,
                        error_message TEXT,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    )
                    ''')
                    cur.execute('CREATE INDEX IF NOT EXISTS idx_history_user_timestamp ON history(user_id, timestamp DESC)')
                    cur.execute('CREATE INDEX IF NOT EXISTS idx_task_status_user ON task_status(user_id, created_at DESC)')
            
            logger.info("Database tables initialized or verified successfully.")
        except Exception as e:
            logger.error(f"Error initializing database: {e}")
            raise

# Celeryタスク: 画像解析の非同期処理
@celery.task(bind=True, max_retries=3)
def analyze_image_task(self, task_id, user_id, school_id, base64_image, grade_level='junior-high'):
    """画像解析を非同期で実行するタスク"""
    try:
        update_task_status(task_id, 'processing')
        
        # ▼▼▼ 学年に応じてプロンプトを切り替える ▼▼▼
        if grade_level == 'high-school':
            prompt = """
            あなたは優秀な高校教師です。この画像に写っている問題を分析して、高校生の学習者に適した教育的な指導をしてください。

            【絶対に守ること】
            - 計算しなくていいから、解き方の手順だけ教えてください
            - 日本の高校生の知識の範囲内で、専門用語も適宜使用して説明してください

            【表示形式】
            - 考え方と手順のみ表示
            - 重要な数式は $$...$$ で中央揃え表示
            - 式に番号を振ってください

            まず画像の内容を詳しく分析し、問題文を正確に読み取ってから指導を開始してください。
            """
        else:  # デフォルトは中学生向け
            prompt = """
            あなたは優秀な中学教師です。この画像に写っている問題を分析して、中学生の学習者に適した教育的な指導をしてください。

            【絶対に守ること】
            - 計算しなくていいから、解き方の手順だけ教えてください
            - 日本の中学生の知識の範囲内で、専門用語は避け、平易な言葉で説明してください
            - できるだけで細かく、わかりやすく説明してください

            【表示形式】
            - 考え方と手順のみ表示
            - 重要な数式は $$...$$ で中央揃え表示
            - 式に番号を振ってください

            まず画像の内容を詳しく分析し、問題文を正確に読み取ってから指導を開始してください。
            """
        # ▲▲▲ プロンプトの切り替えここまで ▲▲▲
        
        gpt_response = client.chat.completions.create(
            model="gpt-5",
            messages=[
                {"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/jpeg;base64,{base64_image}",
                        "detail": "auto"
                    }}
                ]}
            ],
            max_tokens=2000,
            temperature=0.7,
            timeout=60
        )
        
        explanation_text = gpt_response.choices[0].message.content.strip()
        
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO history (user_id, school_id, image_base64, explanation, timestamp) VALUES (%s, %s, %s, %s, %s)",
                    (user_id, school_id, base64_image, explanation_text, datetime.now())
                )
        
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
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                now = datetime.now()
                if result:
                    cur.execute(
                        "UPDATE task_status SET status = %s, result = %s, updated_at = %s WHERE task_id = %s",
                        (status, result, now, task_id)
                    )
                elif error_message:
                    cur.execute(
                        "UPDATE task_status SET status = %s, error_message = %s, updated_at = %s WHERE task_id = %s",
                        (status, error_message, now, task_id)
                    )
                else:
                    cur.execute(
                        "UPDATE task_status SET status = %s, updated_at = %s WHERE task_id = %s",
                        (status, now, task_id)
                    )
    except Exception as e:
        logger.error(f"Error updating task status: {str(e)}")

# ... (既存のルート、アップロード、タスク確認、履歴取得、ヘルスチェックなどの関数は変更なし) ...
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
        # ▼▼▼ 学年情報を受け取る ▼▼▼
        grade_level = request.form.get('grade_level', 'junior-high') 
        
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
        
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO task_status (task_id, user_id, status, created_at, updated_at) VALUES (%s, %s, %s, %s, %s)",
                    (task_id, user_id, 'pending', datetime.now(), datetime.now())
                )
        
        # ▼▼▼ Celeryタスクに学年情報を渡す ▼▼▼
        analyze_image_task.apply_async(args=[task_id, user_id, school_id, base64_image, grade_level], task_id=task_id)
        
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
        
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("SELECT * FROM task_status WHERE task_id = %s", (task_id,))
                task = cur.fetchone()
        
        if not task:
            return jsonify({"error": "タスクが見つかりません"}), 404
        
        response = dict(task)
        for key, value in response.items():
            if isinstance(value, datetime):
                response[key] = value.isoformat()

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
        
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(
                    "SELECT * FROM history WHERE user_id = %s ORDER BY timestamp DESC LIMIT %s OFFSET %s",
                    (user_id, limit, offset)
                )
                history_rows = cur.fetchall()
                history = [dict(row) for row in history_rows]

                cur.execute("SELECT COUNT(*) as total FROM history WHERE user_id = %s", (user_id,))
                total_count = cur.fetchone()['total']

        for item in history:
            item['timestamp'] = item['timestamp'].isoformat()
        
        return jsonify({"history": history, "total": total_count, "limit": limit, "offset": offset})
        
    except Exception as e:
        logger.error(f"Error in history: {str(e)}")
        return jsonify({"error": "履歴の取得に失敗しました"}), 500

# ヘルスチェック
@app.route('/health', methods=['GET'])
def health_check():
    db_status = "unhealthy"
    redis_status = "unhealthy"
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        db_status = "healthy"
    except Exception as e:
        logger.error(f"Database health check failed: {e}")
    
    try:
        redis_client.ping()
        redis_status = "healthy"
    except Exception as e:
        logger.error(f"Redis health check failed: {e}")
        
    status = "healthy" if db_status == "healthy" and redis_status == "healthy" else "degraded"
    
    return jsonify({
        "status": status,
        "components": {"database": db_status, "redis": redis_status},
        "timestamp": datetime.now().isoformat()
    })


# ▼▼▼ ここから追加したコード ▼▼▼
@app.route('/api/re-question', methods=['POST'])
@rate_limit(max_calls=10, period=60)
def re_question():
    """既存の履歴に対する再質問を処理する"""
    try:
        data = request.get_json()
        history_id = data.get('history_id')
        question_text = data.get('question_text')

        if not history_id or not question_text:
            return jsonify({"error": "履歴IDと質問内容が必要です"}), 400

        # データベースから元の履歴を取得
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("SELECT image_base64, explanation FROM history WHERE id = %s", (int(history_id),))
                history_item = cur.fetchone()

        if not history_item:
            return jsonify({"error": "元の質問が見つかりません"}), 404

        original_explanation = history_item['explanation']
        base64_image = history_item['image_base64']

        # OpenAIへのプロンプトを作成
        prompt = f"""
        ユーザーは以前、画像（添付）で質問をし、以下の解説を受け取りました。

        【以前の解説】
        ---
        {original_explanation}
        ---

        この解説と元の画像を踏まえて、ユーザーから以下の追加質問がありました。
        この質問に対して、分かりやすく、丁寧に追加の解説をしてください。

        【ユーザーの追加質問】
        「{question_text}」

        【指示】
        - 元の画像と以前の解説内容を考慮して回答してください。
        - 重要な数式は $$...$$ を使って表現してください。
        """

        # OpenAI APIを呼び出す
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
            max_tokens=1000,
            temperature=0.7,
            timeout=60
        )

        answer_text = gpt_response.choices[0].message.content.strip()
        logger.info(f"Successfully answered re-question for history_id: {history_id}")

        return jsonify({"success": True, "answer": answer_text})

    except Exception as e:
        logger.error(f"Error in re_question: {str(e)}")
        return jsonify({"error": "再質問の処理中にエラーが発生しました"}), 500

# ▲▲▲ ここまで追加したコード ▲▲▲


# エラーハンドラー
@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({"error": "ファイルサイズが大きすぎます。16MB以下のファイルを選択してください。"}), 413

@app.errorhandler(429)
def too_many_requests(error):
    return jsonify({"error": "リクエストが多すぎます。しばらく待ってから再度お試しください。"}), 429

@app.errorhandler(500)
def internal_error(error):
    logger.error(f"Server Error: {error}")
    return jsonify({"error": "サーバーエラーが発生しました。しばらくしてからもう一度お試しください。"}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)