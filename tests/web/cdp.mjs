// 브라우저 e2e 공용 헬퍼 (J5-006·J5-020). 헤드리스 Chromium 을 CDP 로 구동한다. 테스트 파일이 아니다(*.test.mjs 가 아님).
import test from "node:test";
import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { createServer } from "node:http";
import { existsSync, mkdtempSync, mkdirSync, readFileSync, writeFileSync, readdirSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, extname, resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { createHash } from "node:crypto";
import zlib from "node:zlib";

export const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
export const WEB = join(ROOT, "web");
export const MIME = { ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css", ".json": "application/json", ".svg": "image/svg+xml" };

export function findChrome() {
  const cands = [process.env.J5_CHROME, "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"];
  try { for (const d of readdirSync("/opt/pw-browsers")) if (d.startsWith("chromium-")) cands.push(`/opt/pw-browsers/${d}/chrome-linux/chrome`); } catch {}
  for (const c of cands) if (c && existsSync(c)) return c;
  for (const name of ["google-chrome", "chromium-browser", "chromium", "chrome"]) {
    const r = spawnSync("which", [name]);
    if (r.status === 0) return r.stdout.toString().trim();
  }
  return null;
}

export function serve(dir) {
  return new Promise((res) => {
    const srv = createServer((req, resp) => {
      const path = decodeURIComponent(new URL(req.url, "http://x").pathname);
      const file = join(dir, path === "/" ? "index.html" : path);
      if (!file.startsWith(dir) || !existsSync(file)) { resp.writeHead(404); return resp.end(); }
      resp.writeHead(200, { "content-type": MIME[extname(file)] || "application/octet-stream", "cache-control": "no-store" });
      resp.end(readFileSync(file));
    });
    srv.listen(0, "127.0.0.1", () => res(srv));
  });
}

export function png1x1(rgb) {
  const chunk = (tag, data) => { const len = Buffer.alloc(4); len.writeUInt32BE(data.length); const td = Buffer.concat([Buffer.from(tag), data]); const crc = Buffer.alloc(4); crc.writeUInt32BE(zlib.crc32(td) >>> 0); return Buffer.concat([len, td, crc]); };
  const ihdr = Buffer.alloc(13); ihdr.writeUInt32BE(1, 0); ihdr.writeUInt32BE(1, 4); ihdr[8] = 8; ihdr[9] = 2;
  return Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), chunk("IHDR", ihdr), chunk("IDAT", zlib.deflateSync(Buffer.from([0, ...rgb]))), chunk("IEND", Buffer.alloc(0))]);
}

