package main

import (
	"context"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"go.bob.dev/whatsappbridge/internal/bridge"
	"go.bob.dev/whatsappbridge/internal/config"
)

func main() {
	cfg, err := config.Load()
	if err != nil {
		slog.Error("failed to load config", "error", err)
		os.Exit(1)
	}

	level := parseLogLevel(cfg.LogLevel)
	log := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: level}))

	if err := cfg.EnsureDirs(); err != nil {
		log.Error("failed to create data directories", "error", err)
		os.Exit(1)
	}

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer cancel()

	b, err := bridge.New(cfg, log)
	if err != nil {
		log.Error("failed to create bridge", "error", err)
		os.Exit(1)
	}

	log.Info("starting whatsappbridge", "addr", cfg.ListenAddr())
	if err := b.Run(ctx); err != nil {
		log.Error("bridge exited with error", "error", err)
		os.Exit(1)
	}
}

func parseLogLevel(s string) slog.Level {
	switch s {
	case "debug":
		return slog.LevelDebug
	case "warn":
		return slog.LevelWarn
	case "error":
		return slog.LevelError
	default:
		return slog.LevelInfo
	}
}
