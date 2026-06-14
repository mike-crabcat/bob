package config

import (
	"bufio"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

var envKeyPattern = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)

const envFileName = ".env"

// LoadEnvFiles loads .env files into the process environment without overriding
// values already present. Mirrors packages/bob-server/bob_server/config.py so
// the Go bridge and Python service read from the same sources.
//
// Precedence (first definition wins):
//  1. Existing process environment
//  2. $BOB_ENV_FILE
//  3. ./.env in the current working directory
//  4. $BOB_CONFIG_DIR/.env (default ~/config)
//  5. $WHATSAPPBRIDGE_DATA_DIR/.env (default ~/data/whatsappbridge)
//
// Returns the paths that were successfully loaded, in load order.
func LoadEnvFiles() ([]string, error) {
	var loaded []string

	candidates := []string{}
	if explicit := os.Getenv("BOB_ENV_FILE"); explicit != "" {
		candidates = append(candidates, explicit)
	}
	if cwd, err := os.Getwd(); err == nil {
		candidates = append(candidates, filepath.Join(cwd, envFileName))
	}
	configDir := envOrDefault("BOB_CONFIG_DIR", filepath.Join(homeDir(), "config"))
	candidates = append(candidates, filepath.Join(configDir, envFileName))
	dataDir := envOrDefault("WHATSAPPBRIDGE_DATA_DIR", filepath.Join(homeDir(), "data", "whatsappbridge"))
	candidates = append(candidates, filepath.Join(dataDir, envFileName))

	for _, path := range candidates {
		if path == "" {
			continue
		}
		info, err := os.Stat(path)
		if err != nil || info.IsDir() {
			continue
		}
		if err := loadEnvFile(path); err != nil {
			return loaded, err
		}
		loaded = append(loaded, path)
	}
	return loaded, nil
}

func loadEnvFile(path string) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()

	scanner := bufio.NewScanner(f)
	lineNum := 0
	for scanner.Scan() {
		lineNum++
		key, value, action, err := parseEnvLine(scanner.Text())
		if err != nil {
			return fmt.Errorf("%s:%d: %w", path, lineNum, err)
		}
		if action != actionSet {
			continue
		}
		if _, exists := os.LookupEnv(key); !exists {
			os.Setenv(key, value)
		}
	}
	return scanner.Err()
}

type envLineAction int

const (
	actionSkip envLineAction = iota
	actionSet
)

func parseEnvLine(line string) (key, value string, action envLineAction, err error) {
	stripped := strings.TrimSpace(line)
	if stripped == "" || strings.HasPrefix(stripped, "#") {
		return "", "", actionSkip, nil
	}
	if strings.HasPrefix(stripped, "export ") {
		stripped = strings.TrimSpace(strings.TrimPrefix(stripped, "export "))
	}
	eq := strings.Index(stripped, "=")
	if eq < 0 {
		return "", "", actionSkip, fmt.Errorf("missing '='")
	}
	key = strings.TrimSpace(stripped[:eq])
	if !envKeyPattern.MatchString(key) {
		return "", "", actionSkip, fmt.Errorf("invalid key %q", key)
	}
	raw := strings.TrimSpace(stripped[eq+1:])
	value, err = parseEnvValue(raw)
	if err != nil {
		return "", "", actionSkip, err
	}
	return key, value, actionSet, nil
}

func parseEnvValue(raw string) (string, error) {
	if raw == "" {
		return "", nil
	}
	switch {
	case raw[0] == '"':
		if len(raw) < 2 || raw[len(raw)-1] != '"' {
			return "", fmt.Errorf("unterminated double-quoted value")
		}
		return os.ExpandEnv(raw[1 : len(raw)-1]), nil
	case raw[0] == '\'':
		if len(raw) < 2 || raw[len(raw)-1] != '\'' {
			return "", fmt.Errorf("unterminated single-quoted value")
		}
		return raw[1 : len(raw)-1], nil
	default:
		if idx := strings.IndexAny(raw, " \t"); idx >= 0 {
			rest := strings.TrimSpace(raw[idx:])
			if strings.HasPrefix(rest, "#") {
				raw = strings.TrimSpace(raw[:idx])
			}
		}
		return os.ExpandEnv(raw), nil
	}
}

func homeDir() string {
	if h := os.Getenv("HOME"); h != "" {
		return h
	}
	if h, err := os.UserHomeDir(); err == nil {
		return h
	}
	return "."
}
