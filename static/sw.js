// voice-loop の画面の部品を手元に取っておき、Mac に届かないときでも画面だけは開けるようにする。
// 届かないときの案内と、つながったときの戻り方は画面（index.html）が受け持つ。
//
// 取り方: まず Mac に取りに行き、取れたら手元の写しも新しくする。3秒で返ってこないとき（通信が止まって
// 待たされるとき）と、取れなかったときだけ、手元の写しで答える。更新したのに古い画面が出続けることを避けるため。
// 扱うのは画面（/ を開く操作）と SHELL のファイルだけ。/api と /ws には手を出さない（一覧や会話を古い写しで答えない）。
// 写しに残すのは、転送されていない 200 だけ（ほかのページへの転送や失敗の応答を、画面として残さない）。
const CACHE = 'voice-loop-shell-v1';
const SHELL = [
  '/',
  '/static/vendor/marked.min.js',
  '/static/vendor/purify.min.js',
  '/static/manifest.webmanifest',
  '/static/icons/apple-touch-icon.png',
  '/static/icons/favicon-32.png',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
];
const WAIT_MS = 3000;
const keepable = res => res.status === 200 && !res.redirected;

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE)
    .then(c => Promise.all(SHELL.map(path => fetch(path, { cache: 'no-cache' })
      .then(res => (keepable(res) ? c.put(path, res) : null))
      .catch(() => null)))) // 取れなかったファイルは、次に開いたときに写す
    .then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== self.location.origin) return;
  if (!SHELL.includes(url.pathname)) return; // /api など、画面の部品でないものは素通し（/api を直接開いた場合も）
  const key = url.pathname;
  let saved = Promise.resolve();
  const net = fetch(e.request).then(res => {
    if (keepable(res)) {
      const copy = res.clone();
      saved = caches.open(CACHE).then(c => c.put(key, copy));
    }
    return res;
  });
  e.waitUntil(net.then(() => saved, () => {})); // 先に写しで答えても、取りに行った分で写しを新しくする
  e.respondWith((async () => {
    try {
      return await Promise.race([net, new Promise((_, no) => setTimeout(no, WAIT_MS))]);
    } catch {
      return (await caches.match(key)) || net; // 写しが無い（初めて開いた）ときは、Mac の返事を待つ
    }
  })());
});
