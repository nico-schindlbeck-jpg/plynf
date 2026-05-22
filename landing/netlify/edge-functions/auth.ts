// Plynf preview-site password gate.
//
// HTTP Basic Auth. Browser shows the native "Sign in" prompt; any username
// works, the password is what counts. Returns 401 with WWW-Authenticate so
// search engines don't index. No JS in the page, no third-party dependency.
//
// To unlock: enter any username (e.g. "plynf") and the password below.
// To change the password: edit PASSWORD and re-deploy.
// To remove the gate entirely: delete this file and remove the
// [[edge_functions]] block from netlify.toml.

const REALM = "Plynf — preview site";
const PASSWORD = "Rogginger";

/** Constant-time string compare to avoid timing-attack inference. */
function safeEqual(a, b) {
  if (a.length !== b.length) return false;
  let acc = 0;
  for (let i = 0; i < a.length; i++) {
    acc |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return acc === 0;
}

function challenge(message) {
  return new Response(message || "Authentication required", {
    status: 401,
    headers: {
      "WWW-Authenticate": 'Basic realm="' + REALM + '", charset="UTF-8"',
      "Content-Type": "text/plain; charset=utf-8",
      "Cache-Control": "no-store",
    },
  });
}

export default async (request, context) => {
  const url = new URL(request.url);

  // ACME http-01 challenges + RFC 9116 security.txt stay public so cert
  // renewal and vulnerability disclosure are never gated.
  if (url.pathname.startsWith("/.well-known/")) {
    return context.next();
  }

  const auth = request.headers.get("authorization") || "";

  if (!auth.startsWith("Basic ")) {
    return challenge();
  }

  let decoded;
  try {
    decoded = atob(auth.slice(6).trim());
  } catch (_) {
    return challenge("Invalid Authorization header");
  }

  // Basic header form: "username:password" — colon is the separator.
  // Password itself may contain colons; only split on the first one.
  const colonIdx = decoded.indexOf(":");
  const password = colonIdx === -1 ? "" : decoded.slice(colonIdx + 1);

  if (!safeEqual(password, PASSWORD)) {
    return challenge("Invalid credentials");
  }

  // Auth passed — let Netlify continue serving the actual page.
  return context.next();
};

export const config = {
  path: "/*",
};
