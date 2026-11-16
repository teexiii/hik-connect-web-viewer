// Camera grid: thumbnail poster, live tile when visible, status dot, tap to open fullscreen.
import { getJson } from './api-client.js';
import { LiveTile, STATUS_LABEL } from './live-tile.js';

const OFFLINE_RETRY_MS = 60000;

export class CameraGrid {
  constructor(root, onOpen, onAccount) {
    this.root = root;
    this.onOpen = onOpen;
    this.onAccount = onAccount;
    this.cards = new Map(); // camera id -> { camera, el, tile, visible, offlineUntil }
    this.paused = false;
    this.observer = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        const card = this.cards.get(entry.target.dataset.id);
        card.visible = entry.isIntersecting;
        this.sync(card);
      }
    }, { threshold: 0.3 });
  }

  /** Renders cards for cameras not known to be offline; returns both groups. */
  async load() {
    const cameras = await getJson('/api/cameras');
    const online = cameras.filter((camera) => camera.online !== false);
    const offline = cameras.filter((camera) => camera.online === false);
    this.root.replaceChildren();
    if (!online.length) this.root.innerHTML = '<p class="empty">Không có camera nào đang online.</p>';
    for (const camera of online) this.add(camera);
    this.pollStatus();
    setInterval(() => this.pollStatus(), 5000);
    return { online, offline };
  }

  add(camera) {
    const el = document.createElement('article');
    el.className = 'card';
    el.dataset.id = camera.id;
    el.innerHTML = `
      <img class="poster" alt="" src="/api/cameras/${encodeURIComponent(camera.id)}/thumbnail">
      <div class="live"></div>
      <div class="meta"><span class="dot"></span><span class="name"></span><span class="state"></span></div>`;
    el.querySelector('.name').textContent = camera.name;
    el.addEventListener('click', () => this.onOpen(camera));
    el.querySelector('.poster').addEventListener('error', (ev) => ev.target.classList.add('missing'));
    const card = { camera, el, tile: null, visible: false, offlineUntil: 0 };
    this.cards.set(camera.id, card);
    this.root.append(el);
    this.observer.observe(el);
  }

  setState(card, state) {
    card.el.dataset.state = state;
    card.el.querySelector('.state').textContent = STATUS_LABEL[state] ?? '';
  }

  /** Start or stop the live tile according to visibility, pause and offline back-off. */
  sync(card) {
    const wanted = card.visible && !this.paused && Date.now() >= card.offlineUntil;
    if (wanted && !card.tile) {
      card.tile = new LiveTile(card.el.querySelector('.live'), card.camera, (s) => this.setState(card, s));
      card.tile.start();
    } else if (!wanted && card.tile) {
      card.tile.stop();
      card.tile = null;
    }
  }

  /** Stop every grid stream while the fullscreen viewer owns the connection. */
  pause(paused) {
    this.paused = paused;
    for (const card of this.cards.values()) this.sync(card);
  }

  async pollStatus() {
    let status;
    try {
      status = await getJson('/api/status');
    } catch {
      return;
    }
    this.onAccount(status.account);
    for (const card of this.cards.values()) {
      if (status.streams[card.camera.id]?.state === 'offline' && card.tile) {
        card.tile.stop();
        card.tile = null;
        card.offlineUntil = Date.now() + OFFLINE_RETRY_MS;
        this.setState(card, 'offline');
        setTimeout(() => this.sync(card), OFFLINE_RETRY_MS + 100);
      }
    }
  }
}
