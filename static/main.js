// main.js - 非同期処理対応版

// ページ読み込み時の処理
window.onload = function() {
    // 履歴を読み込み
    loadHistory();
};

/**
 * フォームの送信イベントを処理します。
 * 1. /upload に画像を送信してタスクIDを取得
 * 2. タスクIDを使って解析の完了をポーリング（定期確認）
 * 3. 解析が完了したら履歴を再読み込み
 */
document.getElementById('qform').addEventListener('submit', async function(e) {
    e.preventDefault();

    const fileInput = document.getElementById('fileInput');
    const file = fileInput.files[0];

    if (!file) {
        alert('画像を選択してください');
        return;
    }

    const formData = new FormData();
    formData.append('file', file);
    // ユーザーIDや学校IDは必要に応じて動的に設定してください
    formData.append('school_id', 'default_school');
    formData.append('user_id', 'default_user');

    const submitBtn = e.target.querySelector('button[type="submit"]');
    const originalText = submitBtn.textContent;
    submitBtn.textContent = '解析中...';
    submitBtn.disabled = true;

    try {
        // 1. /uploadにPOSTして、タスクIDを受け取る
        const uploadResponse = await fetch('/upload', {
            method: 'POST',
            body: formData
        });

        if (!uploadResponse.ok) {
            const errorData = await uploadResponse.json();
            throw new Error(errorData.error || `アップロードに失敗しました (HTTP ${uploadResponse.status})`);
        }

        const uploadData = await uploadResponse.json();

        if (!uploadData.success || !uploadData.task_id) {
            throw new Error(uploadData.error || 'タスクの開始に失敗しました');
        }

        // 2. 受け取ったタスクIDを使って、タスクの完了をポーリングする
        const taskId = uploadData.task_id;
        const result = await pollTaskStatus(taskId);

        // 3. タスクが完了したら、結果を表示して履歴を更新
        if (result.status === 'completed') {
            alert('解析が完了しました！');
            await loadHistory(); // 履歴を更新
            fileInput.value = ''; // フォームをリセット
        } else {
            throw new Error(result.error || '解析中に不明なエラーが発生しました');
        }

    } catch (error) {
        alert('エラー: ' + error.message);
        console.error(error);
    } finally {
        // ボタンの状態を元に戻す
        submitBtn.textContent = originalText;
        submitBtn.disabled = false;
    }
});

/**
 * タスクのステータスを定期的に確認（ポーリング）する関数
 * @param {string} taskId - 確認するタスクのID
 * @returns {Promise<object>} - 完了または失敗したタスクの結果オブジェクト
 */
async function pollTaskStatus(taskId) {
    const maxAttempts = 30; // 最大30回試行（合計約1分半）
    const interval = 3000;  // 3秒間隔

    for (let i = 0; i < maxAttempts; i++) {
        try {
            const response = await fetch(`/task/${taskId}`);
            if (!response.ok) {
                // 404 Not Foundなどの場合は少し待ってリトライ
                await new Promise(resolve => setTimeout(resolve, interval));
                continue;
            }
            
            const data = await response.json();

            // 完了または失敗したら結果を返してループを抜ける
            if (data.status === 'completed' || data.status === 'failed') {
                return data;
            }
            
            // 'processing' または 'pending' の場合は待機
            await new Promise(resolve => setTimeout(resolve, interval));

        } catch (error) {
            // ネットワークエラーなどが発生した場合
            console.error('ステータス確認中にエラー:', error);
            // リトライを継続
            await new Promise(resolve => setTimeout(resolve, interval));
        }
    }

    // 指定回数試行しても完了しなかった場合
    throw new Error('解析がタイムアウトしました。しばらくしてからもう一度お試しください。');
}

/**
 * 質問の履歴をサーバーから取得して表示する関数
 */
async function loadHistory() {
    try {
        const response = await fetch('/history?user_id=default_user&limit=20');
        if (!response.ok) {
            throw new Error(`履歴の取得に失敗しました (HTTP ${response.status})`);
        }
        
        const data = await response.json();
        const history = data.history || [];

        const historyDiv = document.getElementById('history');
        historyDiv.innerHTML = ''; // 表示をクリア

        if (history.length === 0) {
            historyDiv.innerHTML = '<p>まだ質問履歴はありません</p>';
            return;
        }

        // 新しい履歴が上に来るように表示
        history.forEach((item, index) => {
            const itemDiv = document.createElement('div');
            
            const timestamp = new Date(item.timestamp).toLocaleString('ja-JP');

            itemDiv.innerHTML = `
                <div style="margin-bottom: 15px;">
                    <strong>質問 ${data.total - index}</strong> 
                    <small>(${timestamp})</small>
                </div>
                <img src="data:image/jpeg;base64,${item.image_base64}" 
                     alt="質問画像"
                     style="max-width: 300px; margin-bottom: 10px; border-radius: 5px;">
                <div class="explanation">${item.explanation}</div>
            `;
            historyDiv.appendChild(itemDiv);
        });

        // MathJaxで数式を再レンダリング
        if (window.MathJax && typeof window.MathJax.typesetPromise === 'function') {
            await MathJax.typesetPromise([historyDiv]);
        }

    } catch (error) {
        console.error('履歴の読み込みに失敗しました:', error);
        const historyDiv = document.getElementById('history');
        historyDiv.innerHTML = '<p style="color: red;">履歴の読み込みに失敗しました。</p>';
    }
}