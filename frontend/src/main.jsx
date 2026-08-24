import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import "./styles.css";

// `npm run dev` + `?mock` in the URL: a pretend backend so every view has
// data to show in a plain browser. Tree-shaken out of a production build.
if (import.meta.env.DEV && new URLSearchParams(location.search).has("mock")) {
  const { installDevMock } = await import("./devMock.js");
  installDevMock();
}

createRoot(document.getElementById("root")).render(<App />);
