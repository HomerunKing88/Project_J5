// sha256. 보안 컨텍스트에서는 WebCrypto, 아니면 순수 JS 폴백을 쓴다. 어느 쪽이든 같은 값이다.
import { sha256Pure } from "./sha256.js";

export function subtleAvailable() {
  return typeof crypto !== "undefined" && !!crypto.subtle && typeof crypto.subtle.digest === "function";
}

export async function sha256Hex(buffer) {
  if (subtleAvailable()) {
    const digest = await crypto.subtle.digest("SHA-256", buffer);
    return Array.from(new Uint8Array(digest), (x) => x.toString(16).padStart(2, "0")).join("");
  }
  return sha256Pure(buffer);
}
