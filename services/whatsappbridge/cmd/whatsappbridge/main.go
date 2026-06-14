package main

import (
	"context"
	"io"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"go.bob.dev/whatsappbridge/internal/bridge"
	"go.bob.dev/whatsappbridge/internal/config"
	"go.bob.dev/whatsappbridge/internal/logging"
)

func main() {
	// Bootstrap .env files before structured logging is up; once the logger
	// exists we log which files were applied so the source of config is visible.
	envFiles, envErr := config.LoadEnvFiles()

	cfg, err := config.Load()
	if err != nil {
		slog.Error("failed to load config", "error", err)
		os.Exit(1)
	}

	level := parseLogLevel(cfg.LogLevel)
	fileWriter, err := logging.NewDailyFileWriter(cfg.LogDir, "whatsappbridge")
	if err != nil {
		slog.Error("failed to open daily log file", "error", err)
		os.Exit(1)
	}
	out := io.MultiWriter(os.Stdout, fileWriter)
	log := slog.New(slog.NewJSONHandler(out, &slog.HandlerOptions{Level: level}))

	if envErr != nil {
		log.Warn("failed to load one or more .env files", "error", envErr)
	}
	if len(envFiles) > 0 {
		log.Info("loaded env files", "paths", envFiles)
	}

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
