import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App.jsx";
import { AuthProvider } from "./state/AuthContext.jsx";
import { CartProvider } from "./state/CartContext.jsx";
import { CheckoutProvider } from "./state/CheckoutContext.jsx";
import "./styles.css";

// Providers sit ABOVE the router outlet so cart and session survive every
// route change — including the login bounce in the middle of checkout.
ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <BrowserRouter>
      <AuthProvider>
        <CartProvider>
          <CheckoutProvider>
            <App />
          </CheckoutProvider>
        </CartProvider>
      </AuthProvider>
    </BrowserRouter>
  </React.StrictMode>
);
