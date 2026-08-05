import { useState } from "react";
import { Link, Navigate, useLocation, useNavigate } from "react-router-dom";
import { Lock, Mail, User } from "lucide-react";
import { api } from "../api/client.js";
import { describeApiError } from "../lib/errors.js";
import { useAuth } from "../state/AuthContext.jsx";
import Field from "../components/ui/Field.jsx";
import Banner from "../components/ui/Banner.jsx";

export default function Register() {
  const { status, signIn } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();

  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  if (status === "authed") return <Navigate to="/" replace />;

  async function submit(e) {
    e?.preventDefault();
    if (busy || !name || !email || !password) return;
    setBusy(true);
    setError(null);

    const r = await api.register(name, email, password);
    if (!r.ok) {
      setBusy(false);
      setError(describeApiError(r, { service: "User Service" }));
      return;
    }

    // POST /register returns {id, name, email} with NO token, so signing the
    // person in needs a second real call. Worth knowing when reading traces:
    // a clean signup is now three requests — /register, /login, /profile.
    const signedIn = await signIn(email, password);
    setBusy(false);
    if (signedIn.ok) {
      navigate(location.state?.from?.pathname ?? "/", { replace: true });
    } else {
      navigate("/login", {
        replace: true,
        state: { ...location.state, email },
      });
    }
  }

  const emailTaken = error?.http === 409;

  return (
    <div className="mx-auto max-w-sm px-6 py-16">
      <h1 className="mb-1 font-display text-2xl font-semibold">Create your account</h1>
      <p className="mb-6 text-sm text-muted">Join Orbit in under a minute.</p>

      <form onSubmit={submit} className="surface flex flex-col gap-4 p-6">
        <Field
          label="Full name"
          icon={User}
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Khushi Patil"
          autoComplete="name"
        />
        <Field
          label="Email"
          icon={Mail}
          type="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          placeholder="you@example.com"
          autoComplete="email"
          error={emailTaken ? "An account with this email already exists" : null}
        />
        <Field
          label="Password"
          icon={Lock}
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="••••••••"
          autoComplete="new-password"
        />

        <button type="submit" disabled={busy || !name || !email || !password} className="btn-primary mt-1">
          {busy ? "Creating…" : "Create account"}
        </button>

        {emailTaken && (
          <p className="text-center text-sm text-muted">
            <Link to="/login" state={{ ...location.state, email }} className="font-medium text-coral">
              Log in instead
            </Link>
          </p>
        )}
        {error && !emailTaken && <Banner tone="err" error={error} />}

        <p className="text-center text-sm text-muted">
          Already have an account?{" "}
          <Link to="/login" state={location.state} className="font-medium text-coral">
            Log in
          </Link>
        </p>
      </form>
    </div>
  );
}
