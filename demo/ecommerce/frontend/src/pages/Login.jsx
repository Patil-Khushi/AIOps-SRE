import { useState } from "react";
import { Link, Navigate, useLocation, useNavigate } from "react-router-dom";
import { Lock, Mail } from "lucide-react";
import { useAuth } from "../state/AuthContext.jsx";
import Field from "../components/ui/Field.jsx";
import Banner from "../components/ui/Banner.jsx";

export default function Login() {
  const { status, signIn } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();

  const [email, setEmail] = useState(location.state?.email ?? "");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  if (status === "authed") return <Navigate to={location.state?.from?.pathname ?? "/"} replace />;

  async function submit(e) {
    e?.preventDefault();
    if (busy || !email || !password) return;
    setBusy(true);
    setError(null);

    const r = await signIn(email, password);
    setBusy(false);
    if (r.ok) {
      navigate(location.state?.from?.pathname ?? "/", { replace: true });
      return;
    }
    setError(r.error);
  }

  // The taxonomy split matters: a wrong password is the person's problem and
  // belongs on the field; a 500 from MySQL being down is infrastructure and
  // must NOT be rendered as "your password is wrong".
  const isCredentialError = error?.kind === "user";

  return (
    <div className="mx-auto max-w-sm px-6 py-16">
      <h1 className="mb-1 font-display text-2xl font-semibold">Log in to Orbit</h1>
      <p className="mb-6 text-sm text-muted">Track orders and check out faster.</p>

      <form onSubmit={submit} className="surface flex flex-col gap-4 p-6">
        <Field
          label="Email"
          icon={Mail}
          type="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          placeholder="you@example.com"
          autoComplete="email"
        />
        <Field
          label="Password"
          icon={Lock}
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="••••••••"
          autoComplete="current-password"
          error={isCredentialError ? error.title : null}
        />

        <button type="submit" disabled={busy || !email || !password} className="btn-primary mt-1">
          {busy ? "Signing in…" : "Log in"}
        </button>

        {error && !isCredentialError && <Banner tone="err" error={error} />}

        <p className="text-center text-sm text-muted">
          New to Orbit?{" "}
          <Link to="/register" state={location.state} className="font-medium text-coral">
            Create an account
          </Link>
        </p>
      </form>
    </div>
  );
}
