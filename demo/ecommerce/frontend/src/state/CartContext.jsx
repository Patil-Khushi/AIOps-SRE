import { createContext, useContext, useEffect, useMemo, useReducer } from "react";
import { getProduct, DELIVERY_FEE, FREE_DELIVERY_ABOVE } from "../data/catalog.js";
import { loadCart, saveCart } from "./session.js";
import { round2 } from "../lib/format.js";

// Cart state.
//
// Shape is deliberately minimal: { [productId]: qty }. Prices and names are
// never stored — they are re-derived from the catalog on every render, so a
// persisted cart cannot drift from what the page shows or from the `amount`
// we POST.
//
// A reducer (not useState) because `clear()` fires from an async callback
// after the order POST, where chained setState spreads would race.

const CartContext = createContext(null);

function reducer(items, action) {
  switch (action.type) {
    case "add": {
      const next = { ...items };
      next[action.id] = Math.min(10, (next[action.id] || 0) + (action.qty || 1));
      return next;
    }
    case "setQty": {
      const next = { ...items };
      const q = Math.max(0, Math.min(10, Math.trunc(Number(action.qty) || 0)));
      if (q === 0) delete next[action.id];
      else next[action.id] = q;
      return next;
    }
    case "remove": {
      const next = { ...items };
      delete next[action.id];
      return next;
    }
    case "clear":
      return {};
    default:
      return items;
  }
}

export function CartProvider({ children }) {
  const [items, dispatch] = useReducer(reducer, null, loadCart);

  useEffect(() => {
    saveCart(items);
  }, [items]);

  const value = useMemo(() => {
    // Unknown ids are dropped defensively — a persisted cart may reference a
    // product that has since left the catalog.
    const lines = Object.entries(items)
      .map(([id, qty]) => ({ product: getProduct(id), qty }))
      .filter((l) => l.product)
      .map((l) => ({ ...l, lineTotal: l.product.price * l.qty }));

    const subtotal = lines.reduce((s, l) => s + l.lineTotal, 0);
    const savings = lines.reduce((s, l) => s + Math.max(0, l.product.mrp - l.product.price) * l.qty, 0);
    const shipping = subtotal === 0 || subtotal >= FREE_DELIVERY_ABOVE ? 0 : DELIVERY_FEE;

    return {
      items,
      lines,
      count: lines.reduce((s, l) => s + l.qty, 0),
      subtotal,
      savings,
      shipping,
      total: subtotal + shipping,
      qtyOf: (id) => items[id] || 0,
      add: (id, qty = 1) => dispatch({ type: "add", id, qty }),
      setQty: (id, qty) => dispatch({ type: "setQty", id, qty }),
      remove: (id) => dispatch({ type: "remove", id }),
      clear: () => dispatch({ type: "clear" }),
    };
  }, [items]);

  return <CartContext.Provider value={value}>{children}</CartContext.Provider>;
}

export function useCart() {
  const ctx = useContext(CartContext);
  if (!ctx) throw new Error("useCart must be used inside <CartProvider>");
  return ctx;
}

/**
 * Map cart lines to the exact payload order-service expects.
 * See order-service/src/models/order.py — OrderItem{sku: str, qty: int,
 * price: float} and CreateOrderRequest{items, amount: float}.
 *
 * `total` includes shipping so the number charged matches the number shown.
 */
export function toOrderPayload(lines, total) {
  return {
    items: lines.map((l) => ({
      sku: String(l.product.id),
      qty: Number(l.qty),
      price: Number(l.product.price),
    })),
    amount: round2(total),
  };
}