export class Cdp {
  static async launch(chrome, tmp) {
    const port = 9500 + Math.floor(Math.random() * 400);
    const proc = spawn(chrome, ["--headless=new", "--no-sandbox", "--disable-gpu", `--remote-debugging-port=${port}`, `--user-data-dir=${join(tmp, "profile")}`, "about:blank"], { stdio: "ignore" });
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    // CI 러너의 첫 Chrome 기동은 10초를 넘길 수 있다 (0e59df6 의 web 작업이 10초에서 실패). 최대 60초 기다린다.
    let page;
    for (let i = 0; i < 300 && !page; i++) {
      try { page = await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: "PUT" })).json(); } catch { await sleep(200); }
    }
    if (!page) { proc.kill(); throw new Error("Chromium CDP 연결 실패 (60초)"); }
    const c = new Cdp(proc, new WebSocket(page.webSocketDebuggerUrl));
    await new Promise((r) => (c.ws.onopen = r));
    for (const m of ["Runtime.enable", "Page.enable", "Log.enable", "Network.enable", "DOM.enable"]) await c.send(m);
    return c;
  }
  constructor(proc, ws) {
    this.proc = proc; this.ws = ws; this.id = 0; this.pending = new Map(); this.errors = [];
    ws.onmessage = (m) => {
      const d = JSON.parse(m.data);
      if (d.id && this.pending.has(d.id)) { this.pending.get(d.id)(d); this.pending.delete(d.id); }
      else if (d.method === "Runtime.exceptionThrown") this.errors.push(JSON.stringify(d.params.exceptionDetails).slice(0, 400));
      else if (d.method === "Log.entryAdded" && d.params.entry.level === "error") this.errors.push(d.params.entry.text.slice(0, 300));
    };
  }
  send(method, params = {}, timeoutMs = 20000) {
    // 응답이 없는 명령은 멈춤 대신 오류로 끝낸다 (CI 에서 무한 대기 방지).
    return new Promise((res, rej) => {
      const i = ++this.id;
      const timer = setTimeout(() => { this.pending.delete(i); rej(new Error(`CDP ${method} 응답 없음 (${timeoutMs}ms)`)); }, timeoutMs);
      this.pending.set(i, (d) => { clearTimeout(timer); d.error ? rej(new Error(`${method}: ${d.error.message}`)) : res(d.result); });
      this.ws.send(JSON.stringify({ id: i, method, params }));
    });
  }
  async eval(expression) { const r = await this.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true }); if (r.exceptionDetails) throw new Error(r.exceptionDetails.text + " " + (r.exceptionDetails.exception?.description || "")); return r.result.value; }
  async waitFor(expression, ms = 8000) { const end = Date.now() + ms; while (Date.now() < end) { if (await this.eval(expression)) return true; await new Promise((r) => setTimeout(r, 100)); } throw new Error("timeout: " + expression); }
  async navigate(url) { await this.send("Page.navigate", { url }); await this.waitFor("document.readyState === 'complete'"); }
  async setFiles(selector, files) { const { root } = await this.send("DOM.getDocument"); const { nodeId } = await this.send("DOM.querySelector", { nodeId: root.nodeId, selector }); await this.send("DOM.setFileInputFiles", { nodeId, files }); }
  /** 요소를 화면 가운데로 옮기고 위치가 두 번 연속 같을 때까지 기다린다. 앱의 smooth 스크롤(관측 화면 열기)이 진행 중이면 좌표가 움직여 클릭이 빗나간다. */
  async stableRect(selector) {
    const measure = () => this.eval(`(() => { const n = document.querySelector(${JSON.stringify(selector)}); if (!n) return null; const b = n.getBoundingClientRect(); return { x: b.left + b.width / 2, y: b.top + b.height / 2 }; })()`);
    await this.eval(`document.querySelector(${JSON.stringify(selector)}).scrollIntoView({ block: 'center', behavior: 'instant' }); 'ok'`);
    let prev = await measure();
    for (let i = 0; i < 30; i++) {
      await new Promise((r) => setTimeout(r, 80));
      const cur = await measure();
      if (!cur) throw new Error(`요소 없음: ${selector}`);
      if (prev && Math.abs(cur.x - prev.x) < 0.5 && Math.abs(cur.y - prev.y) < 0.5) return cur;
      prev = cur;
    }
    throw new Error(`요소 위치가 안정되지 않음: ${selector}`);
  }
  /** 실제 마우스 이벤트로 클릭한다 (다운로드에는 사용자 활성화가 필요하다). */
  async clickSelector(selector) {
    await this.stableRect(selector);
    const { root } = await this.send("DOM.getDocument");
    const { nodeId } = await this.send("DOM.querySelector", { nodeId: root.nodeId, selector });
    const { model } = await this.send("DOM.getBoxModel", { nodeId });
    const q = model.content;
    const x = (q[0] + q[2] + q[4] + q[6]) / 4, y = (q[1] + q[3] + q[5] + q[7]) / 4;
    await this.send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
    await this.send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 });
    await this.send("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 });
  }
  /** getBoundingClientRect 중심을 실제 마우스로 누른다 (SVG 자식은 DOM.getBoxModel 이 불확실하다). 위치가 안정된 뒤 누른다. */
  async clickRect(selector) {
    const r = await this.stableRect(selector);
    await this.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: r.x, y: r.y });
    await this.send("Input.dispatchMouseEvent", { type: "mousePressed", x: r.x, y: r.y, button: "left", clickCount: 1 });
    await this.send("Input.dispatchMouseEvent", { type: "mouseReleased", x: r.x, y: r.y, button: "left", clickCount: 1 });
  }
  close() { try { this.ws.close(); } catch {} this.proc.kill(); }
}

// 마커 중심을 svg 기준 좌표로 (스크롤에 영향받지 않게)
export const MARKER_POS = "(() => { const s = document.getElementById('map-svg').getBoundingClientRect(); return Array.from(document.querySelectorAll('#map-svg g.pt')).map(g => { const b = g.querySelector('.dot').getBoundingClientRect(); return [g.dataset.assetId, b.left + b.width / 2 - s.left, b.top + b.height / 2 - s.top]; }); })()";

export async function waitForDownloads(dir, count, ms = 15000) {
  const end = Date.now() + ms;
  let last = null;
  while (Date.now() < end) {
    const files = readdirSync(dir).filter((f) => f.endsWith(".j5field.zip")).sort((a, b) => statSync(join(dir, a)).mtimeMs - statSync(join(dir, b)).mtimeMs);
    const partial = readdirSync(dir).some((f) => f.endsWith(".crdownload"));
    if (files.length >= count && !partial) {
      const newest = files[files.length - 1];
      const size = statSync(join(dir, newest)).size;
      if (last === `${newest}:${size}` && size > 0) return newest;
      last = `${newest}:${size}`;
    }
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error(`다운로드 ${count}개를 기다리다 시간 초과: ${readdirSync(dir).join(",")}`);
}
