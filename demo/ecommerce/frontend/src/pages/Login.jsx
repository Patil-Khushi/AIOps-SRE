import { useState } from "react";
import { api, setToken } from "../api/client.js";

export default function Login({ onAuth }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);

  async function submit() {
    setBusy(true);
    setResult(null);
    const r = await api.login(email, password);
    if (!r.ok) {
      setBusy(false);
      setResult({ ok: false, msg: r.error || r.data?.detail || `Login failed (HTTP ${r.status || "network"})` });
      return;
    }
    const token = r.data?.access_token || r.data?.token;
    setToken(token);

    // Pull profile so the shell knows who is logged in.
    const p = await api.profile();
    setBusy(false);
    if (p.ok) {
      onAuth({ name: p.data?.name, email: p.data?.email || email });
    } else {
      // Token issued but profile failed — still let them in, show email only.
      onAuth({ name: null, email });
    }
  }

  return (
    <>
      <h1>Log in</h1>
      <p className="sub">Authenticates against the User Service and stores the returned JWT.</p>
      <div className="card">
        <label>Email</label>
        <input value={email} onChange={(e) => setEmail(e.target.value)} placeholder="ada@example.com" type="email" />
        <label>Password</label>
        <input value={password} onChange={(e) => setPassword(e.target.value)} type="password" placeholder="••••••••" />
        <button onClick={submit} disabled={busy || !email || !password}>
          {busy ? "Signing in…" : "Log in"}
        </button>
        {result && <div className={`banner ${result.ok ? "ok" : "err"}`}>{result.msg}</div>}
      </div>
    </>
  );
}