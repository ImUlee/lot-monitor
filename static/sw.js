const CACHE_NAME = 'lot-monitor-v1';
const ASSETS_TO_CACHE = [
    '/',
    '/static/icon.png',
    'https://unpkg.com/bootstrap@5.3.0/dist/css/bootstrap.min.css',
    'https://unpkg.com/vue@3/dist/vue.global.prod.js',
    'https://unpkg.com/axios/dist/axios.min.js'
];

// 安装：缓存静态资源
self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => {
            return cache.addAll(ASSETS_TO_CACHE);
        })
    );
});

// 激活：清理旧缓存
self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then((keyList) => {
            return Promise.all(keyList.map((key) => {
                if (key !== CACHE_NAME) {
                    return caches.delete(key);
                }
            }));
        })
    );
});

// 拦截请求
self.addEventListener('fetch', (event) => {
    // 1. 如果是 API 请求，必须走网络 (Network Only)，绝对不缓存
    if (event.request.url.includes('/api/') || event.request.url.includes('/upload')) {
        return; 
    }

    // 2. 其他静态资源：优先走缓存，没有则走网络 (Cache First)
    event.respondWith(
        caches.match(event.request).then((response) => {
            return response || fetch(event.request);
        })
    );
});