import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client.js";

export default function Register() {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null); // { ok, msg }
  const navigate = useNavigate();

  async function submit() {
    setBusy(true);
    setResult(null);
    const r = await api.register(name, email, password);
    setBusy(false);
    if (r.ok) {
      setResult({ ok: true, msg: "Account created. Redirecting to login…" });
      setTimeout(() => navigate("/login"), 900);
    } else {
      setResult({ ok: false, msg: r.error || r.data?.detail || `Registration failed (HTTP ${r.status || "network"})` });
    }
  }

  return (
    <>
      <h1>Create account</h1>
      <p className="sub">Registers a user in the User Service → MySQL.</p>
      <div className="card">
        <label>Name</label>
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Ada Lovelace" />
        <label>Email</label>
        <input value={email} onChange={(e) => setEmail(e.target.value)} placeholder="ada@example.com" type="email" />
        <label>Password</label>
        <input value={password} onChange={(e) => setPassword(e.target.value)} type="password" placeholder="••••••••" />
        <button onClick={submit} disabled={busy || !name || !email || !password}>
          {busy ? "Creating…" : "Create account"}
        </button>
        {result && <div className={`banner ${result.ok ? "ok" : "err"}`}>{result.msg}</div>}
      </div>
    </>
  );
}