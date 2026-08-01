import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { MediaCryptoProvider } from "./MediaCrypto";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <MediaCryptoProvider><App /></MediaCryptoProvider>
  </StrictMode>,
);

