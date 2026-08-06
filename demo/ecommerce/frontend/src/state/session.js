// sessionStorage-backed session, with an in-memory mirror.
//
// sessionStorage (not localStorage) is deliberate: it survives a refresh —
// which is what makes the multi-step checkout and the failure scenarios
// usable — but dies when the tab closes, so every demo run starts clean and
// a second tab is a genuinely separate session.
//
// The in-memory mirror means the request hot path in client.js never touches
// storage, and the whole module degrades to volatile behaviour if storage is
// unavailable (private mode, blocked cookies).

const TOKEN_KEY = "ecommerce_jwt";
const CART_KEY = "ecommerce_cart";

let memToken = null;

function safeGet(key) {
  try {
    return window.sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

function safeSet(key, value) {
  try {
    if (value == null) window.sessionStorage.removeItem(key);
    else window.sessionStorage.setItem(key, value);
  } catch {
    /* storage unavailable — in-memory mirror still works for this tab */
  }
}

// ---------------------------------------------------------------- token

export function loadToken() {
  if (memToken == null) memToken = safeGet(TOKEN_KEY);
  return memToken;
}

export function saveToken(token) {
  memToken = token || null;
  safeSet(TOKEN_KEY, memToken);
}

export function dropToken() {
  memToken = null;
  safeSet(TOKEN_KEY, null);
}

// ------------------------------------------------------------------ jwt

/**
 * Decode a JWT payload WITHOUT verifying it.
 *
 * Display purposes only — it lets the authed shell render immediately on a
 * refresh instead of blocking first paint on /profile. The server is still
 * the only thing that decides whether the token is actually valid.
 */
export function decodeJwt(token) {
  if (!token || typeof token !== "string") return null;
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  try {
    const base64 = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    const padded = base64 + "=".repeat((4 - (base64.length % 4)) % 4);
    return JSON.parse(atob(padded));
  } catch {
    return null;
  }
}

/** True when the token's own `exp` claim is already past. Checked locally so
 *  we never fire a request we know will 401 — a self-inflicted 401 pollutes
 *  login_failure_total and the traces being demoed. */
export function isExpired(claims) {
  if (!claims || typeof claims.exp !== "number") return false;
  return claims.exp * 1000 <= Date.now();
}

// ----------------------------------------------------------------- cart

/** Persist only the { id: qty } map. Prices are always re-derived from the
 *  catalog so a stale cart can never diverge from what is on screen. */
export function loadCart() {
  const raw = safeGet(CART_KEY);
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    const out = {};
    for (const [id, qty] of Object.entries(parsed)) {
      const n = Number(qty);
      if (Number.isInteger(n) && n > 0) out[id] = n;
    }
    return out;
  } catch {
    return {};
  }
}

export function saveCart(items) {
  safeSet(CART_KEY, JSON.stringify(items ?? {}));
}

/** Wipe everything this module owns. Address and payment-method data are
 *  never persisted at all, so there is nothing to clear for those. */
export function clearSession() {
  dropToken();
  safeSet(CART_KEY, null);
}
