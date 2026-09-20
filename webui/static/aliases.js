/** Presentation-only worker display names. Never authority, evidence, or configuration.
 *  In-memory only: aliases last for this page lifetime and are never persisted.
 */

export const MAX_ALIAS_LENGTH = 80;

const aliases = new Map();

const FORBIDDEN = /digest|fingerprint|sha256|evidence|sqlite|\.db\b|token|secret/i;

function displayNameAllowed(name) {
  if (typeof name !== "string") return false;
  const text = name.trim();
  if (!text || text.length > MAX_ALIAS_LENGTH) return false;
  if (text.startsWith("/") || text.includes("\\") || text.includes("://")) return false;
  if (FORBIDDEN.test(text)) return false;
  return true;
}

export function workerAlias(workerId) {
  if (typeof workerId !== "string" || !workerId) return workerId;
  const value = aliases.get(workerId);
  if (!displayNameAllowed(value)) return workerId;
  return value.trim();
}

export function saveWorkerAlias(workerId, name) {
  if (typeof workerId !== "string" || !workerId) return;
  const cleanName = typeof name === "string" ? name.trim() : "";
  if (!cleanName) {
    aliases.delete(workerId);
    return;
  }
  if (!displayNameAllowed(cleanName)) return;
  aliases.set(workerId, cleanName);
}
