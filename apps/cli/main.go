// plynf — the end-user CLI for running and managing a Plynf install.
//
// Two backends:
//   - Docker Compose (full 13-service stack)
//   - Embedded mode (single-binary runtime, no Docker)
//
// The CLI auto-detects which is appropriate or honors --embedded /
// --docker flags. Commands always have one job and exit cleanly.
//
// See `plynf --help` for the full surface.
package main

import (
	"fmt"
	"os"

	"github.com/plynf/plynf/apps/cli/cmd"
)

// Version is overwritten at build time via -ldflags.
// goreleaser sets it from the git tag.
var (
	Version   = "dev"
	GitCommit = "unknown"
	BuildDate = "unknown"
)

func main() {
	cmd.SetVersion(Version, GitCommit, BuildDate)
	if err := cmd.Execute(); err != nil {
		fmt.Fprintf(os.Stderr, "✘ %v\n", err)
		os.Exit(1)
	}
}
