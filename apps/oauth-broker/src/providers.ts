// Provider-specific OAuth registration data.
//
// Adding a new provider:
//   1. Register an OAuth app at the provider's developer portal
//   2. Add its client_id to wrangler.toml [vars]
//   3. (Optional) Add its client_secret as a wrangler secret
//   4. Add an entry below
//   5. Document available scopes for that provider in the README

export type Provider = {
  id: "github" | "linear" | "notion";
  /** Public OAuth name shown to the user during consent. */
  displayName: string;
  /** Provider's authorize URL (where the browser is redirected first). */
  authorizeUrl: string;
  /** Provider's token-exchange endpoint. */
  tokenUrl: string;
  /** Whether to use PKCE (S256). Required by best practice; not all providers support it yet. */
  usePkce: boolean;
  /** Whether the provider requires a client secret in token exchange. */
  confidential: boolean;
  /** Default scope string when caller doesn't specify. */
  defaultScopes: string;
};

export const providers: Record<string, Provider> = {
  github: {
    id: "github",
    displayName: "GitHub",
    authorizeUrl: "https://github.com/login/oauth/authorize",
    tokenUrl: "https://github.com/login/oauth/access_token",
    usePkce: true,
    confidential: false,        // public client (PKCE-only flow available since 2024)
    defaultScopes: "read:user repo",
  },
  linear: {
    id: "linear",
    displayName: "Linear",
    authorizeUrl: "https://linear.app/oauth/authorize",
    tokenUrl: "https://api.linear.app/oauth/token",
    usePkce: true,
    confidential: false,
    defaultScopes: "read write",
  },
  notion: {
    id: "notion",
    displayName: "Notion",
    authorizeUrl: "https://api.notion.com/v1/oauth/authorize",
    tokenUrl: "https://api.notion.com/v1/oauth/token",
    usePkce: false,             // Notion does not support PKCE yet — confidential only
    confidential: true,
    defaultScopes: "",
  },
};
