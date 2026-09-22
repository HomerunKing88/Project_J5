// IndexedDB 저장소. 스토어: meta(설정), assets(시드 물건), events(관측: 객체 + 고정 바이트), photos(sha256 → Blob).
// 저장 실패(용량 부족 등)는 예외로 올려 화면이 '저장됨'으로 오표시하지 않게 한다. 자동 삭제는 없다.

export const DB_NAME = "j5";
export const DB_VERSION = 1;

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

  async counts() {
    const tx = this.db.transaction(["events", "photos", "assets"]);
    const [events, photos, assets] = await Promise.all([
      req(tx.objectStore("events").count()), req(tx.objectStore("photos").count()), req(tx.objectStore("assets").count()),
    ]);
    return { events, photos, assets };
  }
}
