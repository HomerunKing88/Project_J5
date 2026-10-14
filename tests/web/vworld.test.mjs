// 온라인 필지 조회 응답 해석 (J5-080). 응답 모양은 폰 실측(2026-10-14, 기기 시험 페이지 격리 프레임)에서 확인한 VWorld 연속지적 응답과 같은 구조이고 값은 가상이다.
import test from "node:test";
import assert from "node:assert/strict";
import { parseParcelResponse, parseZoningResponse, splitJibun, polygonAreaM2, polygonsOfGeometry, canLookup, fmtJiga, fmtAreaPyeong, createVworldBridge, MAX_VERTICES } from "../../web/app/vworld.js";

// 경도 0.0001° × 위도 0.0001° 사각형 (위도 37.57 에서 약 8.8m × 11.1m)
const square = [[[127.0019, 37.5709], [127.0020, 37.5709], [127.0020, 37.5710], [127.0019, 37.5710], [127.0019, 37.5709]]];
const doc = (props, geometry = { type: "MultiPolygon", coordinates: [square] }) => ({ response: { service: { name: "data", version: "2.0", operation: "GetFeature", time: "23(ms)" }, status: "OK",
  record: { total: "1", current: "1" }, page: { total: "1", current: "1", size: "10" },
  result: { featureCollection: { type: "FeatureCollection", bbox: [127.0019, 37.5709, 127.002, 37.571], features: [{ type: "Feature", geometry, properties: props, id: "LP_PA_CBND_BUBUN.1" }] } } } });
const PROPS = { gosi_year: "2025", pnu: "1111013800101820013", jibun: "182-13 대", bonbun: "0182", bubun: "0013", addr: "서울특별시 종로구 종로5가 182-13", gosi_month: "01", jiga: "12345000" };

test("parseParcelResponse: 실측 필드(pnu·addr·jibun·jiga·gosi_year·gosi_month)와 경계", () => {
  const r = parseParcelResponse(doc(PROPS));
  assert.equal(r.status, "OK");
  const p = r.parcel;
  assert.equal(p.pnu, "1111013800101820013");
  assert.equal(p.addr, "서울특별시 종로구 종로5가 182-13");
  assert.equal(p.lot, "182-13");
  assert.equal(p.jimok, "대");
  assert.equal(p.jiga, 12345000);
  assert.equal(p.gosi, "2025-01");
  assert.ok(p.areaM2 > 90 && p.areaM2 < 105, `도형면적 약 98㎡: ${p.areaM2}`);
  assert.deepEqual(p.bbox, [127.0019, 37.5709, 127.002, 37.571]);
  assert.equal(p.feature.id, p.pnu);
  assert.equal(p.feature.properties.label, "182-13");
  assert.equal(p.feature.properties.emd_name, "종로5가");
});

test("parseParcelResponse: 없음·오류·형식 이상은 사유로", () => {
  assert.equal(parseParcelResponse({ response: { status: "NOT_FOUND" } }).status, "NOT_FOUND");
  const err = parseParcelResponse({ response: { status: "ERROR", error: { level: "1", code: "INCORRECT_KEY", text: "인증키 정보가 올바르지 않습니다." } } });
  assert.equal(err.status, "ERROR");
  assert.equal(err.error, "인증키 정보가 올바르지 않습니다.");
  assert.equal(parseParcelResponse(null).status, "bad_response");
  assert.equal(parseParcelResponse(doc({ ...PROPS, pnu: "123" })).status, "bad_response", "PNU 19자리가 아니면 거절");
  assert.equal(parseParcelResponse(doc(PROPS, { type: "Point", coordinates: [127, 37.5] })).status, "bad_response", "다각형이 아니면 거절");
  assert.equal(parseParcelResponse(doc(PROPS, { type: "Polygon", coordinates: [[[0, 0], [1, 0], [1, 1], [0, 0]]] })).status, "bad_response", "한국 범위 밖 좌표 거절");
});

test("parseParcelResponse: 값이 없거나 숫자가 아니면 null (0 으로 채우지 않는다)", () => {
  const p = parseParcelResponse(doc({ ...PROPS, jiga: "", gosi_year: "", gosi_month: "", jibun: "182-13", addr: "" })).parcel;
  assert.equal(p.jiga, null);
  assert.equal(p.gosi, null);
  assert.equal(p.jimok, null);
  assert.equal(p.addr, null);
});

