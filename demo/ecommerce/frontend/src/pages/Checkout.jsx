import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client.js";

const CATALOG = [
  { sku: "widget", label: "Widget", price: 12.0 },
  { sku: "gadget", label: "Gadget", price: 29.5 },
  { sku: "gizmo", label: "Gizmo", price: 7.25 },
];

export default function Checkout() {
  const [qty, setQty] = useState(() => ({ widget: 1, gadget: 0, gizmo: 0 }));
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const navigate = useNavigate();

  const items = CATALOG.filter((c) => qty[c.sku] > 0).map((c) => ({
    sku: c.sku,
    qty: qty[c.sku],
    price: c.price,
  }));
  const amount = items.reduce((sum, i) => sum + i.qty * i.price, 0);

  async function placeOrder() {
    setBusy(true);
    setResult(null);
    const r = await api.createOrder(items, Number(amount.toFixed(2)));
    setBusy(false);
    if (r.ok) {
      const id = r.data?.id ?? r.data?.order_id;
      const status = r.data?.status || "PENDING";
      setResult({ ok: true, msg: `Order ${id} created — status ${status}.` });
    } else {
      setResult({
        ok: false,
        msg: r.error || r.data?.detail || `Order failed (HTTP ${r.status || "network"})`,
      });
    }
  }

  return (
    <>
      <h1>Checkout</h1>
      <p className="sub">
        Creates an order in the Order Service, which validates the user, then calls the
        Payment Service → mock gateway.
      </p>

      <div className="card">
        {CATALOG.map((c) => (
          <div className="row" key={c.sku} style={{ justifyContent: "space-between", marginBottom: 10 }}>
            <div>
              <strong>{c.label}</strong>{" "}
              <span className="meta" style={{ color: "var(--muted)", fontFamily: "var(--mono)", fontSize: 12 }}>
                ${c.price.toFixed(2)}
              </span>
            </div>
            <div className="row">
              <button className="ghost" style={{ marginTop: 0, padding: "2px 10px" }}
                onClick={() => setQty((q) => ({ ...q, [c.sku]: Math.max(0, q[c.sku] - 1) }))}>
                −
              </button>
              <span style={{ minWidth: 20, textAlign: "center", fontFamily: "var(--mono)" }}>{qty[c.sku]}</span>
              <button className="ghost" style={{ marginTop: 0, padding: "2px 10px" }}
                onClick={() => setQty((q) => ({ ...q, [c.sku]: q[c.sku] + 1 }))}>
                +
              </button>
            </div>
          </div>
        ))}

        <div className="row" style={{ justifyContent: "space-between", marginTop: 16, borderTop: "1px solid var(--border)", paddingTop: 14 }}>
          <span className="meta" style={{ color: "var(--muted)" }}>Total</span>
          <strong style={{ fontFamily: "var(--mono)", fontSize: 18 }}>${amount.toFixed(2)}</strong>
        </div>

        <div className="row">
          <button onClick={placeOrder} disabled={busy || items.length === 0}>
            {busy ? "Placing order…" : "Place order"}
          </button>
          <button className="ghost" onClick={() => navigate("/orders")}>View orders</button>
        </div>

        {result && <div className={`banner ${result.ok ? "ok" : "err"}`}>{result.msg}</div>}
      </div>
    </>
  );
}