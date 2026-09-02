package config

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

func writeEnvFile(t *testing.T, dir, name, content string) string {
	t.Helper()
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, []byte(content), 0600); err != nil {
		t.Fatalf("write %s: %v", path, err)
	}
	return path
}

func TestParseEnvLine(t *testing.T) {
	cases := []struct {
		name   string
		line   string
		key    string
		value  string
		action envLineAction
	}{
		{"blank", "", "", "", actionSkip},
		{"comment", "# hello", "", "", actionSkip},
		{"plain", "FOO=bar", "FOO", "bar", actionSet},
		{"export prefix", "export FOO=bar", "FOO", "bar", actionSet},
		{"single quote", "FOO='hello world'", "FOO", "hello world", actionSet},
		{"double quote", `FOO="hello world"`, "FOO", "hello world", actionSet},
		{"unquoted trailing comment", "FOO=bar # note", "FOO", "bar", actionSet},
		{"empty value", "FOO=", "FOO", "", actionSet},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			key, value, action, err := parseEnvLine(tc.line)
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if action != tc.action {
				t.Fatalf("action: got %v want %v", action, tc.action)
			}
			if action == actionSet && (key != tc.key || value != tc.value) {
				t.Fatalf("got (%q, %q) want (%q, %q)", key, value, tc.key, tc.value)
			}
		})
	}
}

func TestParseEnvLineErrors(t *testing.T) {
	cases := []struct {
		name string
		line string
	}{
		{"invalid key", "1FOO=bar"},
		{"bad quote", `FOO="unterminated`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, _, _, err := parseEnvLine(tc.line)
			if err == nil {
				t.Fatalf("expected error for %q", tc.line)
			}
		})
	}
}

func TestLoadEnvFilesDoesNotOverrideExisting(t *testing.T) {
	t.Setenv("EXISTING_VAR", "from-process")
	dir := t.TempDir()
	writeEnvFile(t, dir, ".env", "EXISTING_VAR=from-file\nNEW_VAR=from-file\n")

	t.Setenv("WHATSAPPBRIDGE_DATA_DIR", dir)
	t.Setenv("BOB_ENV_FILE", filepath.Join(dir, ".env"))
	// Ensure cwd/config-dir candidates don't accidentally exist.
	t.Setenv("BOB_CONFIG_DIR", t.TempDir())

	if _, err := LoadEnvFiles(); err != nil {
		t.Fatalf("LoadEnvFiles: %v", err)
	}
	if got := os.Getenv("EXISTING_VAR"); got != "from-process" {
		t.Fatalf("EXISTING_VAR overridden: got %q want %q", got, "from-process")
	}
	if got := os.Getenv("NEW_VAR"); got != "from-file" {
		t.Fatalf("NEW_VAR not loaded: got %q want %q", got, "from-file")
	}
}

func TestLoadEnvFilesPrecedence(t *testing.T) {
	high := t.TempDir()
	low := t.TempDir()
	writeEnvFile(t, high, ".env", "SHARED=high\nHIGH_ONLY=high\n")
	writeEnvFile(t, low, ".env", "SHARED=low\nLOW_ONLY=low\n")

	t.Setenv("BOB_ENV_FILE", filepath.Join(high, ".env"))
	t.Setenv("BOB_CONFIG_DIR", low)
	t.Setenv("WHATSAPPBRIDGE_DATA_DIR", t.TempDir())
	cwd := t.TempDir()
	if err := os.Chdir(cwd); err != nil {
		t.Fatalf("chdir: %v", err)
	}

	loaded, err := LoadEnvFiles()
	if err != nil {
		t.Fatalf("LoadEnvFiles: %v", err)
	}

	wantLoaded := []string{
		filepath.Join(high, ".env"),
		filepath.Join(low, ".env"),
	}
	if !reflect.DeepEqual(loaded, wantLoaded) {
		t.Fatalf("loaded: got %v want %v", loaded, wantLoaded)
	}
	if got := os.Getenv("SHARED"); got != "high" {
		t.Fatalf("SHARED: got %q want %q (BOB_ENV_FILE should win)", got, "high")
	}
	if got := os.Getenv("HIGH_ONLY"); got != "high" {
		t.Fatalf("HIGH_ONLY: got %q", got)
	}
	if got := os.Getenv("LOW_ONLY"); got != "low" {
		t.Fatalf("LOW_ONLY: got %q", got)
	}
}
