// Stub commands. Each one is implemented enough to be invokable
// (--help works, sensible default behavior or "not yet implemented"
// message) but the deeper logic lands in follow-up PRs.
//
// Real implementations live in separate files once we tackle them:
//   logs.go     — multiplexed colored log streaming via docker compose logs
//   status.go   — table view of service health, similar to 'docker ps'
//   update.go   — pull new image tags, snapshot, migrate, rollback on fail
//   oauth.go    — open browser, drive the broker flow, store token
//   uninstall.go — purge ~/.plynf, autostart units, /usr/local/bin/plynf

package cmd

import (
	"fmt"

	"github.com/spf13/cobra"
)

var logsCmd = &cobra.Command{
	Use:   "logs [service]",
	Short: "Stream logs from one or all services",
	Long: `Multiplexed colored log streaming. Without an argument, streams logs
from all 13 services. With a service name (e.g. 'workspace', 'gateway'), streams
only that one.

Backed by 'docker compose logs -f' in Docker mode, or tail-following the
embedded log file in Embedded mode.`,
	RunE: func(c *cobra.Command, args []string) error {
		return notYetImplemented("logs", "tracking issue: TODO link once we file it")
	},
}

var statusCmd = &cobra.Command{
	Use:   "status",
	Short: "Show service status table",
	RunE: func(c *cobra.Command, args []string) error {
		return notYetImplemented("status", "use 'plynf doctor' for now")
	},
}

var updateCmd = &cobra.Command{
	Use:   "update",
	Short: "Pull new image tags + run migrations + rollback on failure",
	Long: `Update Plynf to the latest released version. Steps performed:

  1. Resolve target version from https://plynf.com/manifest.json (cosign-verified)
  2. Snapshot current data dir (so we can roll back if something fails)
  3. Pull new images
  4. Run 'plynf migrate up'
  5. Restart services on new tag
  6. Verify health checks pass within 120s
  7. On failure: restore snapshot, restart on previous tag, exit 1
  8. On success: write new version to state, delete snapshot

Atomic. Either the update completes or nothing changes.`,
	RunE: func(c *cobra.Command, args []string) error {
		return notYetImplemented("update", "in progress on dist/block-e3-update")
	},
}

var oauthCmd = &cobra.Command{
	Use:   "oauth",
	Short: "Manage OAuth-tool connections",
}

var oauthConnectCmd = &cobra.Command{
	Use:   "connect <provider>",
	Short: "Open browser to authorize a tool provider",
	Long: `Drives an OAuth flow via the Plynf broker at oauth.plynf.com.

Supported providers (v1.6): github, linear, notion
Coming in v1.7:           slack, atlassian, asana
Coming in v2.0:           google, salesforce`,
	Args: cobra.ExactArgs(1),
	RunE: func(c *cobra.Command, args []string) error {
		return notYetImplemented(fmt.Sprintf("oauth connect %s", args[0]),
			"in progress on dist/block-e3-oauth-flow")
	},
}

var uninstallCmd = &cobra.Command{
	Use:   "uninstall",
	Short: "Remove Plynf from this machine",
	Long: `Stops services, removes containers + volumes, removes auto-start units,
and (with --purge) wipes ~/.plynf and the 'plynf' binary from /usr/local/bin.

Without --purge, your data is preserved.`,
	RunE: func(c *cobra.Command, args []string) error {
		return notYetImplemented("uninstall", "tracking: TODO link")
	},
}

func init() {
	oauthCmd.AddCommand(oauthConnectCmd)
	uninstallCmd.Flags().Bool("purge", false, "also wipe ~/.plynf and CLI binary (DESTRUCTIVE)")
}

// notYetImplemented gives a friendly message instead of a panic.
// Returns nil so the user doesn't get a spurious exit-1.
func notYetImplemented(name, hint string) error {
	fmt.Printf("\n  %s is not yet implemented.\n", name)
	if hint != "" {
		fmt.Printf("  %s\n\n", hint)
	}
	return nil
}
