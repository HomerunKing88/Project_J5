// 브라우저 e2e (J5-006). 헤드리스 Chromium 을 CDP 로 구동한다. 브라우저가 없으면 건너뛴다.
// 흐름: 설정 → 가상 시드 → 관측 저장(사진 포함) → 재접속 후 목록 유지 → 오프라인 재접속(서비스 워커) →
//       IndexedDB 내용을 패키지 폴더로 꺼내 `python -m j5 inspect` 가 ok 를 내는지 확인.
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
  /** 실제 마우스 이벤트로 클릭한다 (다운로드에는 사용자 활성화가 필요하다). */
  async clickSelector(selector) {
    await this.eval(`document.querySelector(${JSON.stringify(selector)}).scrollIntoView({ block: 'center' }); 'ok'`);
    const { root } = await this.send("DOM.getDocument");
    const { nodeId } = await this.send("DOM.querySelector", { nodeId: root.nodeId, selector });
    const { model } = await this.send("DOM.getBoxModel", { nodeId });
    const q = model.content;
    const x = (q[0] + q[2] + q[4] + q[6]) / 4, y = (q[1] + q[3] + q[5] + q[7]) / 4;
    await this.send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
    await this.send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 });
    await this.send("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 });
  }
  /** getBoundingClientRect 중심을 실제 마우스로 누른다 (SVG 자식은 DOM.getBoxModel 이 불확실하다). */
  async clickRect(selector) {
    const r = await this.eval(`(() => { const n = document.querySelector(${JSON.stringify(selector)}); n.scrollIntoView({ block: 'center' }); const b = n.getBoundingClientRect(); return { x: b.left + b.width / 2, y: b.top + b.height / 2 }; })()`);
    await this.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: r.x, y: r.y });
    await this.send("Input.dispatchMouseEvent", { type: "mousePressed", x: r.x, y: r.y, button: "left", clickCount: 1 });
    await this.send("Input.dispatchMouseEvent", { type: "mouseReleased", x: r.x, y: r.y, button: "left", clickCount: 1 });
  }
  close() { try { this.ws.close(); } catch {} this.proc.kill(); }
}

// 마커 중심을 svg 기준 좌표로 (스크롤에 영향받지 않게)
const MARKER_POS = "(() => { const s = document.getElementById('map-svg').getBoundingClientRect(); return Array.from(document.querySelectorAll('#map-svg g.pt')).map(g => { const b = g.querySelector('.dot').getBoundingClientRect(); return [g.dataset.assetId, b.left + b.width / 2 - s.left, b.top + b.height / 2 - s.top]; }); })()";

