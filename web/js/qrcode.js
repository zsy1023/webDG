/* qrcode.js —— 极简 QR 编码器（byte 模式 / UTF-8），零依赖，输出 SVG
 *
 * 为什么手写：项目硬约束「不引 CDN / 不引构建工具」，而房间邀请链接需要一个二维码。
 *
 * 算法参照 Nayuki 的 QR Code generator（MIT）精简：只保留 byte 模式，
 * 支持版本 1–10（够放 URL 与房间码）、4 个纠错级别、8 个掩码按惩罚分择优。
 * 内容超出容量会抛错并给出提示 —— 本面板只用来编码邀请链接，够用。
 *
 * 用法：
 *   const svg = CL.QR.svg("http://192.168.1.5:8780/?room=AB12CD", { ecl: "M" });
 *   box.appendChild(svg);
 *   CL.QR.matrix(text)   // 只拿 0/1 矩阵（自测用）
 */
(function () {
  "use strict";
  window.CL = window.CL || {};

  const MAX_VER = 10;

  //: 每块的纠错码字数（索引 = 版本 - 1）
  const ECC_PER_BLOCK = {
    L: [7, 10, 15, 20, 26, 18, 20, 24, 30, 18],
    M: [10, 16, 26, 18, 24, 16, 18, 22, 22, 26],
    Q: [13, 22, 18, 26, 18, 24, 18, 22, 20, 24],
    H: [17, 28, 22, 16, 22, 28, 26, 26, 24, 28],
  };
  //: 纠错块数（索引 = 版本 - 1）
  const NUM_BLOCKS = {
    L: [1, 1, 1, 1, 1, 2, 2, 2, 2, 4],
    M: [1, 1, 1, 2, 2, 4, 4, 4, 5, 5],
    Q: [1, 1, 2, 2, 4, 4, 6, 6, 8, 8],
    H: [1, 1, 2, 4, 4, 4, 5, 6, 8, 8],
  };
  //: 格式信息里的 2 位纠错级别编码
  const ECL_FMT = { L: 1, M: 0, Q: 3, H: 2 };

  function rawDataModules(ver) {
    let n = (16 * ver + 128) * ver + 64;
    if (ver >= 2) {
      const align = Math.floor(ver / 7) + 2;
      n -= (25 * align - 10) * align - 55;
      if (ver >= 7) n -= 36;
    }
    return n;
  }
  function dataCodewords(ver, ecl) {
    return Math.floor(rawDataModules(ver) / 8)
      - ECC_PER_BLOCK[ecl][ver - 1] * NUM_BLOCKS[ecl][ver - 1];
  }

  /* ── GF(256) 与 Reed–Solomon ── */
  const EXP = new Uint8Array(512);
  const LOG = new Uint8Array(256);
  (function initGF() {
    let x = 1;
    for (let i = 0; i < 255; i++) {
      EXP[i] = x;
      LOG[x] = i;
      x = (x << 1) ^ ((x & 0x80) ? 0x11d : 0);
    }
    for (let i = 255; i < 512; i++) EXP[i] = EXP[i - 255];
  })();
  const gfMul = (a, b) => (a === 0 || b === 0 ? 0 : EXP[LOG[a] + LOG[b]]);

  function rsDivisor(degree) {
    const result = new Uint8Array(degree);
    result[degree - 1] = 1;
    let root = 1;
    for (let i = 0; i < degree; i++) {
      for (let j = 0; j < degree; j++) {
        result[j] = gfMul(result[j], root);
        if (j + 1 < degree) result[j] ^= result[j + 1];
      }
      root = gfMul(root, 0x02);
    }
    return result;
  }
  function rsRemainder(data, divisor) {
    const result = new Uint8Array(divisor.length);
    for (const b of data) {
      const factor = b ^ result[0];
      result.copyWithin(0, 1);
      result[result.length - 1] = 0;
      for (let i = 0; i < result.length; i++) result[i] ^= gfMul(divisor[i], factor);
    }
    return result;
  }

  const getBit = (x, i) => ((x >>> i) & 1) !== 0;

  /* ── 1. 编码成码字 ── */
  function encode(text, ecl) {
    const bytes = new TextEncoder().encode(text);
    let ver = 0;
    for (let v = 1; v <= MAX_VER; v++) {
      const ccBits = v < 10 ? 8 : 16;
      if (4 + ccBits + bytes.length * 8 <= dataCodewords(v, ecl) * 8) { ver = v; break; }
    }
    if (!ver) {
      throw new Error(`内容太长（${bytes.length} 字节）：超出 QR 版本 ${MAX_VER} 的容量`);
    }
    const bits = [];
    const push = (val, len) => { for (let i = len - 1; i >= 0; i--) bits.push((val >>> i) & 1); };
    push(4, 4);                                        // 模式指示符：byte
    push(bytes.length, ver < 10 ? 8 : 16);             // 字符计数
    for (const b of bytes) push(b, 8);
    const cap = dataCodewords(ver, ecl) * 8;
    push(0, Math.min(4, cap - bits.length));           // 终止符
    push(0, (8 - (bits.length % 8)) % 8);              // 补齐到字节
    for (let pad = 0xec; bits.length < cap; pad ^= 0xec ^ 0x11) push(pad, 8);   // 填充字节
    const data = new Uint8Array(bits.length / 8);
    bits.forEach((b, i) => { data[i >> 3] |= b << (7 - (i & 7)); });
    return { ver, data };
  }

  /* ── 2. 分块 + 纠错 + 交错 ── */
  function addEcc(data, ver, ecl) {
    const numBlocks = NUM_BLOCKS[ecl][ver - 1];
    const eccLen = ECC_PER_BLOCK[ecl][ver - 1];
    const rawCodewords = Math.floor(rawDataModules(ver) / 8);
    const shortCount = numBlocks - (rawCodewords % numBlocks);
    const shortLen = Math.floor(rawCodewords / numBlocks);
    const divisor = rsDivisor(eccLen);

    const blocks = [];
    for (let i = 0, k = 0; i < numBlocks; i++) {
      const len = shortLen - eccLen + (i < shortCount ? 0 : 1);
      const dat = Array.from(data.slice(k, k + len));
      k += len;
      const ecc = Array.from(rsRemainder(dat, divisor));
      if (i < shortCount) dat.push(0);                 // 占位：交错时跳过
      blocks.push(dat.concat(ecc));
    }
    const out = [];
    for (let i = 0; i < blocks[0].length; i++) {
      for (let j = 0; j < blocks.length; j++) {
        if (i !== shortLen - eccLen || j >= shortCount) out.push(blocks[j][i]);
      }
    }
    return out;
  }

  /* ── 3. 矩阵 ── */
  function newMatrix(ver) {
    const size = ver * 4 + 17;
    return {
      ver,
      size,
      mod: Array.from({ length: size }, () => new Uint8Array(size)),
      fn: Array.from({ length: size }, () => new Uint8Array(size)),
    };
  }
  const setFn = (m, x, y, dark) => {
    if (x < 0 || y < 0 || x >= m.size || y >= m.size) return;
    m.mod[y][x] = dark ? 1 : 0;
    m.fn[y][x] = 1;
  };

  function drawFinder(m, cx, cy) {
    for (let dy = -4; dy <= 4; dy++) {
      for (let dx = -4; dx <= 4; dx++) {
        const d = Math.max(Math.abs(dx), Math.abs(dy));
        setFn(m, cx + dx, cy + dy, d !== 2 && d !== 4);
      }
    }
  }
  function drawAlign(m, cx, cy) {
    for (let dy = -2; dy <= 2; dy++) {
      for (let dx = -2; dx <= 2; dx++) {
        setFn(m, cx + dx, cy + dy, Math.max(Math.abs(dx), Math.abs(dy)) !== 1);
      }
    }
  }
  function alignPositions(ver) {
    if (ver === 1) return [];
    const n = Math.floor(ver / 7) + 2;
    const step = Math.ceil((ver * 4 + 4) / (n * 2 - 2)) * 2;
    const pos = [6];
    for (let p = ver * 4 + 10; pos.length < n; p -= step) pos.splice(1, 0, p);
    return pos;
  }
  function drawFormat(m, ecl, mask) {
    const data = (ECL_FMT[ecl] << 3) | mask;
    let rem = data;
    for (let i = 0; i < 10; i++) rem = (rem << 1) ^ ((rem >>> 9) * 0x537);
    const bits = ((data << 10) | rem) ^ 0x5412;
    const size = m.size;
    for (let i = 0; i <= 5; i++) setFn(m, 8, i, getBit(bits, i));
    setFn(m, 8, 7, getBit(bits, 6));
    setFn(m, 8, 8, getBit(bits, 7));
    setFn(m, 7, 8, getBit(bits, 8));
    for (let i = 9; i < 15; i++) setFn(m, 14 - i, 8, getBit(bits, i));
    for (let i = 0; i < 8; i++) setFn(m, size - 1 - i, 8, getBit(bits, i));
    for (let i = 8; i < 15; i++) setFn(m, 8, size - 15 + i, getBit(bits, i));
    setFn(m, 8, size - 8, true);                       // 固定暗模块
  }
  function drawVersion(m) {
    if (m.ver < 7) return;
    let rem = m.ver;
    for (let i = 0; i < 12; i++) rem = (rem << 1) ^ ((rem >>> 11) * 0x1f25);
    const bits = (m.ver << 12) | rem;
    for (let i = 0; i < 18; i++) {
      const b = getBit(bits, i);
      const a = m.size - 11 + (i % 3);
      const c = Math.floor(i / 3);
      setFn(m, a, c, b);
      setFn(m, c, a, b);
    }
  }
  function drawFunctionPatterns(m, ecl) {
    for (let i = 0; i < m.size; i++) {
      setFn(m, 6, i, i % 2 === 0);
      setFn(m, i, 6, i % 2 === 0);
    }
    drawFinder(m, 3, 3);
    drawFinder(m, m.size - 4, 3);
    drawFinder(m, 3, m.size - 4);
    const pos = alignPositions(m.ver);
    const n = pos.length;
    for (let i = 0; i < n; i++) {
      for (let j = 0; j < n; j++) {
        if ((i === 0 && j === 0) || (i === 0 && j === n - 1) || (i === n - 1 && j === 0)) continue;
        drawAlign(m, pos[i], pos[j]);
      }
    }
    drawFormat(m, ecl, 0);
    drawVersion(m);
  }
  function drawCodewords(m, codewords) {
    let i = 0;
    const total = codewords.length * 8;
    for (let right = m.size - 1; right >= 1; right -= 2) {
      if (right === 6) right = 5;
      for (let vert = 0; vert < m.size; vert++) {
        for (let j = 0; j < 2; j++) {
          const x = right - j;
          const upward = ((right + 1) & 2) === 0;
          const y = upward ? m.size - 1 - vert : vert;
          if (!m.fn[y][x] && i < total) {
            m.mod[y][x] = getBit(codewords[i >> 3], 7 - (i & 7)) ? 1 : 0;
            i++;
          }
        }
      }
    }
  }

  const MASKS = [
    (x, y) => (x + y) % 2 === 0,
    (x, y) => y % 2 === 0,
    (x, y) => x % 3 === 0,
    (x, y) => (x + y) % 3 === 0,
    (x, y) => (Math.floor(y / 2) + Math.floor(x / 3)) % 2 === 0,
    (x, y) => ((x * y) % 2) + ((x * y) % 3) === 0,
    (x, y) => (((x * y) % 2) + ((x * y) % 3)) % 2 === 0,
    (x, y) => (((x + y) % 2) + ((x * y) % 3)) % 2 === 0,
  ];
  function applyMask(m, mask) {
    for (let y = 0; y < m.size; y++) {
      for (let x = 0; x < m.size; x++) {
        if (!m.fn[y][x] && MASKS[mask](x, y)) m.mod[y][x] ^= 1;
      }
    }
  }

  /** 掩码惩罚分（QR 规范的四条规则，用于挑最"好扫"的掩码） */
  function penalty(m) {
    const size = m.size;
    let result = 0;
    // ① 行/列里连续同色的长度
    for (let y = 0; y < size; y++) {
      let run = 0, color = -1;
      for (let x = 0; x < size; x++) {
        const c = m.mod[y][x];
        if (c === color) { run++; if (run === 5) result += 3; else if (run > 5) result++; }
        else { color = c; run = 1; }
      }
    }
    for (let x = 0; x < size; x++) {
      let run = 0, color = -1;
      for (let y = 0; y < size; y++) {
        const c = m.mod[y][x];
        if (c === color) { run++; if (run === 5) result += 3; else if (run > 5) result++; }
        else { color = c; run = 1; }
      }
    }
    // ② 2×2 同色块
    for (let y = 0; y < size - 1; y++) {
      for (let x = 0; x < size - 1; x++) {
        const c = m.mod[y][x];
        if (c === m.mod[y][x + 1] && c === m.mod[y + 1][x] && c === m.mod[y + 1][x + 1]) result += 3;
      }
    }
    // ③ 形如定位图案的 1:1:3:1:1（前后各留 4 空白）
    const P1 = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0];
    const P2 = [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1];
    const matches = (get, at, len, pat) => {
      for (let k = 0; k + pat.length <= len; k++) {
        let ok = true;
        for (let t = 0; t < pat.length; t++) if (get(at(k + t)) !== pat[t]) { ok = false; break; }
        if (ok) return true;
      }
      return false;
    };
    for (let y = 0; y < size; y++) {
      const row = (i) => m.mod[y][i];
      if (matches(row, (i) => i, size, P1) || matches(row, (i) => i, size, P2)) result += 40;
    }
    for (let x = 0; x < size; x++) {
      const col = (i) => m.mod[i][x];
      if (matches(col, (i) => i, size, P1) || matches(col, (i) => i, size, P2)) result += 40;
    }
    // ④ 深色比例偏离 50%
    let dark = 0;
    for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) dark += m.mod[y][x];
    const total = size * size;
    result += Math.ceil(Math.abs(dark * 20 - total * 10) / total) * 10;
    return result;
  }

  function buildMatrix(ver, data, ecl) {
    const m = newMatrix(ver);
    drawFunctionPatterns(m, ecl);
    drawCodewords(m, addEcc(data, ver, ecl));

    let bestMask = 0, bestScore = Infinity;
    for (let mask = 0; mask < 8; mask++) {
      applyMask(m, mask);
      drawFormat(m, ecl, mask);
      const score = penalty(m);
      if (score < bestScore) { bestScore = score; bestMask = mask; }
      applyMask(m, mask);                              // 异或回去，试下一个
    }
    applyMask(m, bestMask);
    drawFormat(m, ecl, bestMask);
    return m;
  }

  /* ── 4. 输出 SVG（矢量，任意尺寸都清晰）── */
  function toSvg(m, opts) {
    const o = opts || {};
    const border = o.border == null ? 3 : o.border;
    const dim = m.size + border * 2;
    let d = "";
    for (let y = 0; y < m.size; y++) {
      for (let x = 0; x < m.size; x++) {
        if (m.mod[y][x]) d += "M" + (x + border) + " " + (y + border) + "h1v1h-1z";
      }
    }
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 " + dim + " " + dim);
    svg.setAttribute("shape-rendering", "crispEdges");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", o.label || "二维码");
    // 深色模块 + 浅色底（反色二维码有些扫码器不认，所以坚持标准配色）
    const light = o.light || "#f6f1e4";
    const dark = o.dark || "#14120d";
    const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    rect.setAttribute("width", String(dim));
    rect.setAttribute("height", String(dim));
    rect.setAttribute("fill", light);
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", d);
    path.setAttribute("fill", dark);
    svg.appendChild(rect);
    svg.appendChild(path);
    return svg;
  }

  window.CL.QR = {
    svg(text, opts) {
      const ecl = (opts && opts.ecl) || "M";
      const { ver, data } = encode(String(text), ecl);
      return toSvg(buildMatrix(ver, data, ecl), opts || {});
    },
    /** 自测用：返回 { ver, size, rows: ["0101…", …] } */
    matrix(text, opts) {
      const ecl = (opts && opts.ecl) || "M";
      const { ver, data } = encode(String(text), ecl);
      const m = buildMatrix(ver, data, ecl);
      return {
        ver, size: m.size,
        rows: Array.from({ length: m.size }, (_, y) => Array.from(m.mod[y]).join("")),
      };
    },
    maxVersion: MAX_VER,
  };
})();
