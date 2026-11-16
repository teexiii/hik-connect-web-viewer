// Browser-side reconnect layer: restarts a <video-stream> whose picture stops advancing.

const BACKOFF_MS = [1000, 2000, 5000, 10000, 30000];
const STALL_MS = 5000; // playback frozen after it had started
const FIRST_FRAME_MS = 20000; // cloud open + transcode can take a while
const STABLE_MS = 30000; // playing this long resets the failure counter
const MAX_FAILS = 10;

/** Pick the stream that this browser can decode (HEVC passthrough, else go2rtc H.264). */
export function pickStream(streams, force) {
  if (force === 'hevc' || force === 'h264') return streams[force];
  const MS = window.ManagedMediaSource ?? window.MediaSource;
  const hevc = MS?.isTypeSupported?.('video/mp4; codecs="hvc1.1.6.L93.B0"');
  return hevc ? streams.hevc : streams.h264;
}

export class StreamWatchdog {
  /** @param player <video-stream>; @param onStatus (state, detail) => void */
  constructor(player, streamName, onStatus) {
    this.player = player;
    this.url = `/go2rtc/api/ws?src=${encodeURIComponent(streamName)}`;
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
    this.player.src = this.url;
    this.onStatus('connecting');
  }

  tick() {
    const video = this.player.video;
    if (this.stopped || !video || document.hidden) return;
    const now = Date.now();
    if (video.error || video.ended) return this.reconnect();
    if (video.currentTime > this.lastTime + 0.05) {
      this.lastTime = video.currentTime;
      this.lastAdvance = now;
      this.playingSince ||= now;
      this.everPlayed = true;
      if (now - this.playingSince > STABLE_MS) this.fails = 0;
      this.onStatus('live');
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
      if (this.fails > MAX_FAILS) {
        this.onStatus('failed');
        clearInterval(this.timer);
        this.timer = null;
        return;
      }
      delay = BACKOFF_MS[Math.min(this.fails - 1, BACKOFF_MS.length - 1)];
    }
    this.onStatus('reconnecting', delay);
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
