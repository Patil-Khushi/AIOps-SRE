// Single source of truth for the checkout sequence.
//
// Consumed by BOTH the OrbitTracker (labels + which node is active) and
// RequireCheckoutStep (which redirect to issue), so the two can never drift
// out of sync.

export const CHECKOUT_STEPS = [
  { key: "cart", label: "Cart", path: "/cart" },
  { key: "address", label: "Address", path: "/checkout/address" },
  { key: "payment", label: "Payment", path: "/checkout/payment" },
  { key: "confirmation", label: "Confirmed", path: "/checkout/confirmation" },
];

export const CHECKOUT_LABELS = CHECKOUT_STEPS.map((s) => s.label);

export const STEP = {
  CART: 0,
  ADDRESS: 1,
  PAYMENT: 2,
  CONFIRMATION: 3,
};

/** The address fields we require before allowing the payment step. All of
 *  this is display-only — no address service exists — but the form should
 *  still behave like a real one. */
export const ADDRESS_FIELDS = ["name", "phone", "line1", "city", "pincode"];

export function isAddressComplete(address) {
  if (!address) return false;
  return ADDRESS_FIELDS.every((f) => String(address[f] ?? "").trim().length > 0);
}
