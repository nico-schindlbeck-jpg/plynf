// Plynf preview-site password gate.
//
// HTTP Basic Auth. Browser shows the native "Sign in" prompt; any username
// works, the password is what counts. Returns 401 with WWW-Authenticate so
// search engines don't index, no JS needed, no third-party dependency.
//
// To unlock: enter any username (e.g. "plynf") and the password below.
// To change the password: edit PASSWORD and re-deploy.
// To remove the gate entirely: delete this file and remove the
// [[edge_functions]] block from netlify.toml.

import type { Context } from "https://edge.netlify.com/";

const REALM = "Plynf — preview site";
const PASSWORD = "Rogginger";

/** Constant-time string compare to avoid timing-attack inference. */
function safeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let acc = 0;
  for (let i = 0; i < a.length; i++) {
    acc |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return acc === 0;
}

function challenge(message = "Authentication required"): Response {
  return new Response(message, {
    status: 401,
    headers: {
      "WWW-Authenticate": `Basic realm="${REALM}", charset="UTF-8"`,
      "Content-Type": "text/plain; charset=utf-8",
      // Prevent caches from serving the 401 cross-user.
      "Cache-Control": "no-store",
    },
  });
}

export default async (request: Request, context: Context) => {
  const auth = request.headers.get("authorization") ?? "";

  if (!auth.startsWith("Basic ")) {
    return challenge();
  }

  let decoded: string;
  try {
    decoded = atob(auth.slice("Basic ".length).trim());
  } catch {
    return challenge("Invalid Authorization header");
  }

  // Basic header form: "username:password" — colon is the separator.
  // Password itself may contain colons; only split on the first one.
  const colonIdx = decoded.indexOf(":");
  if (colonIdx === -1) return challenge("Malformed credentials");
  const password = decoded.slice(colonIdx + 1);

  if (!safeEqual(password, PASSWORD)) {
    return challenge("Invalid credentials");
  }

  // Auth passed — let Netlify continue serving the actual page.
  return context.next();
};

export const config = {
  // Apply to every path EXCEPT health/probe endpoints we want public.
  // /.well-known/* stays public so security.txt + ACME challenges work.
  path: "/*",
  excludedPath: ["/.well-known/*"],
};
