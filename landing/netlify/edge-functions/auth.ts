// Plynf preview-site password gate (Netlify Edge Function).
//
// Browser shows the native "Sign in" prompt; any username works, the
// password below is the gate. Returns 401 (with WWW-Authenticate header)
// so search engines don't index. /.well-known/* stays public for ACME
// cert renewal + security.txt.
//
// To change the password: edit PASSWORD and re-deploy.
// To remove the gate: delete this file + the [[edge_functions]] block
// in netlify.toml.

const PASSWORD = "Rogginger";

function unauthorized() {
  return new Response("Authentication required\n", {
    status: 401,
    headers: {
      "WWW-Authenticate": 'Basic realm="Plynf preview"',
      "Content-Type": "text/plain",
    },
  });
}

export default async (request, context) => {
  try {
    const url = new URL(request.url);

    // ACME challenges + security.txt stay public.
    if (url.pathname.startsWith("/.well-known/")) {
      return context.next();
    }

    const auth = request.headers.get("authorization");

    if (auth && auth.startsWith("Basic ")) {
      try {
        const decoded = atob(auth.slice(6).trim());
        // Basic auth = "user:password" — split only on FIRST colon so
        // passwords with colons in them still work.
        const colon = decoded.indexOf(":");
        const password = colon === -1 ? "" : decoded.slice(colon + 1);
        if (password === PASSWORD) {
          return context.next();
        }
      } catch (_) {
        // malformed base64 — fall through to 401
      }
    }

    return unauthorized();
  } catch (_) {
    // Should never reach this in normal flow, but Netlify shows a generic
    // crash page on uncaught exceptions — better to fall back to 401 than
    // leak an error UI.
    return unauthorized();
  }
};

export const config = {
  path: "/*",
};
