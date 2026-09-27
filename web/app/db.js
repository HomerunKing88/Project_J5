// IndexedDB 저장소. 스토어: meta(설정), assets(시드 물건), events(관측: 객체 + 고정 바이트), photos(sha256 → Blob),
// parcels(필지 번들 한 벌, v2·J5-013B-1), basemap(배경 도형 번들 한 벌, v3·J5-022), view(PC 조회 파생본의 정본 기록·거래, v4·J5-023),
// device_assets(이 기기가 필지에서 만든 임시 매입 단위, v5·J5-028: 시드 교체에도 남고, 정본이 같은 ID 를 돌려주면(파생본 시드) 기기 사본을 지운다). 저장 실패(용량 부족 등)는 예외로 올려 화면이 '저장됨'으로 오표시하지 않게 한다. 자동 삭제는 없다.

export const DB_NAME = "j5";
export const DB_VERSION = 5; // v3: basemap, v4: view, v5: device_assets 스토어 추가 (기존 스토어·기록은 그대로)
const PARCELS_KEY = "active";
const BASEMAP_KEY = "active";
const VIEW_KEY = "active";

function req(r) {
  return new Promise((resolve, reject) => {
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error);
  });
}

function done(tx) {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error || new Error("transaction aborted"));
  });
}

export async function openDb() {
  const r = indexedDB.open(DB_NAME, DB_VERSION);
  r.onupgradeneeded = () => {
    const db = r.result;
    if (!db.objectStoreNames.contains("meta")) db.createObjectStore("meta");
    if (!db.objectStoreNames.contains("assets")) db.createObjectStore("assets", { keyPath: "asset_id" });
    if (!db.objectStoreNames.contains("events")) {
      const s = db.createObjectStore("events", { keyPath: "event_id" });
      s.createIndex("by_asset", "asset_id");
      s.createIndex("by_saved", "saved_at");
    }
    if (!db.objectStoreNames.contains("photos")) db.createObjectStore("photos", { keyPath: "sha256" });
    if (!db.objectStoreNames.contains("parcels")) db.createObjectStore("parcels");
    if (!db.objectStoreNames.contains("basemap")) db.createObjectStore("basemap");
    if (!db.objectStoreNames.contains("view")) db.createObjectStore("view");
    if (!db.objectStoreNames.contains("device_assets")) db.createObjectStore("device_assets", { keyPath: "asset_id" });
  };
  return req(r);
}

export class Store {
  constructor(db) { this.db = db; }

  static async open() { return new Store(await openDb()); }

  async getMeta(key) {
    return req(this.db.transaction("meta").objectStore("meta").get(key));
  }

  async setMeta(key, value) {
    const tx = this.db.transaction("meta", "readwrite");
    tx.objectStore("meta").put(value, key);
    await done(tx);
  }

  /** 활성 시드를 통째로 교체한다 (한 트랜잭션). events·photos 는 건드리지 않는다. 시드에 든 ID 의 기기 생성 물건은 정본이 넘겨받은 것이라 기기 사본을 지운다. */
  async replaceAssets(assets, source) {
    const tx = this.db.transaction(["assets", "meta", "device_assets"], "readwrite");
    const s = tx.objectStore("assets");
    s.clear();
    for (const a of assets) s.add({ ...a, source });
    for (const a of assets) tx.objectStore("device_assets").delete(a.asset_id);
    tx.objectStore("meta").put(new Date().toISOString(), "seed_loaded_at");
    tx.objectStore("meta").put(source, "seed_source");
    await done(tx);
  }

  /** 시드 물건 + 이 기기가 만든 물건(시드에 없는 것만). 이름순. */
  async listAssets() {
    const tx = this.db.transaction(["assets", "device_assets"]);
    const [seed, device] = await Promise.all([req(tx.objectStore("assets").getAll()), req(tx.objectStore("device_assets").getAll())]);
    const ids = new Set(seed.map((a) => a.asset_id));
    return [...seed, ...device.filter((a) => !ids.has(a.asset_id))].sort((a, b) => a.label.localeCompare(b.label, "ko"));
  }

  async getAsset(id) {
    return (await req(this.db.transaction("assets").objectStore("assets").get(id))) ?? req(this.db.transaction("device_assets").objectStore("device_assets").get(id));
  }

