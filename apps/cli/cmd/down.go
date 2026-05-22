package cmd

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"

	"github.com/spf13/cobra"
)

var downVolumes bool

var downCmd = &cobra.Command{
	Use:   "down",
	Short: "Stop the Plynf runtime",
	Long: `Stop a running Plynf instance — Docker Compose or Embedded.

By default keeps the data volume so 'plynf up' picks up where you
left off. Use --volumes (or -v) to wipe state too.`,
	RunE: runDown,
}

func init() {
	downCmd.Flags().BoolVarP(&downVolumes, "volumes", "v", false, "also remove data volumes (DESTRUCTIVE)")
}

func runDown(c *cobra.Command, args []string) error {
	// Try docker first if compose file is present
	composeFile := findComposeFile()
	if composeFile != "" {
		if isDockerRunning() {
			ctx := context.Background()
			args := []string{"compose", "-f", composeFile, "down"}
			if downVolumes {
				args = append(args, "-v")
			}
			cmd := exec.CommandContext(ctx, "docker", args...)
			cmd.Stdout = os.Stdout
			cmd.Stderr = os.Stderr
			if err := cmd.Run(); err != nil {
				return fmt.Errorf("docker compose down: %w", err)
			}
			fmt.Println("✓ Docker stack stopped")
		}
	}

	// Also stop any embedded pid we tracked
	pidPath := filepath.Join(plynfHome(), "embedded.pid")
	if data, err := os.ReadFile(pidPath); err == nil {
		pidStr := strings.TrimSpace(string(data))
		if pid, perr := strconv.Atoi(pidStr); perr == nil {
			if proc, ferr := os.FindProcess(pid); ferr == nil {
				if sigErr := proc.Signal(syscall.SIGTERM); sigErr == nil {
					fmt.Printf("✓ Sent SIGTERM to embedded pid %d\n", pid)
				}
			}
		}
		os.Remove(pidPath)
	}

	return nil
}

func isDockerRunning() bool {
	if _, err := exec.LookPath("docker"); err != nil {
		return false
	}
	return exec.Command("docker", "version", "--format", "{{.Server.Version}}").Run() == nil
}
