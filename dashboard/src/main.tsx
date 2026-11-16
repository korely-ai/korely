import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { ThemeProvider } from "@/contexts/ThemeContext";
import { partsChunk } from "@/lib/cloud";
import { returnFirst } from "@/lib/returns";
import "./index.css";

function render(): void {
  ReactDOM.createRoot(document.getElementById("root")!).render(
    <React.StrictMode>
      <ThemeProvider>
        <App />
      </ThemeProvider>
    </React.StrictMode>,
  );
}

// What a source outside sent back to a part's page (an OAuth `code` and
// `state`, in the query), out of the address before anything renders or
// reads it: lib/returns.ts. Only an address that ends in `/callback` loses
// its query and then asks the parts which addresses they take back (their
// chunk, lib/cloud.ts partsChunk, given RETURN_WAIT_MS); any other address
// asks nothing. The screen renders once that is done, whatever the chunk
// did.
returnFirst(window.location, window.history, partsChunk).finally(render);
