import { USER_SERVICE_URL, ORDER_SERVICE_URL } from "../../api/client.js";

/**
 * The footer doubles as the honesty line. This storefront is a system under
 * test for the AIOps agents, and the service URLs it is actually talking to
 * are worth showing — during a failure demo it is the fastest way to confirm
 * which endpoints the browser is hitting.
 */
export default function Footer() {
  return (
    <footer className="mt-16 border-t border-line">
      <div className="mx-auto flex max-w-7xl flex-col gap-2 px-6 py-6 text-xs text-muted sm:flex-row sm:items-center sm:justify-between">
        <p>
          <span className="font-display font-semibold text-ink">orbit</span> — a demo storefront used
          as a system under test. Orders, payments and accounts are real; delivery is not.
        </p>
        <p className="mono truncate">
          {new URL(USER_SERVICE_URL, window.location.origin).host} ·{" "}
          {new URL(ORDER_SERVICE_URL, window.location.origin).host}
        </p>
      </div>
    </footer>
  );
}
