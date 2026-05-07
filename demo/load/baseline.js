// k6 baseline load — gives anomaly detectors a steady state to depart from.
//
//   k6 run demo/load/baseline.js
//
// Defaults assume the OTel demo frontend is reachable at FRONTEND_URL.
// infra/bootstrap.ps1 forwards it to http://localhost:8080.

import http from 'k6/http';
import { sleep, group } from 'k6';

const FRONTEND_URL = __ENV.FRONTEND_URL || 'http://localhost:8080';

export const options = {
  // Two-stage steady-state to mimic gentle business-hours traffic.
  stages: [
    { duration: '30s', target: 5 },
    { duration: '5m',  target: 5 },
    { duration: '30s', target: 0 },
  ],
  thresholds: {
    http_req_failed: ['rate<0.05'],
    http_req_duration: ['p(95)<2000'],
  },
};

const PRODUCTS = ['OLJCESPC7Z', '66VCHSJNUP', '1YMWWN1N4O', 'L9ECAV7KIM', '2ZYFJ3GM2N'];

export default function () {
  group('home', () => {
    http.get(`${FRONTEND_URL}/`);
    sleep(1);
  });

  group('product', () => {
    const id = PRODUCTS[Math.floor(Math.random() * PRODUCTS.length)];
    http.get(`${FRONTEND_URL}/product/${id}`);
    sleep(1);
  });

  group('cart-add', () => {
    http.post(`${FRONTEND_URL}/cart`, JSON.stringify({
      productId: PRODUCTS[0],
      quantity: 1,
    }), { headers: { 'Content-Type': 'application/json' } });
    sleep(2);
  });
}
