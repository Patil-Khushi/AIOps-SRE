import { Navigate } from "react-router-dom";
import { useCart } from "../state/CartContext.jsx";
import { useCheckout } from "../state/CheckoutContext.jsx";
import { STEP } from "../data/checkoutSteps.js";

/**
 * Stops someone deep-linking into the middle of checkout.
 *
 * Order of the rules matters. The confirmation case MUST be handled first
 * and MUST NOT apply the empty-cart rule: the cart is cleared the instant an
 * order succeeds, so an empty-cart check on confirmation would bounce every
 * successful purchase straight back to /cart.
 */
export default function RequireCheckoutStep({ step, children }) {
  const cart = useCart();
  const { lastOrder, addressComplete } = useCheckout();

  if (step === STEP.CONFIRMATION) {
    // Reachable only after a real order came back. On a hard refresh the
    // page itself re-reads the order by id from the URL, so this guard only
    // catches someone typing the path in cold.
    if (!lastOrder) return <Navigate to="/orders" replace />;
    return children;
  }

  if (cart.lines.length === 0) return <Navigate to="/cart" replace />;

  if (step >= STEP.PAYMENT && !addressComplete) {
    return <Navigate to="/checkout/address" replace />;
  }

  return children;
}
