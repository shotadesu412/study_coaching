// Service Worker - sw.js
const CACHE_NAME = 'study-support-v2';
const urlsToCache = [
  '/',
  '/static/main.js',
  '/static/icon-192.png',
  '/static/icon-512.png',
  'https://polyfill.io/v3/polyfill.min.js?features=es6',
  'https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js'
];

// ネットワークファーストで処理すべきURL
const networkFirstUrls = [
  '/upload',
  '/task/',
  '/history',
  '/api/',
  '/health',
  '/monitoring'
];

// インストール時にキャッシュ
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => {
        console.log('Opened cache');
        return cache.addAll(urlsToCache);
      })
      .then(() => self.skipWaiting()) // 即座に有効化
  );
});

// リクエスト時の処理
self.addEventListener('fetch', event => {
  const { request } = event;
  const url = new URL(request.url);

  // POST、PUT、DELETE等の非GETリクエストは常にネットワークから
  if (request.method !== 'GET') {
    return;
  }

  // APIエンドポイントはネットワークファースト戦略
  if (networkFirstUrls.some(path => url.pathname.includes(path))) {
    event.respondWith(networkFirst(request));
    return;
  }

  // 静的リソースはキャッシュファースト戦略
  event.respondWith(cacheFirst(request));
});

// キャッシュファースト戦略
async function cacheFirst(request) {
  const cache = await caches.open(CACHE_NAME);
  const cached = await cache.match(request);
  
  if (cached) {
    // バックグラウンドで更新をチェック
    fetchAndCache(request);
    return cached;
  }
  
  return fetchAndCache(request);
}

// ネットワークファースト戦略
async function networkFirst(request) {
  try {
    const response = await fetch(request);
    
    // 成功したレスポンスのみキャッシュ
    if (response.status === 200) {
      const cache = await caches.open(CACHE_NAME);
      cache.put(request, response.clone());
    }
    
    return response;
  } catch (error) {
    // ネットワークエラー時はキャッシュから
    const cached = await caches.match(request);
    if (cached) {
      return cached;
    }
    
    // キャッシュもない場合はエラーレスポンス
    return new Response(JSON.stringify({
      error: 'ネットワークエラーが発生しました',
      offline: true
    }), {
      status: 503,
      headers: { 'Content-Type': 'application/json' }
    });
  }
}

// フェッチしてキャッシュに保存
async function fetchAndCache(request) {
  try {
    const response = await fetch(request);
    
    // 成功したレスポンスのみキャッシュ
    if (response.status === 200) {
      const cache = await caches.open(CACHE_NAME);
      cache.put(request, response.clone());
    }
    
    return response;
  } catch (error) {
    // フェッチ失敗時はオフライン用の代替レスポンス
    return new Response('オフライン中です', {
      status: 503,
      statusText: 'Service Unavailable'
    });
  }
}

// 古いキャッシュの削除
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(cacheNames => {
        return Promise.all(
          cacheNames
            .filter(cacheName => cacheName !== CACHE_NAME)
            .map(cacheName => caches.delete(cacheName))
        );
      })
      .then(() => self.clients.claim()) // 即座にクライアントを制御
  );
});

// バックグラウンド同期（オプション）
self.addEventListener('sync', event => {
  if (event.tag === 'upload-sync') {
    event.waitUntil(syncUpload());
  }
});

// オフライン時の同期処理
async function syncUpload() {
  const cache = await caches.open('pending-uploads');
  const requests = await cache.keys();
  
  for (const request of requests) {
    try {
      const response = await fetch(request.clone());
      if (response.ok) {
        await cache.delete(request);
      }
    } catch (error) {
      console.error('Sync failed:', error);
    }
  }
}

// プッシュ通知（タスク完了通知用）
self.addEventListener('push', event => {
  if (event.data) {
    const data = event.data.json();
    const options = {
      body: data.body || '画像の解析が完了しました',
      icon: '/static/icon-192.png',
      badge: '/static/icon-192.png',
      vibrate: [200, 100, 200],
      data: {
        taskId: data.taskId,
        timestamp: new Date().toISOString()
      }
    };
    
    event.waitUntil(
      self.registration.showNotification(
        data.title || '解析完了',
        options
      )
    );
  }
});

// 通知クリック時の処理
self.addEventListener('notificationclick', event => {
  event.notification.close();
  
  event.waitUntil(
    clients.matchAll({ type: 'window' })
      .then(clientList => {
        // 既存のウィンドウがあればフォーカス
        for (const client of clientList) {
          if (client.url.includes('/') && 'focus' in client) {
            return client.focus();
          }
        }
        // なければ新規ウィンドウを開く
        if (clients.openWindow) {
          return clients.openWindow('/');
        }
      })
  );
});

// メッセージ処理（クライアントとの通信）
self.addEventListener('message', event => {
  if (event.data && event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
  
  // キャッシュクリアのリクエスト
  if (event.data && event.data.type === 'CLEAR_CACHE') {
    event.waitUntil(
      caches.keys()
        .then(cacheNames => Promise.all(
          cacheNames.map(cacheName => caches.delete(cacheName))
        ))
        .then(() => {
          event.ports[0].postMessage({ success: true });
        })
        .catch(error => {
          event.ports[0].postMessage({ success: false, error: error.message });
        })
    );
  }
});

// 定期的なキャッシュ更新（24時間ごと）
const CACHE_UPDATE_INTERVAL = 24 * 60 * 60 * 1000; // 24時間

async function updateCache() {
  const cache = await caches.open(CACHE_NAME);
  const keys = await cache.keys();
  
  for (const request of keys) {
    try {
      const response = await fetch(request);
      if (response.ok) {
        await cache.put(request, response);
      }
    } catch (error) {
      console.error('Cache update failed:', error);
    }
  }
}

// ページの表示状態を監視
self.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') {
    // ページが表示されたときにキャッシュを確認
    const lastUpdate = localStorage.getItem('lastCacheUpdate');
    const now = Date.now();
    
    if (!lastUpdate || now - parseInt(lastUpdate) > CACHE_UPDATE_INTERVAL) {
      updateCache();
      localStorage.setItem('lastCacheUpdate', now.toString());
    }
  }
});