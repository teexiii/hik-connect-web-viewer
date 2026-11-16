// Fullscreen viewer: live view plus refresh, snapshot and clip recording.
import { getJson, postJson, saveFile } from './api-client.js';
import { LiveTile, STATUS_LABEL } from './live-tile.js';

const CLIP_POLL_MS = 2000;
const CHROME_HIDE_MS = 4000;

export class FullscreenViewer {
  constructor(root, onClose) {
    this.root = root;
    this.onClose = onClose;
    this.tile = null;
    this.camera = null;
    this.q = (sel) => root.querySelector(sel);
    this.q('.back').addEventListener('click', () => this.close());
    this.q('.refresh').addEventListener('click', () => this.refresh());
    this.q('.snapshot').addEventListener('click', () => this.snapshot());
    this.q('.clip').addEventListener('click', () => this.clip());
    this.q(':scope > .retry').addEventListener('click', () => this.tile?.retry());
    this.q('.live').addEventListener('click', () => this.toggleChrome());
    this.q('.viewer-bottom').addEventListener('click', () => this.showChrome());
  }

  open(camera) {
    this.camera = camera;
    this.q('.viewer-top .title').textContent = camera.name;
    this.q('.live').replaceChildren();
    delete this.root.dataset.state;
    this.setStatus('connecting');
    this.root.hidden = false;
    this.tile = new LiveTile(this.q('.live'), camera, (state) => this.setStatus(state));
    this.tile.start();
  }

  close() {
    this.tile?.stop();
    this.tile = null;
    clearTimeout(this.chromeTimer);
    this.root.hidden = true;
    this.toast('');
    this.onClose();
  }

  setStatus(state) {
    if (state === this.root.dataset.state) return; // the watchdog repeats 'live' every second
    const label = state === 'live' ? 'LIVE' : (STATUS_LABEL[state] ?? state);
    this.q('.viewer-top .status').textContent = label;
    this.q('.veil-text').textContent = STATUS_LABEL[state] ?? '';
    this.root.dataset.state = state;
    this.q(':scope > .retry').hidden = state !== 'failed';
    this.showChrome();
  }

  /** Overlay controls fade out after a few seconds of live video; a tap on the picture brings them back. */
  showChrome() {
    clearTimeout(this.chromeTimer);
    this.root.dataset.chrome = 'on';
    if (this.root.dataset.state !== 'live') return;
    this.chromeTimer = setTimeout(() => { this.root.dataset.chrome = 'off'; }, CHROME_HIDE_MS);
  }

  toggleChrome() {
    if (this.root.dataset.chrome === 'off') this.showChrome();
    else {
      clearTimeout(this.chromeTimer);
      this.root.dataset.chrome = 'off';
    }
  }

  toast(text) {
    this.q('.toast').textContent = text;
    if (text) this.showChrome();
  }

  async refresh() {
    try {
      await postJson(`/api/streams/${encodeURIComponent(this.camera.id)}/reconnect`);
    } catch { /* ingest not running yet: the player reconnect below starts it */ }
    this.tile?.refresh();
  }

  async snapshot() {
    try {
      await saveFile(`/api/cameras/${encodeURIComponent(this.camera.id)}/snapshot`, 'snapshot.jpg');
      this.toast('Đã lưu ảnh');
    } catch (err) {
      this.toast(`Không chụp được ảnh (${err.message})`);
    }
  }

  async clip() {
    const seconds = Number(this.q('.clip-seconds').value);
    const button = this.q('.clip');
    button.disabled = true;
    try {
      const { job_id: jobId } = await postJson(
        `/api/cameras/${encodeURIComponent(this.camera.id)}/clip`, { seconds });
      this.toast(`Đang ghi ${seconds} giây…`);
      for (;;) {
        await new Promise((resolve) => setTimeout(resolve, CLIP_POLL_MS));
        const job = await getJson(`/api/clips/${jobId}`);
        if (job.status === 'done') break;
        if (job.status === 'failed') throw new Error('ghi thất bại');
      }
      await saveFile(`/api/clips/${jobId}/download`, 'clip.mp4');
      this.toast('Đã lưu clip');
    } catch (err) {
      this.toast(`Không ghi được clip (${err.message})`);
    } finally {
      button.disabled = false;
    }
  }
}
