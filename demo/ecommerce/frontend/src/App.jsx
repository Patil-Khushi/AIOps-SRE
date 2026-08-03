import { useEffect, useState } from "react";
import { Routes, Route, NavLink, Navigate, useNavigate } from "react-router-dom";
import { onRequest, clearToken } from "./api/client.js";
import Login from "./pages/Login.jsx";
import Register from "./pages/Register.jsx";
import Orders from "./pages/Orders.jsx";
import Checkout from "./pages/Checkout.jsx";

function RequestLog() {
  const [entries, setEntries] = useState([]);
  useEffect(() => onRequest((e) => setEntries((prev) => [e, ...prev].slice(0, 50))), []);

  return (
    <aside className="log-panel">
      <div className="log-head">
        <h2>Request log</h2>
        {entries.length > 0 && (
          <button className="ghost" style={{ marginTop: 0, padding: "4px 10px" }} onClick={() => setEntries([])}>
            Clear
          </button>
        )}
      </div>
      {entries.length === 0 ? (
        <p className="log-empty">No requests yet. Actions you take appear here with status &amp; latency.</p>
      ) : (
        entries.map((e) => (
          <div key={e.id} className={`log-entry ${e.ok ? "ok" : "err"}`}>
            <div className="l1">
              <span className={`status ${e.ok ? "ok" : "err"}`}>{e.status ?? "ERR"}</span>
              <span className="method">{e.method}</span>
              <span className="path">{new URL(e.url).pathname}</span>
              <span className="ms">{e.ms}ms</span>
            </div>
            {e.error && <div className="err-line">{e.error}</div>}
          </div>
        ))
      )}
    </aside>
  );
}

export default function App() {
  const [user, setUser] = useState(null); // { name, email }
  const navigate = useNavigate();

  function handleLogout() {
    clearToken();
    setUser(null);
    navigate("/login");
  }

  const authed = !!user;

  return (
    <div className="app">
      <div className="main">
        <div className="topbar">
          <div className="brand">ecommerce<span> / sre-demo</span></div>
          <nav className="nav">
            {authed && <NavLink to="/checkout" className={({ isActive }) => (isActive ? "active" : "")}>Checkout</NavLink>}
            {authed && <NavLink to="/orders" className={({ isActive }) => (isActive ? "active" : "")}>Orders</NavLink>}
          </nav>
          <span className="spacer" />
          {authed ? (
            <>
              <span className="who">{user.email}</span>
              <button className="ghost" style={{ marginTop: 0, padding: "6px 12px" }} onClick={handleLogout}>
                Log out
              </button>
            </>
          ) : (
            <nav className="nav">
              <NavLink to="/login" className={({ isActive }) => (isActive ? "active" : "")}>Login</NavLink>
              <NavLink to="/register" className={({ isActive }) => (isActive ? "active" : "")}>Register</NavLink>
            </nav>
          )}
        </div>

        <Routes>
          <Route path="/login" element={authed ? <Navigate to="/checkout" /> : <Login onAuth={setUser} />} />
          <Route path="/register" element={authed ? <Navigate to="/checkout" /> : <Register />} />
          <Route path="/checkout" element={authed ? <Checkout /> : <Navigate to="/login" />} />
          <Route path="/orders" element={authed ? <Orders /> : <Navigate to="/login" />} />
          <Route path="*" element={<Navigate to={authed ? "/checkout" : "/login"} />} />
        </Routes>
      </div>

      <RequestLog />
    </div>
  );
}