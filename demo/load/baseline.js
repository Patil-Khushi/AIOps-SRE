// k6 baseline load — gives anomaly detectors a steady state to depart from.
//
//   k6 run demo/load/baseline.js
//
// Targets the ecommerce SUT (demo/ecommerce) via the NodePorts that
// demo/ecommerce/k8s/20-app.yaml exposes on the k3s node. Override per service
// if you are port-forwarding instead:
//
//   k6 run -e USER_URL=http://localhost:8081 -e ORDER_URL=http://localhost:8082 demo/load/baseline.js
//
// Only user-service and order-service are called directly. payment-service,
// redis and mock-payment-gateway are exercised *through* POST /orders, which is
// the point: one request walks the whole chain the failure scenarios break, so
// a fault anywhere in it shows up in this script's error rate.

import http from 'k6/http';
import { check, sleep } from 'k6';

const USER_URL = __ENV.USER_URL || 'http://localhost:30081';
const ORDER_URL = __ENV.ORDER_URL || 'http://localhost:30082';

// Fixed pool of load users, registered once in setup(). Bounded on purpose:
// registering per-iteration would grow the `users` table without limit over a
// long soak, and every row is a MySQL connection's worth of work on a demo box.
const VU_POOL = Number(__ENV.VU_POOL || 5);
const PASSWORD = __ENV.LOAD_PASSWORD || 'loadtest123';

export const options = {
  // Two-stage steady-state to mimic gentle business-hours traffic.
  stages: [
    { duration: '30s', target: VU_POOL },
    { duration: '5m', target: VU_POOL },
    { duration: '30s', target: 0 },
  ],
  thresholds: {
    http_req_failed: ['rate<0.05'],
    http_req_duration: ['p(95)<2000'],
  },
};

const JSON_HEADERS = { headers: { 'Content-Type': 'application/json' } };

const CATALOG = [
  { sku: 'SKU-KEYBOARD', price: 49.99 },
  { sku: 'SKU-MOUSE', price: 24.5 },
  { sku: 'SKU-MONITOR', price: 189.0 },
  { sku: 'SKU-HEADSET', price: 79.95 },
  { sku: 'SKU-WEBCAM', price: 64.25 },
];

function loadUser(i) {
  return { name: `Load User ${i}`, email: `loadtest+${i}@example.com`, password: PASSWORD };
}

// Registered once for the whole run. A 409 means a previous run already created
// the account, which is the normal case — re-running the script must not need a
// database reset.
export function setup() {
  const users = [];
  for (let i = 0; i < VU_POOL; i++) {
    const u = loadUser(i);
    const res = http.post(`${USER_URL}/register`, JSON.stringify(u), JSON_HEADERS);
    if (res.status !== 201 && res.status !== 409) {
      throw new Error(`cannot seed load user ${u.email}: ${res.status} ${res.body}`);
    }
    users.push(u);
  }
  return { users };
}

// JWTs last 60 minutes (JWT_EXPIRY_MINUTES), so one login per VU covers a run of
// this length. Cached in VU-local scope rather than fetched per iteration —
// otherwise login traffic would swamp the order traffic and login_requests_total
// would stop meaning "a user signed in".
let token = null;

export default function (data) {
  const user = data.users[(__VU - 1) % data.users.length];

  if (token === null) {
    const res = http.post(
      `${USER_URL}/login`,
      JSON.stringify({ email: user.email, password: user.password }),
      JSON_HEADERS
    );
    if (!check(res, { 'login 200': (r) => r.status === 200 })) {
      sleep(2); // let the dependency recover rather than hot-looping on it
      return;
    }
    token = res.json('access_token');
  }

  const auth = { headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` } };

  // browse — read path against MySQL
  const profile = http.get(`${USER_URL}/profile`, auth);
  check(profile, { 'profile 200': (r) => r.status === 200 });
  sleep(1);

  // checkout — walks order-service -> user-service -> postgres -> payment-service
  //            -> redis -> mock-payment-gateway
  const item = CATALOG[Math.floor(Math.random() * CATALOG.length)];
  const qty = 1 + Math.floor(Math.random() * 3);
  const order = http.post(
    `${ORDER_URL}/orders`,
    JSON.stringify({
      items: [{ sku: item.sku, qty: qty, price: item.price }],
      amount: Math.round(item.price * qty * 100) / 100,
    }),
    auth
  );
  // 401 means the token aged out mid-run; drop it and re-login next iteration.
  if (order.status === 401) {
    token = null;
  }
  check(order, { 'order 201': (r) => r.status === 201 });
  sleep(2);

  // order history — read path against Postgres
  const history = http.get(`${ORDER_URL}/orders`, auth);
  check(history, { 'orders 200': (r) => r.status === 200 });
  sleep(1);
}
