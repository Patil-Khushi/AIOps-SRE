// All cached values live under this localStorage prefix.
const PREFIX = 'aiops:cache:';

// Every makeCache instance registers here so clearAllCaches() can
// reach both the in-memory and the localStorage layer in one call.
const registry: Array<{ clear(): void }> = [];

/**
 * Creates a typed cache backed by both an in-memory Map (fast reads) and
 * localStorage (survives page reloads and new tabs). The two layers stay
 * in sync: writes go to both, reads warm the memory layer from storage on
 * the first miss so subsequent reads never touch the disk again.
 */
export function makeCache<V>(namespace: string) {
  const mem = new Map<string, V>();
  const ns = `${PREFIX}${namespace}:`;

  function get(key: string): V | undefined {
    if (mem.has(key)) return mem.get(key);
    try {
      const raw = localStorage.getItem(ns + key);
      if (raw !== null) {
        const val = JSON.parse(raw) as V;
        mem.set(key, val);
        return val;
      }
    } catch {}
    return undefined;
  }

  function set(key: string, val: V): void {
    mem.set(key, val);
    try { localStorage.setItem(ns + key, JSON.stringify(val)); } catch {}
  }

  function has(key: string): boolean {
    return get(key) !== undefined;
  }

  // Exposed as 'delete' so callers have a Map-compatible interface.
  function remove(key: string): void {
    mem.delete(key);
    try { localStorage.removeItem(ns + key); } catch {}
  }

  function clear(): void {
    mem.clear();
    try {
      Object.keys(localStorage)
        .filter((k) => k.startsWith(ns))
        .forEach((k) => localStorage.removeItem(k));
    } catch {}
  }

  const instance = { get, set, has, delete: remove, clear };
  registry.push(instance);
  return instance;
}

/** Wipes every cache created by makeCache — both memory and localStorage. */
export function clearAllCaches(): void {
  registry.forEach((c) => c.clear());
}