  // ---- 기기가 만든 임시 매입 단위 (J5-028, ADR-21) ----
  async addDeviceAsset(a) {
    const tx = this.db.transaction("device_assets", "readwrite");
    tx.objectStore("device_assets").add(a);
    await done(tx);
  }

  async listDeviceAssets() {
    return req(this.db.transaction("device_assets").objectStore("device_assets").getAll());
  }

  /** 기록이 하나도 없는 기기 생성 물건만 지운다 (기록이 있으면 거절: 기록의 대상이 사라지지 않게). */
  async deleteDeviceAsset(id) {
    const tx = this.db.transaction(["device_assets", "events"], "readwrite");
    const n = await req(tx.objectStore("events").index("by_asset").count(id));
    if (n > 0) { tx.abort(); throw new Error(`기록 ${n}건이 이 물건을 가리킨다. 기록이 있는 물건은 지우지 않는다`); }
    tx.objectStore("device_assets").delete(id);
    await done(tx);
  }

  /** 필지 번들을 통째로 교체한다 (한 벌만 둔다). events·photos·assets 는 건드리지 않는다.
   *  그때의 시드 적재 시각(seed_loaded_at)을 함께 적어, 시드가 바뀌면 번들의 정본 연결(asset_ids)을 더 이상 믿지 않게 한다. */
  async replaceParcels(bundle, source) {
    const tx = this.db.transaction(["parcels", "meta"], "readwrite");
    const seedLoadedAt = (await req(tx.objectStore("meta").get("seed_loaded_at"))) ?? null;
    tx.objectStore("parcels").put({ bundle, source, loaded_at: new Date().toISOString(), seed_loaded_at: seedLoadedAt }, PARCELS_KEY);
    await done(tx);
  }

  async getParcels() {
    return req(this.db.transaction("parcels").objectStore("parcels").get(PARCELS_KEY));
  }

  async clearParcels() {
    const tx = this.db.transaction("parcels", "readwrite");
    tx.objectStore("parcels").delete(PARCELS_KEY);
    await done(tx);
  }

  /** 배경 도형 번들을 통째로 교체한다 (한 벌만 둔다). 다른 스토어는 건드리지 않는다. */
  async replaceBasemap(bundle, source) {
    const tx = this.db.transaction("basemap", "readwrite");
    tx.objectStore("basemap").put({ bundle, source, loaded_at: new Date().toISOString() }, BASEMAP_KEY);
    await done(tx);
  }

  async getBasemap() {
    return req(this.db.transaction("basemap").objectStore("basemap").get(BASEMAP_KEY));
  }

  async clearBasemap() {
    const tx = this.db.transaction("basemap", "readwrite");
    tx.objectStore("basemap").delete(BASEMAP_KEY);
    await done(tx);
  }

  /** PC 조회 파생본(manifest·정본 기록·거래)을 통째로 교체한다 (한 벌만). 이 기기의 관측(events)·사진은 건드리지 않는다. */
  async replaceView(rec) {
    const tx = this.db.transaction("view", "readwrite");
    tx.objectStore("view").put({ ...rec, loaded_at: new Date().toISOString() }, VIEW_KEY);
    await done(tx);
  }

  /**
   * 파생본 한 벌을 한 트랜잭션으로 넣는다 (리뷰 반영): 설정(비어 있을 때만)·물건 목록·필지·정본 기록·거래.
   * 중간에 실패(용량 부족 등)하면 아무것도 바뀌지 않는다. 파생본에 필지가 없으면 기존 필지를 지운다(같은 정본 버전의 자료만 남게).
   * events·photos 는 건드리지 않는다.
   */
  async replaceProjection({ settings = null, assets, parcels = null, view, source }) {
    const tx = this.db.transaction(["meta", "assets", "parcels", "view", "device_assets"], "readwrite");
    const now = new Date().toISOString();
    const meta = tx.objectStore("meta");
    if (settings) for (const [k, v] of Object.entries(settings)) meta.put(v, k);
    const as = tx.objectStore("assets");
    as.clear();
    for (const a of assets) as.add({ ...a, source });
    for (const a of assets) tx.objectStore("device_assets").delete(a.asset_id);   // 정본이 넘겨받은 기기 생성 물건은 사본을 지운다
    meta.put(now, "seed_loaded_at");
    meta.put(source, "seed_source");
    const ps = tx.objectStore("parcels");
    if (parcels) ps.put({ bundle: parcels, source, loaded_at: now, seed_loaded_at: now }, PARCELS_KEY);
    else ps.delete(PARCELS_KEY);
    tx.objectStore("view").put({ ...view, source, loaded_at: now }, VIEW_KEY);
    await done(tx);
  }

