// 배포 주소 흉내 e2e (J5-059, ADR-24). Cloudflare Pages 처럼 ./index.html 을 ./ 로, devtest.html 을 devtest 로 넘기는(308) 서버에서
// 앱을 연다. 서비스 워커가 설치 때 받은 넘겨받은 응답을 탐색에 그대로 돌려주면 브라우저가 페이지를 열지 않는다. 브라우저가 없으면 건너뛴다.
// 가상 서버 시험이다. 실제 Cloudflare Pages·iPhone Safari 에서의 확인은 아니다.
import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { existsSync, mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, extname } from "node:path";
import { Cdp, MIME, WEB, findChrome } from "./cdp.mjs";

const chrome = findChrome();
const REDIRECTS = { "/index.html": "/", "/devtest.html": "/devtest" };
const ALIASES = { "/": "index.html", "/devtest": "devtest.html" };

function servePages(dir) {
  return new Promise((res) => {
    const srv = createServer((req, resp) => {
      const path = decodeURIComponent(new URL(req.url, "http://x").pathname);
      if (REDIRECTS[path]) { resp.writeHead(308, { location: REDIRECTS[path], "cache-control": "no-store" }); return resp.end(); }
      const file = join(dir, ALIASES[path] ?? path);
      if (!file.startsWith(dir) || !existsSync(file) || path.startsWith("/_")) { resp.writeHead(404); return resp.end(); }
      resp.writeHead(200, { "content-type": MIME[extname(file)] || "application/octet-stream", "cache-control": "no-store" });
      resp.end(readFileSync(file));
    });
    srv.listen(0, "127.0.0.1", () => res(srv));
  });
}

const appUp = "document.getElementById('status-line')?.textContent.includes('앱 0.2.31')";

test("배포 주소: index.html 을 넘기는 서버에서도 서비스 워커가 앱을 연다", { skip: chrome ? false : "Chromium 없음" }, async () => {
  const tmp = mkdtempSync(join(tmpdir(), "j5-deploy-"));
  const srv = await servePages(WEB);
  const base = `http://127.0.0.1:${srv.address().port}`;
  let cdp;
  try {
    cdp = await Cdp.launch(chrome, tmp);
    await cdp.navigate(`${base}/`);
    await cdp.waitFor(appUp);
    await cdp.waitFor("navigator.serviceWorker.ready.then(() => !!navigator.serviceWorker.controller)", 15000);
    // 설치 때 ./index.html 을 받으며 넘겨받은 응답이 캐시에 들어갔다(서버 흉내가 맞는지 확인).
    assert.equal(await cdp.eval("caches.open('j5-app-v0.2.31').then((c) => c.match('./index.html')).then((r) => !!r && r.redirected)"), true);

    // 1. 서비스 워커가 맡은 뒤 index.html 로 직접 열기 (홈 화면 북마크·예전 주소)
    await cdp.navigate(`${base}/index.html`);
    await cdp.waitFor(appUp);
    assert.equal(await cdp.eval("!!navigator.serviceWorker.controller"), true, "서비스 워커가 응답했다");

    // 2. 기기 시험 페이지의 "처음으로" 는 넘김 없이 앱으로
    await cdp.navigate(`${base}/devtest.html`);
    await cdp.waitFor("location.pathname === '/devtest'");
    assert.equal(await cdp.eval("new URL(document.querySelector('a[href]').getAttribute('href'), location.href).pathname"), "/");

    // 3. 오프라인: 캐시만으로 ./ 와 ./index.html 이 열린다
    await cdp.send("Network.emulateNetworkConditions", { offline: true, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
    for (const p of ["/", "/index.html"]) {
      await cdp.navigate(`${base}${p}`);
      await cdp.waitFor(appUp);
    }
    await cdp.send("Network.emulateNetworkConditions", { offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
    assert.deepEqual(cdp.errors, []);
  } finally {
    cdp?.close();
    srv.close();
  }
});
