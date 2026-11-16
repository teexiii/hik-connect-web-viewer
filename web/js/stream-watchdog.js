// Browser-side reconnect layer: restarts a <video-stream> whose picture stops advancing.

const BACKOFF_MS = [1000, 2000, 5000, 10000, 30000];
const STALL_MS = 5000; // playback frozen after it had started
const FIRST_FRAME_MS = 20000; // cloud open + transcode can take a while
const STABLE_MS = 30000; // playing this long resets the failure counter
const MAX_FAILS = 10;

const CODEC_PREF_KEY = 'camviewer-codec';
const HEVC_FAILS_BEFORE_FALLBACK = 1;
const HEVC_FIRST_FRAME_MS = 10000; // a decodable HEVC stream shows its first frame quickly

function readPref() {
  try { return localStorage.getItem(CODEC_PREF_KEY); } catch { return null; }
}

function savePref(codec) {
  try { localStorage.setItem(CODEC_PREF_KEY, codec); } catch { /* private mode */ }
}

/** HEVC passthrough when the browser claims support, else go2rtc's H.264 transcode. */
export function pickCodec(force) {
  if (force === 'hevc' || force === 'h264') return force;
  if (readPref() === 'h264') return 'h264';
  const MS = window.ManagedMediaSource ?? window.MediaSource;
  return MS?.isTypeSupported?.('video/mp4; codecs="hvc1.1.6.L93.B0"') ? 'hevc' : 'h264';
}

export class StreamWatchdog {
  /** @param player <video-stream>; @param onStatus (state, detail) => void */
  constructor(player, streams, codec, canFallback, onStatus) {
    this.player = player;
    this.streams = streams;
    this.codec = codec;
    this.canFallback = canFallback; // not when the URL forces a codec
    this.hasPlayedEver = false;
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
      this.hasPlayedEver = true;
      if (now - this.playingSince > STABLE_MS) this.fails = 0;
      this.emit('live');
      return;
    }
    let limit = this.everPlayed ? STALL_MS : FIRST_FRAME_MS;
    if (!this.hasPlayedEver && this.codec === 'hevc') limit = HEVC_FIRST_FRAME_MS;
    if (now - this.lastAdvance > limit) this.reconnect();
  }

  /** Tear the player down and start again after the backoff for the current failure count. */
  reconnect(delay) {
    if (this.stopped || this.retryTimer) return;
    if (delay === undefined) {
      this.fails += 1;
      // Browsers can claim HEVC support yet fail to decode it: fall back to H.264 for good.
      if (this.canFallback && this.codec === 'hevc' && !this.hasPlayedEver
          && this.fails >= HEVC_FAILS_BEFORE_FALLBACK) {
        this.codec = 'h264';
        savePref('h264');
      }
      if (this.fails > MAX_FAILS) {
        this.emit('failed');
        clearInterval(this.timer);
        this.timer = null;
        return;
      }
      delay = BACKOFF_MS[Math.min(this.fails - 1, BACKOFF_MS.length - 1)];
    }
    this.emit('reconnecting', delay);
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
