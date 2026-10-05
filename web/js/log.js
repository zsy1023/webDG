/* log.js —— 审计日志（开火记录高亮 + 导出 JSON） */
(function () {
  "use strict";
  window.CL = window.CL || {};

  const MAX = 300;

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  class AuditLog {
    constructor(listEl, hintEl) {
      this.list = listEl;
      this.hint = hintEl;
      this.records = [];
    }

    /** rec: {kind, dir, type, summary, detail?} */
    add(rec) {
      const entry = Object.assign({ ts: Date.now() }, rec);
      this.records.push(entry);
      if (this.records.length > MAX) this.records.splice(0, this.records.length - MAX);
      this._prepend(entry);
      if (this.hint) this.hint.textContent = `共 ${this.records.length} 条 · 开火记录高亮`;
    }

    _prepend(e) {
      if (!this.list) return;
      const empty = this.list.querySelector(".empty");
      if (empty) empty.remove();
      const el = document.createElement("div");
      el.className = "rec" + (e.type === "fire" ? " fire" : "");
      const time = new Date(e.ts).toLocaleTimeString("zh-CN");
      const dir = e.dir === "out" ? "→ 发出" : e.dir === "in" ? "← 收到" : "· 本地";
      el.innerHTML =
        `<div class="top"><span>${time} · ${esc(dir)} · ${esc(e.kind || e.type)}</span>` +
        `<span>${esc(e.type)}</span></div><div>${esc(e.summary)}</div>`;
      this.list.prepend(el);
      while (this.list.children.length > MAX) this.list.removeChild(this.list.lastChild);
    }

    export() {
      const blob = new Blob([JSON.stringify(this.records, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `coyotelink-audit-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
  }

  window.CL.AuditLog = AuditLog;
})();
