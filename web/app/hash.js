// sha256 (WebCrypto). 비보안 컨텍스트(http LAN 등)에서는 crypto.subtle 이 없어 null 을 돌려준다.
export function subtleAvailable() {
  return typeof crypto !== "undefined" && !!crypto.subtle && typeof crypto.subtle.digest === "function";
}

export async function sha256Hex(buffer) {
  if (!subtleAvailable()) return null;
  const digest = await crypto.subtle.digest("SHA-256", buffer);
  return Array.from(new Uint8Array(digest), (x) => x.toString(16).padStart(2, "0")).join("");
}
