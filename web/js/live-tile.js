// One live view: <video-stream> + watchdog, shared by the grid cards and the fullscreen viewer.
import '../vendor/video-stream.js';
import { StreamWatchdog, pickCodec } from './stream-watchdog.js';

export const STATUS_LABEL = {
  connecting: 'Đang kết nối',
  live: 'Trực tiếp',
  reconnecting: 'Đang kết nối lại',
  failed: 'Mất kết nối',
  offline: 'Camera offline',
};

export class LiveTile {
  /** @param host element to mount into; @param camera API camera; @param onStatus (state) => void */
  constructor(host, camera, onStatus) {
    this.host = host;
    this.camera = camera;
    this.onStatus = onStatus;
    this.player = null;
    this.watchdog = null;
  }

  start() {
    if (this.player) return;
    const force = new URLSearchParams(location.search).get('stream');
    const codec = pickCodec(force);
    this.player = document.createElement('video-stream');
    this.player.setAttribute('muted', '');
    this.host.append(this.player);
    this.player.video.muted = true;
    this.player.video.playsInline = true;
    this.player.video.setAttribute('playsinline', '');
    // The vendored player turns native controls on; this UI has its own.
    this.player.video.controls = false;
    this.player.video.addEventListener('playing', () => { this.player.video.controls = false; });
    this.watchdog = new StreamWatchdog(
      this.player, this.camera.streams, codec, !force, (state, detail) => this.onStatus(state, detail));
    this.watchdog.start();
  }

  stop() {
    this.watchdog?.stop();
    this.player?.ondisconnect();
    this.player?.remove();
    this.player = null;
    this.watchdog = null;
  }

  /** Hard reconnect initiated by the user. */
  refresh() {
    this.watchdog?.reconnect(0);
  }

  retry() {
    this.watchdog?.retry();
  }

  get currentTime() {
    return this.player?.video?.currentTime ?? 0;
  }
}
