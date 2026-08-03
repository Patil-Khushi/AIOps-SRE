// Thin fetch wrapper. Because this app exists to observe failures, every call
// returns a normalized result that records status, latency, and error text so
// the UI can display exactly what happened (and which service answered).

const cfg = window.__APP_CONFIG__ || {};
export const USER_SERVICE_URL = cfg.USER_SERVICE_URL || "http://localhost:8001";
export const ORDER_SERVICE_URL = cfg.ORDER_SERVICE_URL || "http://localhost:8002";

const TOKEN_KEY = "ecommerce_jwt";

export function getToken() {
  return window.__authToken || null;
}
export function setToken(t) {
  window.__authToken = t;
}
export function clearToken() {
  window.__authToken = null;
}

// Subscribers (the request-log panel listens here).
const listeners = new Set();
export function onRequest(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}
function emit(entry) {
  listeners.forEach((fn) => fn(entry));
}

async function request(base, path, { method = "GET", body, auth = false } = {}) {
  const url = `${base}${path}`;
  const headers = { "Content-Type": "application/json" };
  if (auth && getToken()) headers["Authorization"] = `Bearer ${getToken()}`;

  const started = performance.now();
  const entry = {
    id: crypto.randomUUID(),
    method,
    url,
    at: new Date().toISOString(),
    status: null,
    ms: null,
    ok: false,
    error: null,
  };

  try {
    const res = await fetch(url, {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
    });
    entry.ms = Math.round(performance.now() - started);
    entry.status = res.status;
    entry.ok = res.ok;

    let data = null;
    const text = await res.text();
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = text;
    }

    if (!res.ok) {
      entry.error =
        (data && (data.detail || data.error || data.message)) ||
        `HTTP ${res.status}`;
    }
    emit(entry);
    return { ok: res.ok, status: res.status, data, ms: entry.ms };
  } catch (err) {
    // Network-level failure (service down, connection refused, timeout).
    entry.ms = Math.round(performance.now() - started);
    entry.error = err.message || "Network error";
    emit(entry);
    return { ok: false, status: 0, data: null, ms: entry.ms, error: entry.error };
  }
}

// --- User service ----------------------------------------------------------
export const api = {
  register: (name, email, password) =>
    request(USER_SERVICE_URL, "/register", {
      method: "POST",
      body: { name, email, password },
    }),

  login: (email, password) =>
    request(USER_SERVICE_URL, "/login", {
      method: "POST",
      body: { email, password },
    }),

  profile: () => request(USER_SERVICE_URL, "/profile", { auth: true }),

  // --- Order service -------------------------------------------------------
  createOrder: (items, amount) =>
    request(ORDER_SERVICE_URL, "/orders", {
      method: "POST",
      auth: true,
      body: { items, amount },
    }),

  getOrders: () => request(ORDER_SERVICE_URL, "/orders", { auth: true }),

  getOrder: (id) =>
    request(ORDER_SERVICE_URL, `/orders/${id}`, { auth: true }),
};