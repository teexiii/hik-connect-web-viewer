// Page bootstrap: grid, fullscreen viewer, Hik-Connect status banner and the small menu.
import { postJson } from './api-client.js';
import { CameraGrid } from './camera-grid.js';
import { FullscreenViewer } from './fullscreen-viewer.js';

const BANNER_TEXT = {
  captcha: 'Hik-Connect yêu cầu CAPTCHA: mở app Hik-Connect, đăng nhập một lần rồi bấm Thử lại.',
  auth: 'Hik-Connect từ chối tài khoản của máy chủ. Kiểm tra cấu hình rồi bấm Thử lại.',
  config: 'Máy chủ chưa được cấu hình tài khoản Hik-Connect.',
};
const banner = document.querySelector('#banner');

function showAccount(account) {
  const attention = account.status === 'needs_attention';
  banner.hidden = !attention;
  if (attention) {
    document.querySelector('#banner-text').textContent = BANNER_TEXT[account.reason] ?? 'Hik-Connect cần xử lý.';
  }
}

const grid = new CameraGrid(document.querySelector('#grid'), (camera) => {
  grid.pause(true);
  viewer.open(camera);
}, showAccount);
const viewer = new FullscreenViewer(document.querySelector('#viewer'), () => grid.pause(false));

function listOffline(cameras) {
  const box = document.querySelector('#offline-list');
  box.hidden = !cameras.length;
  box.replaceChildren(...cameras.map((camera) => {
    const row = document.createElement('div');
    row.innerHTML = '<strong></strong> · offline';
    row.querySelector('strong').textContent = camera.name;
    return row;
  }));
}

grid.load().then(({ online, offline }) => {
  listOffline(offline);
  // A single online camera opens straight into the live view.
  if (online.length === 1) {
    grid.pause(true);
    viewer.open(online[0]);
  }
}).catch((err) => {
  document.querySelector('#grid').textContent = `Không tải được danh sách camera: ${err.message}`;
});

const menu = document.querySelector('#menu');
document.querySelector('#menu-button').addEventListener('click', () => { menu.hidden = !menu.hidden; });

document.querySelector('#logout').addEventListener('click', async () => {
  await postJson('/api/logout');
  location.href = '/login.html';
});

document.querySelector('#banner-retry').addEventListener('click', async () => {
  await postJson('/api/account/retry');
  banner.hidden = true;
});
