// IndexedDB 저장소. 스토어: meta(설정), assets(시드 물건), events(관측: 객체 + 고정 바이트), photos(sha256 → Blob),
// parcels(필지 번들 한 벌, v2·J5-013B-1). 저장 실패(용량 부족 등)는 예외로 올려 화면이 '저장됨'으로 오표시하지 않게 한다. 자동 삭제는 없다.

export const DB_NAME = "j5";
export const DB_VERSION = 2;
const PARCELS_KEY = "active";

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

  /** 활성 시드를 통째로 교체한다 (한 트랜잭션). events·photos 는 건드리지 않는다. */
  async replaceAssets(assets, source) {
    const tx = this.db.transaction(["assets", "meta"], "readwrite");
    const s = tx.objectStore("assets");
    s.clear();
    for (const a of assets) s.add({ ...a, source });
    tx.objectStore("meta").put(new Date().toISOString(), "seed_loaded_at");
    tx.objectStore("meta").put(source, "seed_source");
    await done(tx);
  }

  async listAssets() {
    const all = await req(this.db.transaction("assets").objectStore("assets").getAll());
    return all.sort((a, b) => a.label.localeCompare(b.label, "ko"));
  }

  async getAsset(id) {
    return req(this.db.transaction("assets").objectStore("assets").get(id));
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
