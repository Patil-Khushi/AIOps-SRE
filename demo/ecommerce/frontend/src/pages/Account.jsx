import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { LogOut } from "lucide-react";
import { api } from "../api/client.js";
import { useAuth } from "../state/AuthContext.jsx";
import Banner from "../components/ui/Banner.jsx";

export default function Account() {
  const { user, degraded, signOut } = useAuth();
  const [orderCount, setOrderCount] = useState(null);
  const navigate = useNavigate();

  // Only fields ProfileResponse actually has ({id, name, email}) plus a real
  // order count. No phone, avatar, or preferences — none of that is stored.
  useEffect(() => {
    let cancelled = false;
    api.getOrders().then((r) => {
      if (cancelled || !r.ok) return;
      const list = Array.isArray(r.data) ? r.data : r.data?.orders || [];
      setOrderCount(list.length);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const initial = (user?.name || user?.email || "?").slice(0, 1).toUpperCase();

  return (
    <div className="mx-auto max-w-md px-6 py-16">
      {degraded && (
        <div className="mb-4">
          <Banner tone="warn" error={degraded}>
            Your session is still valid — some profile details could not be loaded.
          </Banner>
        </div>
      )}

      <div className="surface p-6 text-center">
        <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-full bg-pine font-display text-lg font-semibold text-white">
          {initial}
        </div>
        <h1 className="font-display text-xl font-semibold">{user?.name ?? "Orbit customer"}</h1>
        <p className="mt-1 text-sm text-muted">{user?.email}</p>
        <p className="mono mt-1 text-xs text-muted">
          user #{user?.id}
          {orderCount != null && ` · ${orderCount} order${orderCount === 1 ? "" : "s"}`}
        </p>

        <div className="my-5 border-t border-line" />

        <button type="button" onClick={() => navigate("/orders")} className="btn-outline mb-3 w-full">
          View orders
        </button>
        <button type="button" onClick={() => navigate("/")} className="btn-outline mb-3 w-full">
          Continue shopping
        </button>
        <button
          type="button"
          onClick={() => {
            signOut();
            navigate("/", { replace: true });
          }}
          className="flex w-full items-center justify-center gap-2 py-2.5 text-sm font-medium text-coral"
        >
          <LogOut size={16} /> Log out
        </button>
      </div>
    </div>
  );
}