test("splitJibun: 지번과 뒤에 붙은 지목", () => {
  assert.deepEqual(splitJibun("182-13대"), { lot: "182-13", jimok: "대" });
  assert.deepEqual(splitJibun("182-13 대"), { lot: "182-13", jimok: "대" });
  assert.deepEqual(splitJibun("산1-2 임"), { lot: "산 1-2", jimok: "임" });
  assert.deepEqual(splitJibun("7"), { lot: "7", jimok: null });
  assert.deepEqual(splitJibun(""), { lot: null, jimok: null });
});

test("polygonAreaM2: 구멍은 빼고, polygonsOfGeometry: 꼭짓점 상한", () => {
  const outer = [[127.0, 37.5], [127.001, 37.5], [127.001, 37.501], [127.0, 37.501], [127.0, 37.5]];
  const hole = [[127.0004, 37.5004], [127.0006, 37.5004], [127.0006, 37.5006], [127.0004, 37.5006], [127.0004, 37.5004]];
  const full = polygonAreaM2([[outer]]), withHole = polygonAreaM2([[outer, hole]]);
  assert.ok(Math.abs(full - 88.4 * 111.2) / full < 0.02, `약 9,830㎡: ${full}`);
  assert.ok(withHole < full && Math.abs(full - withHole - full * 0.04) / full < 0.01);
  const big = Array.from({ length: MAX_VERTICES + 1 }, (_, i) => [127 + i * 1e-7, 37.5 + (i % 2) * 1e-7]);
  assert.equal(polygonsOfGeometry({ type: "Polygon", coordinates: [big] }), null);
});

test("parseZoningResponse: 속성은 이름 그대로, 글·숫자만", () => {
  const z = parseZoningResponse(doc({ uname: "일반상업지역", dyear: "2020", obj: { a: 1 }, "bad key": "x", empty: "" }));
  assert.equal(z.status, "OK");
  assert.deepEqual(z.items, [[["uname", "일반상업지역"], ["dyear", "2020"]]]);
  assert.equal(parseZoningResponse({ response: { status: "NOT_FOUND" } }).status, "NOT_FOUND");
});

test("canLookup·표기", () => {
  assert.equal(canLookup([127.0019, 37.5709]), true);
  assert.equal(canLookup([0, 0]), false);
  assert.equal(fmtJiga(12345000), "1,235만 원/㎡ · 4,081만 원/평");
  assert.equal(fmtJiga(null), null);
  assert.equal(fmtAreaPyeong(330.5785), "330.6㎡ (100평)");
});

test("createVworldBridge: 격리 프레임을 sandbox=allow-scripts 로 만들고, 그 프레임에서 온 응답만 받는다", async () => {
  const listeners = [];
  const posted = [];
  const frameWin = { postMessage: (m) => posted.push(m) };
  const frame = { setAttribute(k, v) { this[k] = v; }, remove() { this.removed = true; } };
  const fakeDoc = { createElement: () => frame, body: { append: () => { Object.defineProperty(frame, "contentWindow", { value: frameWin }); } } };
  const win = { addEventListener: (t, fn) => listeners.push(fn), removeEventListener: () => {} };
  const bridge = createVworldBridge(fakeDoc, { win, timeoutMs: 1000 });
  const p = bridge.request("LP_PA_CBND_BUBUN", [127.0019, 37.5709], "ABCDEFGH-1234", "https://h.example");
  assert.equal(frame.sandbox, "allow-scripts");
  const send = (source, data) => listeners.forEach((fn) => fn({ source, data }));
  send({}, { type: "j5-vworld-ready" });                 // 다른 창에서 온 신호는 무시
  send(frameWin, { type: "j5-vworld-ready" });
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(posted.length, 1);
  assert.deepEqual({ ...posted[0], id: 1 }, { type: "j5-vworld-req", id: 1, key: "ABCDEFGH-1234", domain: "https://h.example", data: "LP_PA_CBND_BUBUN", lon: 127.0019, lat: 37.5709 });
  send({}, { type: "j5-vworld-res", id: posted[0].id, ok: true, data: { fake: true } });   // 다른 창의 응답 무시
  send(frameWin, { type: "j5-vworld-res", id: posted[0].id, ok: true, data: { response: { status: "OK" } } });
  assert.deepEqual(await p, { response: { status: "OK" } });
  const q = bridge.request("LT_C_UQ111", [127, 37.5], "ABCDEFGH-1234", "https://h.example");
  await new Promise((r) => setTimeout(r, 0));
  send(frameWin, { type: "j5-vworld-res", id: posted[1].id, ok: false, error: "script_error" });
  await assert.rejects(q, /script_error/);
  bridge.destroy();
  assert.equal(frame.removed, true);
});

