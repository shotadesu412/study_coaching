// main.js - 再質問機能対応版

window.onload = function() {
    loadHistory();
    setupEventListeners();
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
    
    const selectedGrade = document.querySelector('input[name="grade"]:checked').value;
    formData.append('grade_level', selectedGrade);
    
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
                
                <button class="re-question-btn" data-history-id="${item.id}">さらに質問する</button>
                
                <div class="re-question-form" id="form-${item.id}" style="display: none;">
                    <textarea placeholder="わからなかったことを詳しく書いてください。"></textarea>
                    <button class="re-question-submit-btn" data-history-id="${item.id}">送信</button>
                    <div class="loading-indicator">回答を生成中...</div>
                </div>
                
                <div class="re-question-answer" id="answer-${item.id}"></div>
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

function setupEventListeners() {
    const historyDiv = document.getElementById('history');

    historyDiv.addEventListener('click', async (e) => {
        // 「さらに質問する」ボタンが押された場合
        if (e.target.classList.contains('re-question-btn')) {
            const historyId = e.target.dataset.historyId;
            const form = document.getElementById(`form-${historyId}`);
            if (form) {
                form.style.display = form.style.display === 'none' ? 'block' : 'none';
            }
        }

        // 再質問の「送信」ボタンが押された場合
        if (e.target.classList.contains('re-question-submit-btn')) {
            const historyId = e.target.dataset.historyId;
            await handleReQuestionSubmit(historyId);
        }
    });
}

async function handleReQuestionSubmit(historyId) {
    const form = document.getElementById(`form-${historyId}`);
    const textarea = form.querySelector('textarea');
    const submitBtn = form.querySelector('.re-question-submit-btn');
    const loadingIndicator = form.querySelector('.loading-indicator');
    const answerDiv = document.getElementById(`answer-${historyId}`);

    const questionText = textarea.value.trim();
    if (!questionText) {
        alert('質問内容を入力してください。');
        return;
    }

    submitBtn.disabled = true;
    loadingIndicator.style.display = 'block';
    answerDiv.innerHTML = '';

    try {
        const response = await fetch('/api/re-question', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                history_id: historyId,
                question_text: questionText
            })
        });

        if (!response.ok) {
            const errorData = await response.json();
            throw new Error(errorData.error || 'サーバーエラーが発生しました。');
        }

        const data = await response.json();
        answerDiv.textContent = data.answer;
        
        if (window.MathJax && typeof window.MathJax.typesetPromise === 'function') {
            await MathJax.typesetPromise([answerDiv]);
        }
        
        textarea.value = ''; // テキストエリアをクリア
        form.style.display = 'none'; // フォームを非表示

    } catch (error) {
        answerDiv.innerHTML = `<p style="color: red;">エラー: ${error.message}</p>`;
    } finally {
        submitBtn.disabled = false;
        loadingIndicator.style.display = 'none';
    }
}