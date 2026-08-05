import { Link, NavLink, useNavigate } from "react-router-dom";
import { Package, Search, ShoppingCart, User } from "lucide-react";
import { useAuth } from "../../state/AuthContext.jsx";
import { useCart } from "../../state/CartContext.jsx";

export default function Header() {
  const { status, user } = useAuth();
  const { count } = useCart();
  const navigate = useNavigate();
  const authed = status === "authed";

  return (
    <header className="sticky top-0 z-20 border-b border-line bg-canvas/95 backdrop-blur">
      <div className="mx-auto flex max-w-7xl items-center gap-6 px-6 py-3">
        <Link to="/" className="flex shrink-0 items-center gap-1.5">
          <span className="font-display text-2xl font-bold tracking-tight">orbit</span>
          <span className="inline-block h-2 w-2 rounded-full bg-coral" />
        </Link>

        <div className="hidden max-w-xl flex-1 items-center gap-2 rounded-full border border-line bg-white px-4 py-2 sm:flex">
          <Search size={16} className="text-muted" />
          <span className="text-sm text-muted">Search products, brands and more</span>
        </div>

        <div className="ml-auto flex items-center gap-4">
          {authed && (
            <NavLink
              to="/orders"
              className={({ isActive }) =>
                `hidden items-center gap-1.5 text-sm font-medium transition hover:text-coral md:flex ${
                  isActive ? "text-coral" : ""
                }`
              }
            >
              <Package size={18} />
              Orders
            </NavLink>
          )}

          <button
            type="button"
            onClick={() => navigate(authed ? "/account" : "/login")}
            className="flex items-center gap-1.5 text-sm font-medium transition hover:text-coral"
          >
            <User size={19} />
            <span className="hidden md:inline">
              {authed ? (user?.name?.split(" ")[0] ?? user?.email ?? "Account") : "Login"}
            </span>
          </button>

          <button
            type="button"
            onClick={() => navigate("/cart")}
            className="relative flex items-center gap-1.5 text-sm font-medium transition hover:text-coral"
          >
            <ShoppingCart size={19} />
            <span className="hidden md:inline">Cart</span>
            {count > 0 && (
              <span className="mono absolute -right-2 -top-2 flex h-[18px] min-w-[18px] items-center justify-center rounded-full bg-coral px-1 text-[10px] text-white md:static md:ml-0">
                {count}
              </span>
            )}
          </button>
        </div>
      </div>
    </header>
  );
}
