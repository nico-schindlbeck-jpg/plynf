// Plynf dashboard SPA entrypoint.
//
// Mounts the Preact app under #app, sets up the router, and decides
// whether to land on the welcome wizard or the existing dashboard
// based on whether any tenant exists yet.

import { render } from "preact";
import { App } from "./app";

const root = document.getElementById("app");
if (!root) {
  throw new Error("#app not found in index.html — bad mount target");
}

render(<App />, root);
