package config

import (
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"time"
)

type Config struct {
	Host    string
	Port    int
	Token   string
	DataDir string
	DevDir  string // BOB_CONFIG_DIR for reading shared .env

	LogLevel string

	IncomingQueueTTL   time.Duration
	OutgoingMaxRetries int
	OutgoingRetryDelay time.Duration

	StatusInterval time.Duration
}

func Load() (*Config, error) {
	cfg := &Config{
		Host:    envOrDefault("WHATSAPPBRIDGE_HOST", "127.0.0.1"),
		Port:    envInt("WHATSAPPBRIDGE_PORT", 8430),
		Token:   os.Getenv("WHATSAPPBRIDGE_TOKEN"),
		DataDir: envOrDefault("WHATSAPPBRIDGE_DATA_DIR", filepath.Join(os.Getenv("HOME"), "data", "whatsappbridge")),
		DevDir:  envOrDefault("BOB_CONFIG_DIR", filepath.Join(os.Getenv("HOME"), "config")),

		LogLevel: envOrDefault("WHATSAPPBRIDGE_LOG_LEVEL", "info"),

		IncomingQueueTTL:   envDuration("WHATSAPPBRIDGE_INCOMING_QUEUE_TTL", 24*time.Hour),
		OutgoingMaxRetries: envInt("WHATSAPPBRIDGE_OUTGOING_MAX_RETRIES", 5),
		OutgoingRetryDelay: envDuration("WHATSAPPBRIDGE_OUTGOING_RETRY_DELAY", 30*time.Second),

		StatusInterval: envDuration("WHATSAPPBRIDGE_STATUS_INTERVAL", 60*time.Second),
	}

	if cfg.Token == "" {
		return nil, fmt.Errorf("WHATSAPPBRIDGE_TOKEN is required")
	}

	return cfg, nil
}

func (c *Config) ListenAddr() string {
	return fmt.Sprintf("%s:%d", c.Host, c.Port)
}

func (c *Config) SessionDBPath() string {
	return filepath.Join(c.DataDir, "whatsmeow.db")
}

func (c *Config) QueueDBPath() string {
	return filepath.Join(c.DataDir, "bridge_queue.db")
}

func (c *Config) EnsureDirs() error {
	return os.MkdirAll(c.DataDir, 0755)
}

func envOrDefault(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func envInt(key string, fallback int) int {
	v := os.Getenv(key)
	if v == "" {
		return fallback
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return fallback
	}
	return n
}

func envDuration(key string, fallback time.Duration) time.Duration {
	v := os.Getenv(key)
	if v == "" {
		return fallback
	}
	d, err := time.ParseDuration(v)
	if err != nil {
		return fallback
	}
	return d
}
