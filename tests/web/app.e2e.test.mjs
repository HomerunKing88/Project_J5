// 브라우저 e2e (J5-006). 헤드리스 Chromium 을 CDP 로 구동한다. 브라우저가 없으면 건너뛴다.
// 흐름: 설정 → 가상 시드 → 관측 저장(사진 포함) → 재접속 후 목록 유지 → 오프라인 재접속(서비스 워커) →
//       IndexedDB 내용을 패키지 폴더로 꺼내 `python -m j5 inspect` 가 ok 를 내는지 확인.
import test from "node:test";
import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { createServer } from "node:http";
import { existsSync, mkdtempSync, mkdirSync, readFileSync, writeFileSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, extname, resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { createHash } from "node:crypto";
import zlib from "node:zlib";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const WEB = join(ROOT, "web");
const MIME = { ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css", ".json": "application/json", ".svg": "image/svg+xml" };

function findChrome() {
  const cands = [process.env.J5_CHROME, "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"];
  try { for (const d of readdirSync("/opt/pw-browsers")) if (d.startsWith("chromium-")) cands.push(`/opt/pw-browsers/${d}/chrome-linux/chrome`); } catch {}
  for (const c of cands) if (c && existsSync(c)) return c;
  for (const name of ["google-chrome", "chromium-browser", "chromium", "chrome"]) {
    const r = spawnSync("which", [name]);
    if (r.status === 0) return r.stdout.toString().trim();
  }
  return null;
}

function serve(dir) {
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

function png1x1(rgb) {
  const chunk = (tag, data) => { const len = Buffer.alloc(4); len.writeUInt32BE(data.length); const td = Buffer.concat([Buffer.from(tag), data]); const crc = Buffer.alloc(4); crc.writeUInt32BE(zlib.crc32(td) >>> 0); return Buffer.concat([len, td, crc]); };
  const ihdr = Buffer.alloc(13); ihdr.writeUInt32BE(1, 0); ihdr.writeUInt32BE(1, 4); ihdr[8] = 8; ihdr[9] = 2;
  return Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), chunk("IHDR", ihdr), chunk("IDAT", zlib.deflateSync(Buffer.from([0, ...rgb]))), chunk("IEND", Buffer.alloc(0))]);
}

class Cdp {
  static async launch(chrome, tmp) {
    const port = 9500 + Math.floor(Math.random() * 400);
    const proc = spawn(chrome, ["--headless=new", "--no-sandbox", "--disable-gpu", `--remote-debugging-port=${port}`, `--user-data-dir=${join(tmp, "profile")}`, "about:blank"], { stdio: "ignore" });
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    let page;
    for (let i = 0; i < 50 && !page; i++) {
      try { page = await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: "PUT" })).json(); } catch { await sleep(200); }
    }
    if (!page) { proc.kill(); throw new Error("Chromium CDP 연결 실패"); }
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
  send(method, params = {}) { return new Promise((res, rej) => { const i = ++this.id; this.pending.set(i, (d) => d.error ? rej(new Error(d.error.message)) : res(d.result)); this.ws.send(JSON.stringify({ id: i, method, params })); }); }
  async eval(expression) { const r = await this.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true }); if (r.exceptionDetails) throw new Error(r.exceptionDetails.text + " " + (r.exceptionDetails.exception?.description || "")); return r.result.value; }
  async waitFor(expression, ms = 8000) { const end = Date.now() + ms; while (Date.now() < end) { if (await this.eval(expression)) return true; await new Promise((r) => setTimeout(r, 100)); } throw new Error("timeout: " + expression); }
  async navigate(url) { await this.send("Page.navigate", { url }); await this.waitFor("document.readyState === 'complete'"); }
  async setFiles(selector, files) { const { root } = await this.send("DOM.getDocument"); const { nodeId } = await this.send("DOM.querySelector", { nodeId: root.nodeId, selector }); await this.send("DOM.setFileInputFiles", { nodeId, files }); }
  close() { try { this.ws.close(); } catch {} this.proc.kill(); }
}

