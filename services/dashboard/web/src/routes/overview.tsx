// Overview route — what users see after wizard.
//
// In the migration phase, this can still defer to the existing vanilla-JS
// dashboard at /static/app.js by mounting an iframe or redirecting.
// As we port views over, replace pieces with native Preact components.

import { useEffect, useState } from "preact/hooks";

interface Workspace {
  id: string;
  name: string;
  version: number;
}

export function Overview() {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [ftux, setFtux] = useState<string | null>(null);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    setFtux(params.get("ftux"));
    fetch("/api/workspaces")
      .then((r) => r.json())
      .then((data) => setWorkspaces(data.workspaces ?? []))
      .catch(() => setWorkspaces([]));
  }, []);

  return (
    <div class="container">
      <header class="overview-header">
        <h1>Workspaces</h1>
        <a href="/welcome" class="btn btn-secondary">+ New workspace</a>
      </header>

      {ftux === "run-sample" && (
        <SampleTaskCard onDismiss={() => setFtux(null)} />
      )}

      {workspaces.length === 0 ? (
        <EmptyState />
      ) : (
        <ul class="workspace-list">
          {workspaces.map((w) => (
            <li key={w.id}>
              <a href={`/workspaces/${w.id}`}>
                <span class="name">{w.name}</span>
                <span class="version mono">v{w.version}</span>
              </a>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function SampleTaskCard({ onDismiss }: { onDismiss: () => void }) {
  return (
    <div class="sample-task-card">
      <div class="header">
        <h2>Try the sample task</h2>
        <button class="dismiss" onClick={onDismiss} aria-label="Dismiss">×</button>
      </div>
      <p>
        Runs a 5-source research workflow in mock mode and shows the 71%-fewer-tokens comparison.
        Takes about 30 seconds. No real LLM call.
      </p>
      <a class="btn btn-primary" href="/demos/research-5-source">Run sample task</a>
    </div>
  );
}

function EmptyState() {
  return (
    <div class="empty">
      <p>No workspaces yet.</p>
      <a class="btn btn-primary" href="/welcome">Create your first</a>
    </div>
  );
}