async function waitForDownloads(dir, count, ms = 15000) {
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

const chrome = findChrome();

test("앱 e2e: 설정·시드·관측 저장·재접속·오프라인·j5 inspect", { skip: chrome ? false : "Chromium 없음" }, async () => {
  const tmp = mkdtempSync(join(tmpdir(), "j5-e2e-"));
  const srv = await serve(WEB);
  const base = `http://127.0.0.1:${srv.address().port}`;
  let cdp;
  try {
    // 브라우저 기동 실패도 finally 로 서버를 닫아야 한다. 안 닫으면 테스트 파일이 --test-timeout 까지 매달린다.
    cdp = await Cdp.launch(chrome, tmp);
    // 지도 모듈 로드 실패: map.js 요청을 막고 첫 접속 (서비스 워커가 아직 없을 때). 목록·설정은 그대로 동작하고 지도 절만 안내를 낸다.
    await cdp.send("Network.setBlockedURLs", { urls: ["*/app/map.js"] });
    await cdp.navigate(`${base}/index.html`);
    await cdp.waitFor("document.getElementById('status-line').textContent.includes('앱 0.1.0')");
    await cdp.waitFor("document.getElementById('map-note').textContent.includes('지도 표시 불가')");
    assert.equal(await cdp.eval("document.querySelectorAll('#asset-list li').length"), 1, "지도 모듈 없이도 목록 절이 그려진다");
    const blockedLogs = cdp.errors.splice(0);
    assert.ok(blockedLogs.every((e) => e.includes("ERR_BLOCKED_BY_CLIENT") || e.includes("Failed to load resource") || e.includes("map.js")), blockedLogs.join("; "));
    await cdp.send("Network.setBlockedURLs", { urls: [] });
    await cdp.navigate(`${base}/index.html`);
    await cdp.waitFor("document.getElementById('status-line').textContent.includes('앱 0.1.0')");
    await cdp.waitFor("document.getElementById('map-note').textContent === ''");
    // 설정
    await cdp.eval("document.getElementById('study-id').value = 'e2e-study'; document.getElementById('save-settings').click(); 'ok'");
    await cdp.waitFor("document.getElementById('settings-note').textContent === '저장됨'");
    // 시드
    await cdp.eval("document.getElementById('load-synthetic').click(); 'ok'");
    await cdp.waitFor("document.querySelectorAll('#asset-list li button').length === 5");
    // 지도 (J5-005): 위치점 4개, 주소만 있는 물건 1개는 목록에서만. 마커 탭 → 관측 시작, 확대 → 좌표 변화, 전체 보기 → 복귀
    await cdp.waitFor("document.querySelectorAll('#map-svg g.pt').length === 4");
    assert.match(await cdp.eval("document.getElementById('map-note').textContent"), /위치점 4개 표시 \(가상 4 · 실제 0\) · 위치점 없는 물건 1개/);
    assert.equal(await cdp.eval("document.querySelectorAll('#map-svg g.pt.synthetic').length"), 4, "가상자료 마커 표시");
    assert.match(await cdp.eval("document.querySelector('#map-svg .layer-scale text').textContent"), /^\d+ m$/, "축척 막대");
    const pos0 = await cdp.eval(MARKER_POS);
    const svgBox = await cdp.eval("(b => [b.width, b.height])(document.getElementById('map-svg').getBoundingClientRect())");
    for (const [, x, y] of pos0) assert.ok(x > 0 && x < svgBox[0] && y > 0 && y < svgBox[1], `마커가 지도 안에 있다 ${x},${y}`);
    await cdp.clickRect('#map-svg g.pt[data-asset-id="7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a51"] .hit');
    await cdp.waitFor("!document.getElementById('sec-observe').hidden");
    assert.equal(await cdp.eval("document.getElementById('target-label').textContent"), "가상 물건 2", "마커 탭으로 관측 대상 선택");
    assert.equal(await cdp.eval("document.querySelectorAll('#map-svg g.pt.sel').length"), 1);
    assert.equal(await cdp.eval("document.querySelector('#map-svg g.pt.sel').dataset.assetId"), "7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a51");
    await cdp.eval("document.getElementById('cancel-observation').click(); 'ok'");
    await cdp.waitFor("document.getElementById('sec-observe').hidden && document.querySelectorAll('#map-svg g.pt.sel').length === 0");
    await cdp.clickRect("#map-zoom-in");
    const pos1 = await cdp.eval(MARKER_POS);
    assert.ok(pos1.some((p, i) => Math.abs(p[1] - pos0[i][1]) > 1 || Math.abs(p[2] - pos0[i][2]) > 1), "확대 후 마커 좌표 변화");
    const orderX = (ps) => [...ps].sort((a, b) => a[1] - b[1]).map((p) => p[0]);
    assert.deepEqual(orderX(pos1), orderX(pos0), "확대해도 동서 순서 유지");
    await cdp.clickRect("#map-fit");
    const pos2 = await cdp.eval(MARKER_POS);
    for (let i = 0; i < pos0.length; i++) { assert.ok(Math.abs(pos2[i][1] - pos0[i][1]) < 0.5 && Math.abs(pos2[i][2] - pos0[i][2]) < 0.5, "전체 보기로 복귀"); }
    // 관측 (사진 1장, 태그 1개)
    const photo = join(tmp, "front.png");
    writeFileSync(photo, png1x1([0, 128, 255]));
    await cdp.eval("document.querySelectorAll('#asset-list li button')[0].click(); 'ok'");
    await cdp.waitFor("!document.getElementById('sec-observe').hidden");
    await cdp.eval("document.getElementById('change-status').value = 'change_observed'; document.getElementById('note').value = '1층 임대 광고 (e2e)'; 'ok'");
    await cdp.setFiles("#photos", [photo]);
    await cdp.waitFor("document.querySelectorAll('#photo-list .photo-item').length === 1 && document.querySelector('#photo-list .tags')");
    await cdp.eval("document.querySelector('#photo-list .tags input').click(); 'ok'");
    // 빠른 두 번 탭: 한 건만 저장돼야 한다
    await cdp.eval("const b = document.getElementById('save-observation'); b.click(); b.click(); 'ok'");
    await cdp.waitFor("document.getElementById('observe-note').textContent.startsWith('저장됨')");
    await cdp.waitFor("document.querySelectorAll('#record-list li').length === 1");
    assert.equal(await cdp.eval("document.querySelectorAll('#record-list li').length"), 1, "두 번 탭에 한 건만 저장");
    // 미지원 사진(HEIC 시그니처)은 오류로 표시되고 저장이 막힌다
    const heic = join(tmp, "x.heic");
    writeFileSync(heic, Buffer.concat([Buffer.from([0, 0, 0, 0x18]), Buffer.from("ftypheic"), Buffer.alloc(20)]));
    await cdp.eval("document.querySelectorAll('#asset-list li button')[1].click(); 'ok'");
    await cdp.setFiles("#photos", [heic]);
    await cdp.waitFor("document.querySelector('#photo-list .bad')?.textContent.includes('HEIC')");
    await cdp.eval("document.getElementById('change-status').value = 'no_change'; document.getElementById('save-observation').click(); 'ok'");
    await cdp.waitFor("document.getElementById('observe-note').textContent.includes('오류가 있는 사진')");
    await cdp.eval("document.getElementById('cancel-observation').click(); 'ok'");
    // 시드 교체(기기 파일, 물건 2개): 이전 물건은 목록에서 빠지고 기록은 남는다
    const seedAll = JSON.parse(readFileSync(join(ROOT, "tests/fixtures/assets.seed.synthetic.json"), "utf8"));
    const seed2 = join(tmp, "seed2.json");
    writeFileSync(seed2, JSON.stringify(seedAll.slice(3)));
    await cdp.setFiles("#seed-file", [seed2]);
    await cdp.waitFor("document.querySelectorAll('#asset-list li button').length === 2");
    await cdp.waitFor("document.querySelectorAll('#record-list li').length === 1");
    assert.ok((await cdp.eval("document.getElementById('record-list').textContent")).includes("현재 시드에 없는 물건"));
    assert.equal(await cdp.eval("document.querySelectorAll('#map-svg g.pt').length"), 2, "시드 교체 후 지도도 2개");
    // 주소만 있는 시드: 지도에는 점이 없고 목록으로 선택한다
    const seedAddr = join(tmp, "seed-addr.json");
    writeFileSync(seedAddr, JSON.stringify([seedAll[2]]));
    await cdp.setFiles("#seed-file", [seedAddr]);
    await cdp.waitFor("document.querySelectorAll('#asset-list li button').length === 1");
    assert.equal(await cdp.eval("document.querySelectorAll('#map-svg g.pt').length"), 0);
    assert.match(await cdp.eval("document.getElementById('map-note').textContent"), /위치점 있는 물건이 없다\. 목록에서 선택한다/);
    await cdp.setFiles("#seed-file", [seed2]);
    await cdp.waitFor("document.querySelectorAll('#asset-list li button').length === 2");
    // 잘못된 시드 파일은 거절되고 목록이 바뀌지 않는다
    const seedBad = join(tmp, "seed-bad.json");
    writeFileSync(seedBad, JSON.stringify([{ ...seedAll[0], extra: 1 }]));
    await cdp.setFiles("#seed-file", [seedBad]);
    await cdp.waitFor("document.getElementById('seed-note').textContent.includes('시드 오류')");
    assert.equal(await cdp.eval("document.querySelectorAll('#asset-list li button').length"), 2);
    await cdp.eval("document.getElementById('load-synthetic').click(); 'ok'");
    await cdp.waitFor("document.querySelectorAll('#asset-list li button').length === 5");
    // 재접속: 기록 유지
    await cdp.navigate(`${base}/index.html`);
    await cdp.waitFor("document.querySelectorAll('#record-list li').length === 1");
    assert.ok((await cdp.eval("document.getElementById('status-line').textContent")).includes("관측 1"));
    await cdp.waitFor("document.querySelectorAll('#map-svg g.pt').length === 4");
    // 지도 실패 시 목록: SVG 요소 생성만 막아(저장소에서 createElementNS 는 map.js 만 쓴다) 지도가 못 뜨는 상황을 만든다.
    // 앱은 안내만 남기고 목록·기록은 그대로여야 하며 콘솔 오류를 내지 않아야 한다.
    const { identifier: stub } = await cdp.send("Page.addScriptToEvaluateOnNewDocument", { source: `
      const orig = document.createElementNS.bind(document);
      document.createElementNS = (ns, name) => { if (String(ns).endsWith('/svg')) throw new Error('e2e: SVG 생성 불가'); return orig(ns, name); };` });
    await cdp.navigate(`${base}/index.html`);
    await cdp.waitFor("document.getElementById('map-note').textContent.includes('지도 표시 불가')");
    assert.match(await cdp.eval("document.getElementById('map-note').textContent"), /지도 표시 불가: e2e: SVG 생성 불가\. 목록에서 선택한다/);
    await cdp.waitFor("document.querySelectorAll('#asset-list li button').length === 5");
    assert.equal(await cdp.eval("document.querySelectorAll('#map-svg g.pt').length"), 0);
    assert.equal(await cdp.eval("document.querySelectorAll('#record-list li').length"), 1, "지도 실패해도 기록 목록 유지");
    await cdp.eval("document.querySelectorAll('#asset-list li button')[2].click(); 'ok'");
    await cdp.waitFor("!document.getElementById('sec-observe').hidden");
    assert.equal(await cdp.eval("document.getElementById('target-label').textContent"), "가상 물건 3", "지도 없이 목록으로 선택");
    await cdp.eval("document.getElementById('cancel-observation').click(); 'ok'");
    assert.deepEqual(cdp.errors, [], "지도 실패 경로에서 콘솔 오류 없음");
    await cdp.send("Page.removeScriptToEvaluateOnNewDocument", { identifier: stub });
    await cdp.navigate(`${base}/index.html`);
    await cdp.waitFor("document.querySelectorAll('#map-svg g.pt').length === 4");
    await cdp.waitFor("document.querySelectorAll('#record-list li').length === 1");
    // 내보내기: 묶음 준비 → 파일 저장(다운로드) → j5 inspect ok → 저장 확인 → 내보냄 표시
    const dl = join(tmp, "dl"); mkdirSync(dl);
    await cdp.send("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: dl, eventsEnabled: true });
    await cdp.eval("document.getElementById('export-prepare').click(); 'ok'");
    await cdp.waitFor("!document.getElementById('export-save').disabled");
    assert.match(await cdp.eval("document.getElementById('export-summary').textContent"), /대상 1건 · 사진 1장/);
    await cdp.clickSelector("#export-save");
    const zip1 = await waitForDownloads(dl, 1);
    assert.match(zip1, /^e2e-study-\d{8}-\d{6}-1of1-[0-9a-f]{8}\.j5field\.zip$/);
    const insp = spawnSync("python3", ["-m", "j5", "inspect", join(dl, zip1), "--seed", join(ROOT, "tests/fixtures/assets.seed.synthetic.json"), "--study-id", "e2e-study", "--json"], { cwd: ROOT, encoding: "utf8" });
    if (insp.error?.code !== "ENOENT") {
      assert.equal(insp.status, 0, insp.stdout + insp.stderr);
      const rep = JSON.parse(insp.stdout);
      assert.equal(rep.verdict, "ok", insp.stdout);
      assert.equal(rep.kind, "zip");
      assert.equal(rep.counts.events, 1);
      assert.equal(rep.counts.photos_referenced, 1);
    }
    // 저장 확인 전에는 아직 저장됨
    assert.ok((await cdp.eval("document.getElementById('record-list').textContent")).includes("저장됨"));
    await cdp.waitFor("!document.getElementById('export-confirm').disabled");
    await cdp.eval("document.getElementById('export-confirm').click(); 'ok'");
    await cdp.waitFor("document.getElementById('export-note').textContent.includes('모두 확인됨')");
    assert.ok((await cdp.eval("document.getElementById('record-list').textContent")).includes("내보냄"));
    assert.match(await cdp.eval("document.getElementById('export-history').textContent"), /저장 확인/);
    // 다시 내보내기(내보낸 기록 포함): observations.jsonl 바이트가 같다
    await cdp.eval("document.getElementById('include-exported').click(); document.getElementById('export-prepare').click(); 'ok'");
    await cdp.waitFor("!document.getElementById('export-save').disabled");
    await cdp.clickSelector("#export-save");
    const zip2 = await waitForDownloads(dl, 2);
    assert.notEqual(zip1, zip2);
    const cmp = spawnSync("python3", ["-c", `
import sys, zipfile, hashlib, json
a, b = (zipfile.ZipFile(p) for p in sys.argv[1:3])
h = lambda z: hashlib.sha256(z.read("observations.jsonl")).hexdigest()
ma, mb = (json.loads(z.read("manifest.json")) for z in (a, b))
print(json.dumps({"same_obs": h(a) == h(b), "same_pkg": ma["package_id"] == mb["package_id"], "names": sorted(a.namelist()) == sorted(b.namelist())}))
`, join(dl, zip1), join(dl, zip2)], { encoding: "utf8" });
    if (cmp.error?.code !== "ENOENT") {
      assert.equal(cmp.status, 0, cmp.stderr);
      assert.deepEqual(JSON.parse(cmp.stdout), { same_obs: true, same_pkg: false, names: true });
    }
    // 두 번째 시도는 확인하지 않고 둔다 → 이력에 '저장 미확인' 이 남고 기록 상태는 그대로 내보냄
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
      return { events: events.map(e => ({ line: Array.from(e.line), hash: e.event_hash, status: e.status, study_id: e.study_id, data_mode: e.data_mode, exported_in: e.exported_in })), photos: await Promise.all(photos.map(async p => ({ sha256: p.sha256, ext: p.ext, b64: await b64(p.blob) }))) };
    })()`);
    assert.equal(dump.events.length, 1);
    assert.equal(dump.events[0].status, "exported", "저장 확인 후 내보냄");
    assert.equal(dump.events[0].exported_in.length, 1, "미확인 두 번째 시도는 exported_in 에 들어가지 않음");
    assert.equal(dump.events[0].study_id, "e2e-study", "기록에 study_id 고정");
    assert.equal(dump.events[0].data_mode, "synthetic", "기록에 data_mode 고정");
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
    cdp?.close();
    try { srv.closeAllConnections(); srv.close(); } catch {}
  }
});
