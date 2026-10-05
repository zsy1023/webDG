/* transport.js —— 与本地 web 后端的 WebSocket 客户端（连不上时优雅降级到 mock） */
(function () {
  "use strict";
  window.CL = window.CL || {};

  class Transport {
    /** handlers: {onOpen, onMessage, onClose, onOffline} */
    constructor(handlers) {
      this.h = handlers || {};
      this.ws = null;
      this.retry = 0;
      this.offlineNotified = false;
      this.url = null;
    }

    get online() { return !!(this.ws && this.ws.readyState === 1); }

    connect() {
      // file:// 直开：同源 WebSocket 无意义，直接脱机 mock
      if (location.protocol === "file:") { this._offline("file:// 无法连后端"); return; }
      const url = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws";
      this.url = url;
      let ws;
      try { ws = new WebSocket(url); }
      catch (e) { this._offline("WebSocket 不可用"); return; }
      this.ws = ws;
      ws.onopen = () => { this.retry = 0; this.offlineNotified = false; this.h.onOpen && this.h.onOpen(url); };
      ws.onmessage = (ev) => {
        let msg; try { msg = JSON.parse(ev.data); } catch (e) { return; }
        this.h.onMessage && this.h.onMessage(msg);
      };
      ws.onerror = () => { /* 交给 onclose 兜底 */ };
      ws.onclose = () => { this.h.onClose && this.h.onClose(); this._scheduleRetry(); };
    }

    _scheduleRetry() {
      this.retry++;
      const delay = Math.min(1000 * this.retry, 5000);
      setTimeout(() => this.connect(), delay);
      if (!this.offlineNotified && this.retry >= 2) this._offline("后端未响应");
    }

    _offline(reason) {
      if (this.offlineNotified) return;
      this.offlineNotified = true;
      this.h.onOffline && this.h.onOffline(reason);
    }

    /** 返回 true = 已发出；false = 离线（调用方自行 mock 处理） */
    send(obj) {
      if (this.online) { this.ws.send(JSON.stringify(obj)); return true; }
      return false;
    }
  }

  window.CL.Transport = Transport;
})();
