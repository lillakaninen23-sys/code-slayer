/** Presentation-only worker display names. Never authority or configuration. */

export const ALIAS_PREFIX = "worker-alias:";
export const MAX_ALIAS_LENGTH = 80;

const FORBIDDEN = /digest|fingerprint|sha256|evidence|sqlite|\.db\b|token|secret/i;

function storage() {
  try {
    const store = globalThis.localStorage;
    if (!store || typeof store.getItem !== "function") return null;
    return store;
  } catch {
    return null;
  }
}

export function aliasStorageKey(workerId) {
  if (typeof workerId !== "string" || !workerId) return null;
  return `${ALIAS_PREFIX}${workerId}`;
}

function displayNameAllowed(name) {
  if (typeof name !== "string") return false;
  const text = name.trim();
  if (!text || text.length > MAX_ALIAS_LENGTH) return false;
  if (text.startsWith("/") || text.includes("\\") || text.includes("://")) return false;
  if (FORBIDDEN.test(text)) return false;
  return true;
}

export function workerAlias(workerId) {
  const key = aliasStorageKey(workerId);
  const store = storage();
  if (!key || !store) return workerId;
  const value = store.getItem(key);
  if (!displayNameAllowed(value)) return workerId;
  return value.trim();
}

export function saveWorkerAlias(workerId, name) {
  const key = aliasStorageKey(workerId);
  const store = storage();
  if (!key || !store) return;
  const cleanName = typeof name === "string" ? name.trim() : "";
  if (!cleanName) {
    store.removeItem(key);
    return;
  }
  if (!displayNameAllowed(cleanName)) return;
  store.setItem(key, cleanName);
}
