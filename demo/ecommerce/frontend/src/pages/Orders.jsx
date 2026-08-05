import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Package, RefreshCw } from "lucide-react";
import { api } from "../api/client.js";
import { describeApiError } from "../lib/errors.js";
import { dateTime, money } from "../lib/format.js";
import { getProduct } from "../data/catalog.js";
import OrbitTracker from "../components/orbit/OrbitTracker.jsx";
import StatusPill from "../components/ui/StatusPill.jsx";
import Banner from "../components/ui/Banner.jsx";

// The backend has exactly three states and no fulfilment service, so the arc
// has three stops — not placed/packed/shipped/delivered. The third node is
// permanently disabled and says why: making the system boundary visible is
// more honest than quietly omitting it.
const TRACKER_STEPS = ["Placed", "Payment", "Fulfilment"];

function statesFor(status) {
  const s = String(status || "PENDING").toUpperCase();
  return ["done", s === "FAILED" ? "error" : s === "PAID" ? "done" : "active", "disabled"];
}

export default function Orders() {
  const [orders, setOrders] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const navigate = useNavigate();

  const load = useCallback(async () => {
    setBusy(true);
    const r = await api.getOrders();
    setBusy(false);
    if (!r.ok) {
      setError(describeApiError(r, { service: "Order Service" }));
      return;
    }
    setError(null);
    // Tolerate either a bare array or {orders: [...]} — rows already arrive
    // newest-first (ORDER BY id DESC), so do not re-sort.
    setOrders(Array.isArray(r.data) ? r.data : r.data?.orders || []);
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function refreshOne(id) {
    const r = await api.getOrder(id);
    if (!r.ok) return;
    setOrders((prev) => prev.map((o) => ((o.id ?? o.order_id) === id ? { ...o, ...r.data } : o)));
  }

  return (
    <div className="mx-auto max-w-4xl px-6 py-6">
      <div className="mb-5 flex items-center justify-between">
        <div>
          <h1 className="font-display text-2xl font-semibold">Your orders</h1>
          <p className="text-sm text-muted">Read from the Order Service → PostgreSQL.</p>
        </div>
        <button type="button" onClick={load} disabled={busy} className="btn-outline px-4 py-2 text-xs">
          <RefreshCw size={14} className={busy ? "animate-spin" : ""} />
          {busy ? "Loading…" : "Refresh"}
        </button>
      </div>

      {error && <Banner tone="err" error={error} />}

      {/* The !error guard is deliberate — a FAILED request must never render
          as "no orders yet". That would hide an outage behind an empty state. */}
      {!error && orders.length === 0 && !busy && (
        <div className="rounded-2xl border border-dashed border-line py-20 text-center">
          <Package size={36} className="mx-auto mb-3 text-muted" />
          <p className="mb-4 text-muted">No orders yet.</p>
          <button type="button" onClick={() => navigate("/")} className="btn-primary">
            Start shopping
          </button>
        </div>
      )}

      <div className="flex flex-col gap-4">
        {orders.map((o) => {
          const id = o.id ?? o.order_id;
          const status = String(o.status || "PENDING").toUpperCase();
          return (
            <div key={id} className="surface p-5">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <span className="mono text-sm font-semibold">Order #{id}</span>
                  <span className="mono ml-3 text-xs text-muted" title={o.created_at}>
                    {dateTime(o.created_at)}
                  </span>
                </div>
                <div className="flex items-center gap-3">
                  <span className="mono text-base font-semibold">{money(o.amount)}</span>
                  <StatusPill status={status} />
                </div>
              </div>

              {Array.isArray(o.items) && o.items.length > 0 && (
                <ul className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
                  {o.items.map((it, i) => (
                    <li key={i} className="mono">
                      {/* Falls back to the raw sku so orders placed by the old
                          UI (widget / gadget / gizmo) still render. */}
                      {getProduct(it.sku)?.name ?? it.sku} × {it.qty}
                    </li>
                  ))}
                </ul>
              )}

              <div className="mx-auto mt-4 max-w-md">
                <OrbitTracker steps={TRACKER_STEPS} current={1} states={statesFor(status)} />
              </div>

              <div className="mt-2 flex items-center justify-between gap-3">
                <p className="text-[11px] text-muted">
                  Fulfilment is not tracked — this demo has no fulfilment service.
                </p>
                <button
                  type="button"
                  onClick={() => refreshOne(id)}
                  className="btn-outline shrink-0 px-3 py-1 text-[11px]"
                >
                  Refresh status
                </button>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