test("parseParcelsInBox: 범위 응답의 필지 목록, 형식 이상은 건너뜀, 일부만 받았는지 (J5-081)", async () => {
  const { parseParcelsInBox } = await import("../../web/app/vworld.js");
  const feat = (pnu, jibun, geometry = { type: "MultiPolygon", coordinates: [square] }) => ({ type: "Feature", geometry, properties: { pnu, jibun } });
  const d = { response: { status: "OK", record: { total: "3", current: "3" }, result: { featureCollection: { features: [
    feat("1111016300101930017", "193-17대"), feat("123", "1대"), feat("1111016300101930018", "193-18 대", { type: "Point", coordinates: [127, 37.5] })] } } } };
  const r = parseParcelsInBox(d);
  assert.equal(r.status, "OK");
  assert.deepEqual(r.parcels.map((p) => [p.pnu, p.lot]), [["1111016300101930017", "193-17"]]);
  assert.equal(r.truncated, false);
  d.response.record.total = "1500";
  assert.equal(parseParcelsInBox(d).truncated, true, "전체 수가 받은 수보다 많으면 일부만");
  assert.deepEqual(parseParcelsInBox({ response: { status: "NOT_FOUND" } }), { status: "OK", parcels: [], total: 0, truncated: false });
  assert.equal(parseParcelsInBox({ response: { status: "ERROR", error: { text: "x" } } }).status, "ERROR");
});

test("zoningSummary: 실측 필드 uname·dyear 로 한 줄, 같은 이름은 한 번, 이름 없는 결과는 뺌 (J5-081)", async () => {
  const { zoningSummary } = await import("../../web/app/vworld.js");
  assert.equal(zoningSummary([[["uname", "일반상업지역"], ["dyear", "2023"], ["dnum", "0243"]], [["dyear", "2000"], ["dnum", "0000"]]]), "일반상업지역 (2023 고시)");
  assert.equal(zoningSummary([[["uname", "일반상업지역"]], [["uname", "일반상업지역"], ["dyear", "2020"]], [["uname", "제3종일반주거지역"], ["dyear", "x"]]]), "일반상업지역, 제3종일반주거지역");
  assert.equal(zoningSummary([[["dyear", "2000"]]]), null);
});

test("aroundQueryBox: 사방 25% 여유, 한 변 0.02° 상한은 가운데 기준 (J5-081)", async () => {
  const { aroundQueryBox, MAX_BOX_DEG } = await import("../../web/app/vworld.js");
  assert.deepEqual(aroundQueryBox([127, 37.5, 127.004, 37.503]), [126.999, 37.49925, 127.005, 37.50375]);
  const big = aroundQueryBox([126.9, 37.4, 127.1, 37.6]);
  assert.ok(big[2] - big[0] <= MAX_BOX_DEG && big[3] - big[1] <= MAX_BOX_DEG);
  assert.ok(Math.abs((big[0] + big[2]) / 2 - 127) < 1e-6 && Math.abs((big[1] + big[3]) / 2 - 37.5) < 1e-6, "가운데 유지");
});

test("searchQuery·parseSearchResponse: 주소 찾기 (J5-082, 응답 필드는 실측 전)", async () => {
  const { searchQuery, parseSearchResponse } = await import("../../web/app/vworld.js");
  assert.equal(searchQuery("  종로5가   193-17 "), "종로5가 193-17");
  assert.equal(searchQuery('<b>종로"5가</b>'), "b 종로 5가 /b");
  assert.equal(searchQuery("   "), null);
  assert.equal(searchQuery("가".repeat(100)).length, 80);
  const ok = parseSearchResponse({ response: { status: "OK", result: { items: [
    { id: "1111016300101930017", address: { parcel: "서울특별시 종로구 종로5가 193-17", road: "" }, point: { x: "127.0021", y: "37.5705" } },
    { address: { road: "서울특별시 종로구 종로 200" }, point: { x: 127.003, y: 37.571 } },
    { address: { parcel: "범위 밖" }, point: { x: "0", y: "0" } },
    { address: { parcel: "자리 없음" } }] } } });
  assert.equal(ok.status, "OK");
  assert.deepEqual(ok.items, [{ title: "서울특별시 종로구 종로5가 193-17", lonlat: [127.0021, 37.5705] }, { title: "서울특별시 종로구 종로 200", lonlat: [127.003, 37.571] }]);
  assert.deepEqual(parseSearchResponse({ response: { status: "NOT_FOUND" } }), { status: "OK", items: [] });
  assert.equal(parseSearchResponse({ response: { status: "ERROR", error: { text: "키 오류" } } }).error, "키 오류");
  assert.equal(parseSearchResponse({}).status, "bad_response");
});
