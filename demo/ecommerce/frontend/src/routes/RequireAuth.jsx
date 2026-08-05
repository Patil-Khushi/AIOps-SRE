import { Navigate, useLocation } from "react-router-dom";
import { useAuth } from "../state/AuthContext.jsx";

/**
 * Auth guard.
 *
 * The `status === 'loading'` branch is not optional: on a refresh the token
 * is rehydrated synchronously but `status` starts as 'loading' for one tick.
 * Redirecting during that tick makes every refresh on a guarded route flash
 * /login and bounce back — visible, ugly, and it corrupts the demo.
 */
export default function RequireAuth({ children }) {
  const { status } = useAuth();
  const location = useLocation();

  if (status === "loading") {
    return (
      <div className="mx-auto max-w-7xl px-6 py-24 text-center">
        <div className="mx-auto h-8 w-8 animate-spin rounded-full border-2 border-line border-t-coral" />
      </div>
    );
  }

  if (status === "anon") {
    // `from` carries the intended destination so login can return there.
    return <Navigate to="/login" replace state={{ from: location }} />;
  }

  return children;
}
