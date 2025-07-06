// main.js - シンプル版

// ページ読み込み時の処理
window.onload = function() {
    // 履歴を読み込み
    loadHistory();
};

// フォーム送信処理
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
    formData.append('school_id', 'default_school');
    formData.append('user_id', 'default_user');
    
    // ローディング表示
    const submitBtn = e.target.querySelector('button[type="submit"]');
    const originalText = submitBtn.textContent;
    submitBtn.textContent = '解析中...';
    submitBtn.disabled = true;
    
    try {
        const response = await fetch('/upload', {
            method: 'POST',
            body: formData
        });
        
        const data = await response.json();
        
        if (data.success) {
            // 履歴を再読み込み
            loadHistory();
            // フォームをリセット
            fileInput.value = '';
            alert('解析が完了しました！');
        } else {
            alert('エラー: ' + (data.error || '解析に失敗しました'));
        }
    } catch (error) {
        alert('通信エラーが発生しました');
        console.error(error);
    } finally {
        submitBtn.textContent = originalText;
        submitBtn.disabled = false;
    }
});

// 履歴読み込み関数
async function loadHistory() {
    try {
        const response = await fetch('/history?user_id=default_user');
        const history = await response.json();
        
        const historyDiv = document.getElementById('history');
        historyDiv.innerHTML = '';
        
        if (history.length === 0) {
            historyDiv.innerHTML = '<p>まだ質問履歴はありません</p>';
            return;
        }
        
        history.forEach((item, index) => {
            const itemDiv = document.createElement('div');
            itemDiv.innerHTML = `
                <div style="margin-bottom: 15px;">
                    <strong>質問 ${history.length - index}</strong> 
                    <small>(${item.timestamp})</small>
                </div>
                <img src="data:image/jpeg;base64,${item.image_base64}" 
                     style="max-width: 300px; margin-bottom: 10px;">
                <div class="explanation">${item.explanation}</div>
            `;
            historyDiv.appendChild(itemDiv);
        });
        
        // MathJaxで数式を再レンダリング
        if (window.MathJax) {
            MathJax.typesetPromise();
        }
    } catch (error) {
        console.error('履歴の読み込みに失敗しました:', error);
    }
}