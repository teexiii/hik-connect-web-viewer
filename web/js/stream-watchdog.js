// Browser-side reconnect layer: restarts a <video-stream> whose picture stops advancing.

const BACKOFF_MS = [1000, 2000, 5000, 10000, 30000];
const STALL_MS = 5000; // playback frozen after it had started
const FIRST_FRAME_MS = 20000; // cloud open + transcode can take a while
const STABLE_MS = 30000; // playing this long resets the failure counter
const MAX_FAILS = 10;

/**
 * H.264 by default. The Hik cloud switches resolution mid-stream (1080p <-> 720p); the app's
 * transcode pins one size, while HEVC passthrough paints a green frame and then crashes iOS
 * Safari's decoder. HEVC stays reachable for debugging with ?stream=hevc.
 */
export function pickCodec(force) {
  return force === 'hevc' ? 'hevc' : 'h264';
}

export class StreamWatchdog {
  /** @param player <video-stream>; @param onStatus (state, detail) => void */
  constructor(player, streams, codec, onStatus) {
    this.player = player;
    this.streams = streams;
    this.codec = codec;
    this.onStatus = onStatus;
    this.fails = 0;
    this.lastTime = -1;
    this.lastAdvance = Date.now();
    this.playingSince = 0;
    this.everPlayed = false;
    this.timer = null;
    this.retryTimer = null;
    this.stopped = false;
    this.onVisible = () => !document.hidden && this.reconnect(0);
    this.onOnline = () => this.reconnect(0);
  }

  /** Report a state to the UI; transitions are also logged for field debugging. */
  emit(state, detail) {
    if (state !== this.lastState) console.info('[watchdog]', state, detail ?? '');
    this.lastState = state;
    this.onStatus(state, detail);
  }

  start() {
    this.player.mode = 'mse,hls';
    this.connect();
    this.timer = setInterval(() => this.tick(), 1000);
    document.addEventListener('visibilitychange', this.onVisible);
    window.addEventListener('online', this.onOnline);
  }

  stop() {
    this.stopped = true;
    clearInterval(this.timer);
    clearTimeout(this.retryTimer);
    document.removeEventListener('visibilitychange', this.onVisible);
    window.removeEventListener('online', this.onOnline);
  }

  /** User pressed "Thử lại" after the failure limit. */
  retry() {
    this.fails = 0;
    this.reconnect(0);
  }

  connect() {
    this.lastTime = -1;
    this.lastAdvance = Date.now();
    this.playingSince = 0;
    this.everPlayed = false;
    this.player.src = `/go2rtc/api/ws?src=${encodeURIComponent(this.streams[this.codec])}`;
    this.emit('connecting');
  }

  tick() {
    const video = this.player.video;
    if (this.stopped || !video || document.hidden) return;
    const now = Date.now();
    if (video.error || video.ended) return this.reconnect();
    if (video.currentTime > 0 && video.currentTime > this.lastTime + 0.05) {
      this.lastTime = video.currentTime;
      this.lastAdvance = now;
      this.playingSince ||= now;
      this.everPlayed = true;
      if (now - this.playingSince > STABLE_MS) this.fails = 0;
      this.emit('live');
      return;
    }
    const limit = this.everPlayed ? STALL_MS : FIRST_FRAME_MS;
    if (now - this.lastAdvance > limit) this.reconnect();
  }

  /** Tear the player down and start again after the backoff for the current failure count. */
  reconnect(delay) {
    if (this.stopped || this.retryTimer) return;
    if (delay === undefined) {
      this.fails += 1;
      delay = BACKOFF_MS[Math.min(this.fails - 1, BACKOFF_MS.length - 1)];
    }
    // Past the limit, show "Mất kết nối" + "Thử lại" but never give up: a camera that comes
    // back after a long outage must resume on its own (slowest backoff keeps retrying).
    this.emit(this.fails > MAX_FAILS ? 'failed' : 'reconnecting', delay);
    clearTimeout(this.player.reconnectTID);
    this.player.ondisconnect();
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      if (this.stopped) return;
      this.connect();
      if (!this.timer) this.timer = setInterval(() => this.tick(), 1000);
    }, delay);
  }
}
