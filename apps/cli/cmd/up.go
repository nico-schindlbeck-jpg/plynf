package cmd

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"time"

	"github.com/spf13/cobra"
)

var (
	upEmbedded bool
	upDocker   bool
	upDetach   bool
	upWait     time.Duration
)

var upCmd = &cobra.Command{
	Use:   "up",
	Short: "Start the Plynf runtime",
	Long: `Start the Plynf runtime in either Docker Compose or Embedded mode.

By default, detects which mode to use:
  - If 'docker' is in PATH and reachable: Docker Compose
  - Otherwise: Embedded (single binary)

Override with --docker or --embedded.`,
	RunE: runUp,
}

func init() {
	upCmd.Flags().BoolVar(&upEmbedded, "embedded", false, "force embedded mode (single binary, SQLite, no Docker)")
	upCmd.Flags().BoolVar(&upDocker,   "docker",   false, "force Docker Compose mode (13 services)")
	upCmd.Flags().BoolVar(&upDetach,   "detach",   true,  "run services in background")
	upCmd.Flags().DurationVar(&upWait, "wait",     120*time.Second, "max wait for services to become healthy")
}

func runUp(c *cobra.Command, args []string) error {
	ctx, cancel := context.WithTimeout(context.Background(), upWait+30*time.Second)
	defer cancel()

	mode := pickMode()
	fmt.Printf("→ Starting Plynf in %s mode\n", mode)

	switch mode {
	case "docker":
		return runDockerUp(ctx)
	case "embedded":
		return runEmbeddedUp(ctx)
	default:
		return fmt.Errorf("internal: pickMode returned %q", mode)
	}
}

func pickMode() string {
	if upEmbedded {
		return "embedded"
	}
	if upDocker {
		return "docker"
	}
	// Auto-detect: prefer Docker if reachable
	if _, err := exec.LookPath("docker"); err == nil {
		if exec.Command("docker", "version", "--format", "{{.Server.Version}}").Run() == nil {
			return "docker"
		}
	}
	return "embedded"
}

func runDockerUp(ctx context.Context) error {
	composeFile := findComposeFile()
	if composeFile == "" {
		return fmt.Errorf(`docker-compose.prod.yml not found.

  remediation: run 'plynf init' to fetch the production compose file,
               or 'cd' into a Plynf repo checkout first.
  see:         https://plynf.com/docs/install`)
	}

	args := []string{"compose", "-f", composeFile, "up"}
	if upDetach {
		args = append(args, "-d")
	}
	args = append(args, "--pull", "always", "--wait", "--wait-timeout",
		fmt.Sprintf("%d", int(upWait.Seconds())))

	cmd := exec.CommandContext(ctx, "docker", args...)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	cmd.Env = append(os.Environ(),
		"PLYNF_ORG=plynf",
		// PLYNF_VERSION is resolved from ~/.plynf/state.yaml or defaults to "latest"
	)
	if err := cmd.Run(); err != nil {
		return fmt.Errorf(`docker compose up failed: %w

  remediation: check 'plynf doctor' for diagnostic info, or
               'docker compose -f %s logs' for service logs`, err, composeFile)
	}

	fmt.Println("✓ Plynf is up. Dashboard: http://localhost:7424")
	return nil
}

func runEmbeddedUp(ctx context.Context) error {
	binPath, err := findEmbeddedBinary()
	if err != nil {
		return err
	}

	fmt.Printf("→ Spawning %s\n", binPath)
	if upDetach {
		// Detach: fork the embedded binary, write pid to ~/.plynf/embedded.pid
		// Simplified: launch as background process. Production version would
		// use launchd / systemd-user units, set up via 'plynf install --autostart'.
		c := exec.Command(binPath)
		c.Stdout = nil
		c.Stderr = nil
		if err := c.Start(); err != nil {
			return fmt.Errorf("failed to spawn embedded: %w", err)
		}
		pidPath := filepath.Join(plynfHome(), "embedded.pid")
		os.WriteFile(pidPath, []byte(fmt.Sprintf("%d\n", c.Process.Pid)), 0o644)
		fmt.Printf("✓ Embedded Plynf running. PID %d → %s\n", c.Process.Pid, pidPath)
		fmt.Println("  Dashboard: http://localhost:7420")
		return nil
	}

	c := exec.CommandContext(ctx, binPath)
	c.Stdout = os.Stdout
	c.Stderr = os.Stderr
	c.Stdin = os.Stdin
	return c.Run()
}

func findComposeFile() string {
	candidates := []string{
		"deploy/compose.prod.yml",
		filepath.Join(plynfHome(), "deploy", "compose.prod.yml"),
		filepath.Join(plynfHome(), "compose.prod.yml"),
	}
	for _, p := range candidates {
		if _, err := os.Stat(p); err == nil {
			return p
		}
	}
	return ""
}

func findEmbeddedBinary() (string, error) {
	candidates := []string{
		filepath.Join(plynfHome(), "bin", "plynf-embedded"),
		"/usr/local/bin/plynf-embedded",
	}
	if p, err := exec.LookPath("plynf-embedded"); err == nil {
		candidates = append([]string{p}, candidates...)
	}
	for _, p := range candidates {
		if _, err := os.Stat(p); err == nil {
			return p, nil
		}
	}
	return "", fmt.Errorf(`plynf-embedded binary not found.

  remediation: download from https://github.com/plynf/plynf/releases
               (look for 'plynf-embedded-<os>-<arch>') and place it at
               ~/.plynf/bin/plynf-embedded`)
}

func plynfHome() string {
	if env := os.Getenv("PLYNF_HOME"); env != "" {
		return env
	}
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".plynf")
}