const chrome = findChrome();

test("앱 e2e: 설정·시드·관측 저장·재접속·오프라인·j5 inspect", { skip: chrome ? false : "Chromium 없음" }, async () => {
  const tmp = mkdtempSync(join(tmpdir(), "j5-e2e-"));
  const srv = await serve(WEB);
  const base = `http://127.0.0.1:${srv.address().port}`;
  const cdp = await Cdp.launch(chrome, tmp);
  try {
    await cdp.navigate(`${base}/index.html`);
    await cdp.waitFor("document.getElementById('status-line').textContent.includes('앱 0.1.0')");
    // 설정
    await cdp.eval("document.getElementById('study-id').value = 'e2e-study'; document.getElementById('save-settings').click(); 'ok'");
    await cdp.waitFor("document.getElementById('settings-note').textContent === '저장됨'");
    // 시드
    await cdp.eval("document.getElementById('load-synthetic').click(); 'ok'");
    await cdp.waitFor("document.querySelectorAll('#asset-list li button').length === 5");
    // 관측 (사진 1장, 태그 1개)
    const photo = join(tmp, "front.png");
    writeFileSync(photo, png1x1([0, 128, 255]));
    await cdp.eval("document.querySelectorAll('#asset-list li button')[0].click(); 'ok'");
    await cdp.waitFor("!document.getElementById('sec-observe').hidden");
    await cdp.eval("document.getElementById('change-status').value = 'change_observed'; document.getElementById('note').value = '1층 임대 광고 (e2e)'; 'ok'");
    await cdp.setFiles("#photos", [photo]);
    await cdp.waitFor("document.querySelectorAll('#photo-list .photo-item').length === 1 && document.querySelector('#photo-list .tags')");
    await cdp.eval("document.querySelector('#photo-list .tags input').click(); 'ok'");
    await cdp.eval("document.getElementById('save-observation').click(); 'ok'");
    await cdp.waitFor("document.getElementById('observe-note').textContent.startsWith('저장됨')");
    // 미지원 사진(HEIC 시그니처)은 오류로 표시되고 저장이 막힌다
    const heic = join(tmp, "x.heic");
    writeFileSync(heic, Buffer.concat([Buffer.from([0, 0, 0, 0x18]), Buffer.from("ftypheic"), Buffer.alloc(20)]));
    await cdp.eval("document.querySelectorAll('#asset-list li button')[1].click(); 'ok'");
    await cdp.setFiles("#photos", [heic]);
    await cdp.waitFor("document.querySelector('#photo-list .bad')?.textContent.includes('HEIC')");
    await cdp.eval("document.getElementById('change-status').value = 'no_change'; document.getElementById('save-observation').click(); 'ok'");
    await cdp.waitFor("document.getElementById('observe-note').textContent.includes('오류가 있는 사진')");
    await cdp.eval("document.getElementById('cancel-observation').click(); 'ok'");
    // 재접속: 기록 유지
    await cdp.navigate(`${base}/index.html`);
    await cdp.waitFor("document.querySelectorAll('#record-list li').length === 1");
    assert.ok((await cdp.eval("document.getElementById('status-line').textContent")).includes("관측 1"));
    // 오프라인 재접속: 정적 서버를 실제로 내린 뒤에도 서비스 워커 캐시로 앱이 뜨고 기록이 남는다 (127.0.0.1 은 보안 컨텍스트).
    // CDP 네트워크 에뮬레이션은 서비스 워커의 요청에 적용되지 않으므로 서버를 내린다.
    await cdp.waitFor("navigator.serviceWorker.ready.then(() => true)");
    await cdp.waitFor("caches.keys().then(k => k.length > 0)");
    srv.closeAllConnections();
    await new Promise((r) => srv.close(r));
    await cdp.navigate(`${base}/index.html`);
    await cdp.waitFor("document.querySelectorAll('#record-list li').length === 1");
    // 캐시 목록에 없는 시드 파일 요청은 실패해야 한다 (서비스 워커가 데이터를 캐시하지 않음). 이 탐침이 남기는 로드 실패 로그는 예상된 것이라 걷어낸다.
    const errorsBefore = cdp.errors.length;
    assert.equal(await cdp.eval("fetch('data/assets.seed.synthetic.json', { cache: 'no-store' }).then(() => 'reachable').catch(() => 'blocked')"), "blocked");
    await new Promise((r) => setTimeout(r, 200));
    const probeLogs = cdp.errors.splice(errorsBefore);
    assert.ok(probeLogs.every((e) => e.includes("ERR_FAILED") || e.includes("Failed to load resource")), probeLogs.join("; "));
    assert.ok((await cdp.eval("document.getElementById('status-line').textContent")).includes("관측 1"));
    // IndexedDB 내용을 패키지 폴더로 꺼내 PC 검사기로 확인
    const dump = await cdp.eval(`(async () => {
      const db = await new Promise((res, rej) => { const r = indexedDB.open('j5', 1); r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error); });
      const all = (s) => new Promise((res, rej) => { const r = db.transaction(s).objectStore(s).getAll(); r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error); });
      const events = await all('events'); const photos = await all('photos');
      const b64 = async (blob) => { const buf = new Uint8Array(await blob.arrayBuffer()); let s = ''; for (const x of buf) s += String.fromCharCode(x); return btoa(s); };
      return { events: events.map(e => ({ line: Array.from(e.line), hash: e.event_hash, status: e.status })), photos: await Promise.all(photos.map(async p => ({ sha256: p.sha256, ext: p.ext, b64: await b64(p.blob) }))) };
    })()`);
    assert.equal(dump.events.length, 1);
    assert.equal(dump.events[0].status, "saved");
    const pkg = join(tmp, "pkg"); mkdirSync(join(pkg, "photos"), { recursive: true });
    const obs = Buffer.concat(dump.events.map((e) => Buffer.from(e.line)));
    assert.equal(createHash("sha256").update(obs).digest("hex"), dump.events[0].hash, "저장된 해시 = 행 바이트(LF 포함) 해시");
    writeFileSync(join(pkg, "observations.jsonl"), obs);
    const files = [{ path: "observations.jsonl", bytes: obs.length, sha256: createHash("sha256").update(obs).digest("hex") }];
    for (const p of dump.photos) { const buf = Buffer.from(p.b64, "base64"); writeFileSync(join(pkg, "photos", `${p.sha256}.${p.ext}`), buf); files.push({ path: `photos/${p.sha256}.${p.ext}`, bytes: buf.length, sha256: createHash("sha256").update(buf).digest("hex") }); }
    writeFileSync(join(pkg, "manifest.json"), JSON.stringify({ format: "j5field", schema_version: "1.0.0", study_id: "e2e-study", package_id: "5e5e0000-0000-4000-8000-00000000e2e0", created_at: "2026-09-22T09:00:00+09:00", data_mode: "synthetic", files }, null, 2) + "\n");
    const py = spawnSync("python3", ["-m", "j5", "inspect", pkg, "--seed", join(ROOT, "tests/fixtures/assets.seed.synthetic.json"), "--study-id", "e2e-study", "--json"], { cwd: ROOT, encoding: "utf8" });
    if (py.error?.code === "ENOENT") { console.log("python3 없음: inspect 단계 건너뜀"); }
    else {
      assert.equal(py.status, 0, py.stdout + py.stderr);
      const rep = JSON.parse(py.stdout);
      assert.equal(rep.verdict, "ok");
      assert.equal(rep.counts.events, 1);
      assert.equal(rep.counts.photos_referenced, 1);
    }
    assert.deepEqual(cdp.errors, [], "브라우저 콘솔 오류 없음");
  } finally {
    cdp.close();
    try { srv.closeAllConnections(); srv.close(); } catch {}
  }
});
