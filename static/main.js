// main.js - 非同期処理・学年選択対応版

window.onload = function() {
    loadHistory();
};

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
    
    // ▼▼▼ 選択された学年情報を取得して追加 ▼▼▼
    const selectedGrade = document.querySelector('input[name="grade"]:checked').value;
    formData.append('grade_level', selectedGrade);
    // ▲▲▲ ここまで ▲▲▲
    
    formData.append('school_id', 'default_school');
    formData.append('user_id', 'default_user');

    const submitBtn = e.target.querySelector('button[type="submit"]');
    const originalText = submitBtn.textContent;
    submitBtn.textContent = '解析中...';
    submitBtn.disabled = true;

    try {
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

        const taskId = uploadData.task_id;
        const result = await pollTaskStatus(taskId);

        if (result.status === 'completed') {
            alert('解析が完了しました！');
            await loadHistory();
            fileInput.value = '';
        } else {
            throw new Error(result.error || '解析中に不明なエラーが発生しました');
        }

    } catch (error) {
        alert('エラー: ' + error.message);
        console.error(error);
    } finally {
        submitBtn.textContent = originalText;
        submitBtn.disabled = false;
    }
});

async function pollTaskStatus(taskId) {
    const maxAttempts = 30;
    const interval = 3000;

    for (let i = 0; i < maxAttempts; i++) {
        try {
            const response = await fetch(`/task/${taskId}`);
            if (!response.ok) {
                await new Promise(resolve => setTimeout(resolve, interval));
                continue;
            }
            
            const data = await response.json();

            if (data.status === 'completed' || data.status === 'failed') {
                return data;
            }
            
            await new Promise(resolve => setTimeout(resolve, interval));

        } catch (error) {
            console.error('ステータス確認中にエラー:', error);
            await new Promise(resolve => setTimeout(resolve, interval));
        }
    }

    throw new Error('解析がタイムアウトしました。しばらくしてからもう一度お試しください。');
}

async function loadHistory() {
    try {
        const response = await fetch('/history?user_id=default_user&limit=20');
        if (!response.ok) {
            throw new Error(`履歴の取得に失敗しました (HTTP ${response.status})`);
        }
        
        const data = await response.json();
        const history = data.history || [];

        const historyDiv = document.getElementById('history');
        historyDiv.innerHTML = '';

        if (history.length === 0) {
            historyDiv.innerHTML = '<p>まだ質問履歴はありません</p>';
            return;
        }

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

        if (window.MathJax && typeof window.MathJax.typesetPromise === 'function') {
            await MathJax.typesetPromise([historyDiv]);
        }

    } catch (error) {
        console.error('履歴の読み込みに失敗しました:', error);
        const historyDiv = document.getElementById('history');
        historyDiv.innerHTML = '<p style="color: red;">履歴の読み込みに失敗しました。</p>';
    }
}