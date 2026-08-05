// One error taxonomy for the whole app.
//
// This app exists to be broken and observed, so "Something went wrong" is a
// bug. Every failure must say WHICH service failed, with WHAT status, after
// HOW long — that is the information the removed request-log sidebar used to
// carry, and it now lives inside the error panels.
//
// The `kind` field drives two behaviours:
//   'auth'   → the session is genuinely invalid; clear it
//   'outage' → a service is down; KEEP the session (see AuthContext)
//   'user'   → the person did something wrong; show it on the field
//   'unknown'→ surface the detail verbatim rather than guessing

/** Pull FastAPI's message out of a client.js result. */
export function detailOf(r) {
  const d = r?.data?.detail ?? r?.data?.message ?? r?.error;
  if (typeof d === "string") return d;
  // Pydantic 422 returns a list of {loc, msg, type}
  if (Array.isArray(d) && d.length) return d.map((e) => e?.msg).filter(Boolean).join("; ");
  return null;
}

/**
 * Classify a failed `client.js` result.
 *
 * Note the 401 carve-out: order-service maps "user-service unreachable" to a
 * 401 that is shape-identical to an expired token (create_order.py:37).
 * Treating that as an auth failure would log people out during a user-service
 * outage and mis-attribute the fault — so it is classified as an outage.
 */
export function describeApiError(r, { service = "service" } = {}) {
  const http = r?.status ?? 0;
  const ms = r?.ms ?? null;
  const detail = detailOf(r);

  if (http === 0) {
    return {
      kind: "outage",
      title: `Could not reach the ${service}`,
      detail: r?.error || "Connection refused, DNS failure, or blocked by CORS.",
      http,
      ms,
    };
  }

  if (http === 401 || http === 403) {
    // --- Service-outage 401s masquerading as auth failures ----------------
    // order-service turns EVERY user-validation problem into a 401
    // (create_order.py:37), including ones that are really upstream outages.
    // Two shapes come out of user_service_client.validate_user:
    //
    //   "user service unreachable"            → user-service is down
    //   "user validation failed (HTTP 500)"   → user-service up, MySQL down
    //   "user validation failed (HTTP 401)"   → genuinely bad token
    //
    // Verified live: under user_service.mysql_down, GET /orders returns
    // 401 "user validation failed (HTTP 500)". Treating that as an auth
    // failure would log the person out mid-outage and blame the wrong
    // component — the exact mis-attribution this app exists to expose.
    if (detail && /unreachable/i.test(detail)) {
      return {
        kind: "outage",
        title: "Could not validate your session",
        detail: `The ${service} could not reach the User Service. Your session is still valid.`,
        http,
        ms,
      };
    }
    const upstream = /user validation failed \(HTTP (\d{3})\)/i.exec(detail || "");
    if (upstream) {
      const code = Number(upstream[1]);
      if (code >= 500 || code === 0) {
        return {
          kind: "outage",
          title: "Could not validate your session",
          detail: `The User Service returned HTTP ${code}. Your session is still valid.`,
          http,
          ms,
        };
      }
      // 401/403 from upstream — the token really is bad.
    }
    if (detail && /invalid credentials/i.test(detail)) {
      return { kind: "user", title: "Email or password is incorrect", detail: null, http, ms };
    }
    return {
      kind: "auth",
      title: "Your session has expired",
      detail: "Please sign in again.",
      http,
      ms,
    };
  }

  if (http === 409) {
    return {
      kind: "user",
      title: "An account with this email already exists",
      detail: null,
      http,
      ms,
    };
  }

  if (http === 404) {
    return { kind: "unknown", title: "Not found", detail, http, ms };
  }

  if (http >= 500) {
    // Distinguish infrastructure from injected application faults — the
    // difference matters when attributing a scenario.
    if (detail && /database/i.test(detail)) {
      return {
        kind: "outage",
        title: `The ${service} could not reach its database`,
        detail: "Your request was not recorded.",
        http,
        ms,
      };
    }
    if (http === 502 || http === 504) {
      return {
        kind: "outage",
        title: http === 504 ? "The payment provider timed out" : "The payment was declined",
        detail,
        http,
        ms,
      };
    }
    return {
      kind: "outage",
      title: `The ${service} returned an error`,
      detail: detail || "No detail supplied.",
      http,
      ms,
    };
  }

  return { kind: "unknown", title: "Request failed", detail, http, ms };
}

/** "HTTP 504 · 5,012 ms" — the provenance line shown under every error. */
export function provenance(err) {
  const bits = [];
  if (err?.http) bits.push(`HTTP ${err.http}`);
  else bits.push("no response");
  if (err?.ms != null) bits.push(`${Number(err.ms).toLocaleString("en-IN")} ms`);
  return bits.join(" · ");
}
