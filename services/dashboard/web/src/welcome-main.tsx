/**
 * Entry point for the First-Run-Wizard at /welcome.
 *
 * Block 4 hook. Block 5 (Tauri) will iframe-load this same route on
 * first launch. Mounts Preact onto #root from welcome.html.
 */

import { render } from "preact";
import { WelcomeWizard } from "./routes/welcome";
import "./styles/global.css";

const root = document.getElementById("root");
if (!root) {
  throw new Error("Plinth dashboard: #root element missing from welcome.html");
}

render(<WelcomeWizard />, root);
