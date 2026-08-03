import { useEffect, useState } from "react";
import { api } from "../api/client.js";

export default function Orders() {
  const [orders, setOrders] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  async function load() {
    setBusy(true);
    setError(null);
    const r = await api.getOrders();
    setBusy(false);
    if (r.ok) {
      const list = Array.isArray(r.data) ? r.data : r.data?.orders || [];
      setOrders(list);
    } else {
      setError(r.error || r.data?.detail || `Could not load orders (HTTP ${r.status || "network"})`);
    }
  }

  useEffect(() => {
    load();
  }, []);

  async function refreshOne(id) {
    const r = await api.getOrder(id);
    if (r.ok) {
      setOrders((prev) => prev.map((o) => ((o.id ?? o.order_id) === id ? { ...o, ...r.data } : o)));
    }
  }

  return (
    <>
      <h1>Orders</h1>
      <p className="sub">Reads from the Order Service → PostgreSQL.</p>

      <div className="row" style={{ marginBottom: 8 }}>
        <button onClick={load} disabled={busy}>{busy ? "Loading…" : "Refresh"}</button>
      </div>

      {error && <div className="banner err">{error}</div>}

      {!error && orders.length === 0 && !busy && (
        <div className="card"><p className="sub" style={{ margin: 0 }}>No orders yet. Place one from Checkout.</p></div>
      )}

      {orders.map((o) => {
        const id = o.id ?? o.order_id;
        const status = (o.status || "PENDING").toUpperCase();
        return (
          <div className="order" key={id}>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <div>
                <strong>Order {id}</strong>{" "}
                <span className={`pill ${status}`}>{status}</span>
              </div>
              <button className="ghost" style={{ marginTop: 0, padding: "4px 10px" }} onClick={() => refreshOne(id)}>
                Refresh status
              </button>
            </div>
            <div className="meta" style={{ marginTop: 6 }}>
              {o.amount != null ? `$${Number(o.amount).toFixed(2)}` : "—"}
              {o.created_at ? ` · ${o.created_at}` : ""}
            </div>
          </div>
        );
      })}
    </>
  );
}