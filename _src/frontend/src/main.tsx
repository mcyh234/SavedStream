import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { I18nProvider } from "./I18n";
import { MediaCryptoProvider } from "./MediaCrypto";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <I18nProvider><MediaCryptoProvider><App /></MediaCryptoProvider></I18nProvider>
  </StrictMode>,
);
