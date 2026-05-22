import { useEffect, useState } from "preact/hooks";
import Router, { Route } from "preact-router";
import { Welcome } from "@/routes/welcome";
import { Overview } from "@/routes/overview";
import { api } from "@/lib/api";

// Top-level router. Decides whether to redirect to /welcome
// (no tenant yet) or show the regular overview.

export function App() {
  const [route, setRoute] = useState<"loading" | "welcome" | "app">("loading");

  useEffect(() => {
    (async () => {
      try {
        const summary = await api.tenantSummary();
        setRoute(summary.count === 0 ? "welcome" : "app");
      } catch (e) {
        // Identity service might not be up yet — show welcome anyway,
        // user will see an error message and retry.
        console.warn("tenant probe failed, falling back to welcome", e);
        setRoute("welcome");
      }
    })();
  }, []);

  if (route === "loading") {
    return <LoadingShell />;
  }

  return (
    <Router>
      <Route path="/welcome" component={Welcome} />
      <Route path="/welcome/:step" component={Welcome} />
      <Route path="/" component={route === "welcome" ? Welcome : Overview} />
      <Route default component={NotFound} />
    </Router>
  );
}

function LoadingShell() {
  return (
    <div class="loading">
      <svg viewBox="0 0 24 24" width="48" height="48" aria-hidden="true">
        <defs>
          <linearGradient id="rock" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stop-color="#ff8551"/>
            <stop offset="50%" stop-color="#d946ef"/>
            <stop offset="100%" stop-color="#6366f1"/>
          </linearGradient>
        </defs>
        <path d="M5 17 L8 9 L12 6 L17 6 L20 11 L20 17 Z" fill="url(#rock)"/>
      </svg>
      <p>Plynf is starting…</p>
    </div>
  );
}

function NotFound() {
  return (
    <div class="container">
      <h1>404</h1>
      <p>That route does not exist.</p>
      <a href="/">Home</a>
    </div>
  );
}
