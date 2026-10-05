/* skeleton.js —— Canvas 2D 骨骼舞台（33 点 + 连线 + 头姿箭头）
 * 关键：只渲染**最新帧**，seq 比当前小的直接丢，绝不排队（否则越描越滞后）。
 */
(function () {
  "use strict";
  window.CL = window.CL || {};

  const CONNECTIONS = [
    [0, 1], [1, 2], [2, 3], [3, 7], [0, 4], [4, 5], [5, 6], [6, 8], [9, 10],
    [11, 12], [11, 13], [13, 15], [15, 17], [15, 19], [15, 21], [17, 19],
    [12, 14], [14, 16], [16, 18], [16, 20], [16, 22], [18, 20],
    [11, 23], [12, 24], [23, 24], [23, 25], [24, 26], [25, 27], [26, 28],
    [27, 29], [28, 30], [29, 31], [30, 32], [27, 31], [28, 32],
  ];

  class Skeleton {
    constructor(canvas, dom) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.W = canvas.width;
      this.H = canvas.height;
      this.dom = dom || {};
      this.frame = null;      // 最新姿态
      this.head = null;       // 最新头姿
      this.lastSeq = -1;
      this.dropped = 0;
      this.counted = 0;       // 用于 FPS 的已渲染帧
      this.fpsTimer = performance.now();
      this.fps = 0;
      this.present = false;
      //: 规则编辑层（`CL.StageEdit`）—— 有就每帧让它把线/框/圆画上去
      this.editor = null;
      this.loop = this.loop.bind(this);
    }

    setFrame(pose) {
      if (!pose) return;
      // ★ 丢过期帧：seq 不比当前大就丢
      if (typeof pose.seq === "number" && pose.seq <= this.lastSeq) {
        this.dropped++;
        return;
      }
      if (typeof pose.seq === "number") this.lastSeq = pose.seq;
      this.frame = pose;
      this.present = !!pose.present;
      this._hud(pose);
    }

    setHead(face) {
      if (face && face.head) this.head = face.head;
    }

    _hud(pose) {
      const d = this.dom;
      if (d.seq && typeof pose.seq === "number") d.seq.textContent = pose.seq;
      if (d.drop) d.drop.textContent = this.dropped;
      if (d.state) d.state.textContent = "pose：" + (pose.present ? "有" : "空");
      if (d.vis) {
        const n = (pose.visibility || []).filter((v) => v > 0.5).length;
        d.vis.textContent = n ? `${n} / ${(pose.landmarks || []).length}` : "—";
      }
      if (d.lat) {
        const recv = pose.recv || Date.now();
        const lat = pose.ts ? Math.max(0, recv - pose.ts) : null;
        d.lat.textContent = lat == null ? "—" : lat + " ms";
      }
    }

    loop() {
      const ctx = this.ctx, W = this.W, H = this.H;
      ctx.clearRect(0, 0, W, H);
      this._grid();
      const f = this.frame;
      if (f && f.present && f.landmarks && f.landmarks.length) {
        this._skeleton(f);
      } else {
        ctx.fillStyle = "#8b949e";
        ctx.font = "15px sans-serif";
        ctx.fillText("等待姿态数据…（无后端时用 mock 演示）", W / 2 - 165, H / 2);
      }
      if (this.editor) this.editor.draw(ctx);
      this._fps();
      requestAnimationFrame(this.loop);
    }

    _grid() {
      const ctx = this.ctx, W = this.W, H = this.H;
      ctx.strokeStyle = "#161b22";
      ctx.lineWidth = 1;
      for (let x = 0; x <= W; x += W / 12) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke(); }
      for (let y = 0; y <= H; y += H / 12) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke(); }
    }

    _skeleton(f) {
      const ctx = this.ctx, W = this.W, H = this.H;
      const lm = f.landmarks;
      const vis = f.visibility || [];
      const on = (i) => (vis.length ? vis[i] > 0.5 : true);
      const px = (p) => (1 - p[0]) * W;   // 镜像：像照镜子
      const py = (p) => p[1] * H;

      ctx.lineWidth = 4;
      ctx.strokeStyle = "#58a6ff";
      ctx.lineCap = "round";
      for (const [a, b] of CONNECTIONS) {
        if (!lm[a] || !lm[b]) continue;
        ctx.globalAlpha = on(a) && on(b) ? 0.95 : 0.22;
        ctx.beginPath();
        ctx.moveTo(px(lm[a]), py(lm[a]));
        ctx.lineTo(px(lm[b]), py(lm[b]));
        ctx.stroke();
      }
      ctx.globalAlpha = 1;
      for (let i = 0; i < lm.length; i++) {
        ctx.fillStyle = on(i) ? "#e6edf3" : "#30363d";
        ctx.beginPath();
        ctx.arc(px(lm[i]), py(lm[i]), 4.5, 0, Math.PI * 2);
        ctx.fill();
      }
      this._headArrow(lm, px, py);
    }

    _headArrow(lm, px, py) {
      if (!this.head || !lm[0]) return;
      const ctx = this.ctx;
      const yaw = (this.head.yaw || 0) * Math.PI / 180;
      const pitch = (this.head.pitch || 0);
      const cx = px(lm[0]), cy = py(lm[0]);
      const r = 52;
      const tx = cx - Math.sin(yaw) * r;
      const ty = cy - Math.cos(yaw) * 14 - pitch * 0.5;
      ctx.strokeStyle = "#f0a33b";
      ctx.lineWidth = 3;
      ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(tx, ty); ctx.stroke();
      ctx.beginPath(); ctx.arc(tx, ty, 5, 0, Math.PI * 2); ctx.fillStyle = "#f0a33b"; ctx.fill();
    }

    _fps() {
      this.counted++;
      const now = performance.now();
      if (now - this.fpsTimer >= 500) {
        this.fps = Math.round(this.counted / ((now - this.fpsTimer) / 1000));
        this.counted = 0;
        this.fpsTimer = now;
        if (this.dom.fps) this.dom.fps.textContent = this.fps + " FPS";
      }
    }
  }

  window.CL.Skeleton = Skeleton;
})();
