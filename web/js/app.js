// Page bootstrap: grid, fullscreen viewer and the small account menu.
import { postJson } from './api-client.js';
import { CameraGrid } from './camera-grid.js';
import { FullscreenViewer } from './fullscreen-viewer.js';

const grid = new CameraGrid(document.querySelector('#grid'), (camera) => {
  grid.pause(true);
  viewer.open(camera);
});
const viewer = new FullscreenViewer(document.querySelector('#viewer'), () => grid.pause(false));

grid.load().catch((err) => {
  document.querySelector('#grid').textContent = `Không tải được danh sách camera: ${err.message}`;
});

const menu = document.querySelector('#menu');
document.querySelector('#menu-button').addEventListener('click', () => { menu.hidden = !menu.hidden; });

document.querySelector('#logout').addEventListener('click', async () => {
  await postJson('/api/logout');
  location.href = '/login.html';
});

document.querySelector('#forget').addEventListener('click', async () => {
  if (!confirm('Xoá tài khoản Hik-Connect khỏi máy chủ này?')) return;
  await postJson('/api/account/forget');
  location.href = '/login.html';
});
