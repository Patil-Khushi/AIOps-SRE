import { Navigate, Route, Routes } from "react-router-dom";
import Header from "./components/layout/Header.jsx";
import Footer from "./components/layout/Footer.jsx";
import RequireAuth from "./routes/RequireAuth.jsx";
import RequireCheckoutStep from "./routes/RequireCheckoutStep.jsx";
import { STEP } from "./data/checkoutSteps.js";

import Home from "./pages/Home.jsx";
import ProductDetail from "./pages/ProductDetail.jsx";
import Cart from "./pages/Cart.jsx";
import Login from "./pages/Login.jsx";
import Register from "./pages/Register.jsx";
import Account from "./pages/Account.jsx";
import Orders from "./pages/Orders.jsx";
import Address from "./pages/checkout/Address.jsx";
import Payment from "./pages/checkout/Payment.jsx";
import Confirmation from "./pages/checkout/Confirmation.jsx";

export default function App() {
  return (
    <div className="flex min-h-screen flex-col">
      <Header />
      <main className="flex-1">
        <Routes>
          {/* public */}
          <Route path="/" element={<Home />} />
          <Route path="/product/:id" element={<ProductDetail />} />
          <Route path="/cart" element={<Cart />} />
          <Route path="/login" element={<Login />} />
          <Route path="/register" element={<Register />} />

          {/* checkout — auth + sequence guarded */}
          <Route
            path="/checkout/address"
            element={
              <RequireAuth>
                <RequireCheckoutStep step={STEP.ADDRESS}>
                  <Address />
                </RequireCheckoutStep>
              </RequireAuth>
            }
          />
          <Route
            path="/checkout/payment"
            element={
              <RequireAuth>
                <RequireCheckoutStep step={STEP.PAYMENT}>
                  <Payment />
                </RequireCheckoutStep>
              </RequireAuth>
            }
          />
          <Route
            path="/checkout/confirmation/:orderId"
            element={
              <RequireAuth>
                <RequireCheckoutStep step={STEP.CONFIRMATION}>
                  <Confirmation />
                </RequireCheckoutStep>
              </RequireAuth>
            }
          />

          {/* account */}
          <Route
            path="/orders"
            element={
              <RequireAuth>
                <Orders />
              </RequireAuth>
            }
          />
          <Route
            path="/account"
            element={
              <RequireAuth>
                <Account />
              </RequireAuth>
            }
          />

          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
      <Footer />
    </div>
  );
}
