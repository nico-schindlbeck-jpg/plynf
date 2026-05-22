package cmd

import (
	"fmt"
	"os"
	"path/filepath"

	"github.com/spf13/cobra"
)

var (
	cfgFile string

	// Build-time injected. main.go calls SetVersion before Execute.
	version   = "dev"
	gitCommit = "unknown"
	buildDate = "unknown"
)

// SetVersion is called from main.go with linker-injected values.
func SetVersion(v, c, d string) {
	version = v
	gitCommit = c
	buildDate = d
}

var rootCmd = &cobra.Command{
	Use:   "plynf",
	Short: "Plynf — the runtime where production AI agents actually work",
	Long: `plynf — command-line interface for the Plynf agent runtime.

Use 'plynf up' to start the stack, 'plynf doctor' to diagnose issues,
'plynf oauth connect <provider>' to wire a tool. See 'plynf <cmd> --help'
for details on any subcommand.`,
	Version: "",  // populated in Execute() so build-time vars are visible
	SilenceUsage:  true,  // don't show usage on every error — too noisy
	SilenceErrors: true,  // main.go formats errors itself
}

// Execute runs the CLI.
func Execute() error {
	rootCmd.Version = fmt.Sprintf("%s (commit %s, built %s)", version, gitCommit, buildDate)
	return rootCmd.Execute()
}

func init() {
	// Resolve default config path
	home, _ := os.UserHomeDir()
	defaultCfg := filepath.Join(home, ".plynf", "config.yaml")

	rootCmd.PersistentFlags().StringVar(&cfgFile, "config", defaultCfg, "config file path")
	rootCmd.PersistentFlags().Bool("debug", false, "enable debug logging")
	rootCmd.PersistentFlags().Bool("no-color", false, "disable colored output")

	// Register subcommands. Each lives in its own file under cmd/.
	rootCmd.AddCommand(upCmd)
	rootCmd.AddCommand(downCmd)
	rootCmd.AddCommand(statusCmd)
	rootCmd.AddCommand(logsCmd)
	rootCmd.AddCommand(doctorCmd)
	rootCmd.AddCommand(updateCmd)
	rootCmd.AddCommand(oauthCmd)
	rootCmd.AddCommand(uninstallCmd)
}