  async getView() {
    return req(this.db.transaction("view").objectStore("view").get(VIEW_KEY));
  }

  async clearView() {
    const tx = this.db.transaction("view", "readwrite");
    tx.objectStore("view").delete(VIEW_KEY);
    await done(tx);
  }

  /** 관측과 사진을 한 트랜잭션으로 저장. 실패하면 아무것도 남지 않는다. */
  async saveObservation(record, photos) {
    const tx = this.db.transaction(["events", "photos"], "readwrite");
    const ev = tx.objectStore("events");
    const ph = tx.objectStore("photos");
    const existing = await req(ev.get(record.event_id));
    if (existing) throw new Error("같은 event_id 가 이미 있음");
    ev.add(record);
    for (const p of photos) ph.put(p);
    await done(tx);
  }

  async listEvents() {
    const all = await req(this.db.transaction("events").objectStore("events").getAll());
    return all.sort((a, b) => (a.saved_at < b.saved_at ? 1 : -1));
  }

  async getEvent(id) {
    return req(this.db.transaction("events").objectStore("events").get(id));
  }

  async getPhoto(sha256) {
    return req(this.db.transaction("photos").objectStore("photos").get(sha256));
  }

  /** 같은 study_id·data_mode 의 기록을 saved_at 오름차순으로. */
  async listEventsFor(studyId, dataMode) {
    const all = await req(this.db.transaction("events").objectStore("events").index("by_saved").getAll());
    return all.filter((r) => r.study_id === studyId && r.data_mode === dataMode);
  }

  async getPhotos(shas) {
    const st = this.db.transaction("photos").objectStore("photos");
    const out = new Map();
    for (const sha of shas) {
      const p = await req(st.get(sha));
      if (p) out.set(sha, p);
    }
    return out;
  }

  /** 사진 메타(크기·확장자)만. 내보내기 계획에 쓴다. */
  async photoMeta() {
    const all = await req(this.db.transaction("photos").objectStore("photos").getAll());
    return new Map(all.map((p) => [p.sha256, { bytes: p.bytes, ext: p.ext }]));
  }

  async listExports() {
    return (await this.getMeta("exports")) ?? [];
  }

  /** 내보내기 시도를 기록한다 (파일 저장 버튼을 누른 시점). 확인 전이므로 confirmed_at 은 null. */
  async appendExport(entry) {
    const tx = this.db.transaction("meta", "readwrite");
    const st = tx.objectStore("meta");
    const list = (await req(st.get("exports"))) ?? [];
    list.push({ ...entry, confirmed_at: null });
    st.put(list, "exports");
    await done(tx);
  }

  /** 사용자가 파일 저장을 확인한 뒤: 기록 상태 exported, exported_in 에 package_id, 시도에 confirmed_at. 한 트랜잭션. */
  async markExported(eventIds, packageId, at) {
    const tx = this.db.transaction(["events", "meta"], "readwrite");
    const ev = tx.objectStore("events");
    for (const id of eventIds) {
      const r = await req(ev.get(id));
      if (!r) continue;
      r.status = "exported";
      r.exported_in = Array.from(new Set([...(r.exported_in ?? []), packageId]));
      r.exported_at = r.exported_at ?? at;
      ev.put(r);
    }
    const meta = tx.objectStore("meta");
    const list = (await req(meta.get("exports"))) ?? [];
    for (const e of list) if (e.package_id === packageId && !e.confirmed_at) e.confirmed_at = at;
    meta.put(list, "exports");
    await done(tx);
  }

  async counts() {
    const tx = this.db.transaction(["events", "photos", "assets"]);
    const [events, photos, assets] = await Promise.all([
      req(tx.objectStore("events").count()), req(tx.objectStore("photos").count()), req(tx.objectStore("assets").count()),
    ]);
    return { events, photos, assets };
  }
}
