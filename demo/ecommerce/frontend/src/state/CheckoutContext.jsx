import { createContext, useContext, useMemo, useState } from "react";
import { isAddressComplete } from "../data/checkoutSteps.js";

// Checkout scratch state: delivery address and payment method.
//
// NOTHING here is ever persisted or transmitted. The backend has no address
// service and POST /orders accepts only { items, amount } — see
// order-service/src/models/order.py. These steps exist to complete the
// shopping flow visually, and both screens say so in the UI.
//
// Not persisting is also a safety decision: people type real phone numbers
// and card digits into demos that get screen-shared.

const CheckoutContext = createContext(null);

const EMPTY_ADDRESS = { name: "", phone: "", line1: "", city: "Pune", pincode: "411001" };

export function CheckoutProvider({ children }) {
  const [address, setAddress] = useState(EMPTY_ADDRESS);
  const [method, setMethod] = useState("card");
  // The real order returned by POST /orders, used by the confirmation guard.
  const [lastOrder, setLastOrder] = useState(null);

  const value = useMemo(
    () => ({
      address,
      setAddress,
      method,
      setMethod,
      lastOrder,
      setLastOrder,
      addressComplete: isAddressComplete(address),
      reset: () => {
        setAddress(EMPTY_ADDRESS);
        setMethod("card");
        setLastOrder(null);
      },
    }),
    [address, method, lastOrder],
  );

  return <CheckoutContext.Provider value={value}>{children}</CheckoutContext.Provider>;
}

export function useCheckout() {
  const ctx = useContext(CheckoutContext);
  if (!ctx) throw new Error("useCheckout must be used inside <CheckoutProvider>");
  return ctx;
}
