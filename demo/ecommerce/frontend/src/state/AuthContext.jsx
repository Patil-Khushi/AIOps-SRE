import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { api, clearToken, setToken } from "../api/client.js";
import { decodeJwt, isExpired, loadToken, clearSession } from "./session.js";
import { describeApiError } from "../lib/errors.js";

// Auth state as a three-state machine: 'loading' | 'authed' | 'anon'.
//
// The three rules below are load-bearing for the failure demos. Getting any
// of them wrong makes the UI mis-attribute an infrastructure outage as an
// authentication problem, which is exactly the mistake the AIOps agents are
// meant to catch.

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [status, setStatus] = useState("loading");
  const [user, setUser] = useState(null);
  // Set when the session is believed good but /profile could not confirm it
  // (user-service degraded). Rendered as a banner, never as a logout.
  const [degraded, setDegraded] = useState(null);

  // --- Rule 1: optimistic-then-reconcile ---------------------------------
  // Decode the stored JWT for display and render the authed shell straight
  // away, then confirm with /profile in the background. First paint must
  // never block on a network call.
  useEffect(() => {
    let cancelled = false;
    const token = loadToken();
    const claims = decodeJwt(token);

    // Rule 3: check exp locally so we never fire a request we know will 401.
    if (!token || !claims || isExpired(claims)) {
      if (token) clearToken();
      setStatus("anon");
      return;
    }

    setUser({ id: Number(claims.sub), email: claims.email, name: null });
    setStatus("authed");

    api.profile().then((r) => {
      if (cancelled) return;
      if (r.ok) {
        setUser(r.data);
        setDegraded(null);
        return;
      }
      // --- Rule 2: ONLY 401/403 clears the session ------------------------
      // A 5xx or a network failure means the User Service is broken, not
      // that this person is unauthenticated. Logging them out here would
      // silently break user_service_mysql_down and blame the wrong thing.
      const err = describeApiError(r, { service: "User Service" });
      if (err.kind === "auth") {
        clearToken();
        setUser(null);
        setStatus("anon");
      } else {
        setDegraded(err);
      }
    });

    return () => {
      cancelled = true;
    };
  }, []);

  /**
   * Real sign-in: login → store token → fetch profile.
   * Returns { ok } or { ok: false, error } so callers can render the
   * taxonomy rather than a flattened string.
   */
  const signIn = useCallback(async (email, password) => {
    const r = await api.login(email, password);
    if (!r.ok) {
      return { ok: false, error: describeApiError(r, { service: "User Service" }) };
    }

    const token = r.data?.access_token || r.data?.token;
    if (!token) {
      return {
        ok: false,
        error: { kind: "unknown", title: "No token returned", detail: null, http: r.status, ms: r.ms },
      };
    }
    setToken(token);

    const claims = decodeJwt(token);
    const p = await api.profile();
    if (p.ok) {
      setUser(p.data);
      setDegraded(null);
    } else {
      // Token was issued, so the credentials were good — the profile read
      // failing is a separate fault (typically MySQL down). Let them in with
      // whatever the JWT claims carry, and flag the degradation.
      setUser({ id: Number(claims?.sub), email: claims?.email ?? email, name: null });
      setDegraded(describeApiError(p, { service: "User Service" }));
    }
    setStatus("authed");
    return { ok: true };
  }, []);

  const signOut = useCallback(() => {
    clearSession();
    setUser(null);
    setDegraded(null);
    setStatus("anon");
  }, []);

  /** Called when a downstream 401 proves the session is genuinely dead. */
  const invalidate = useCallback(() => {
    clearToken();
    setUser(null);
    setStatus("anon");
  }, []);

  const value = useMemo(
    () => ({ status, user, degraded, signIn, signOut, invalidate, dismissDegraded: () => setDegraded(null) }),
    [status, user, degraded, signIn, signOut, invalidate],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}
