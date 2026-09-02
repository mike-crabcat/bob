package logging

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sync"
	"time"
)

var dateSourceRE = regexp.MustCompile(`^(\d{4}-\d{2}-\d{2})_(.+)\.log$`)

// DailyFileWriter appends each Write to {dir}/{YYYY-MM-DD}_{source}.log.
// When the local date changes it closes the current file and moves it into
// {dir}/older/. Any stale files from earlier days are also archived when the
// writer is constructed so a long-running restart does not leave old files
// in the root.
type DailyFileWriter struct {
	dir    string
	source string

	mu           sync.Mutex
	currentDate  string
	currentPath  string
	currentFile  *os.File
}

func NewDailyFileWriter(dir, source string) (*DailyFileWriter, error) {
	for _, d := range []string{dir, filepath.Join(dir, "older")} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			return nil, fmt.Errorf("create log dir %s: %w", d, err)
		}
	}
	w := &DailyFileWriter{dir: dir, source: source}
	today := w.today()
	if err := w.archivePrevious(today); err != nil {
		return nil, err
	}
	w.currentDate = today
	w.currentPath = w.pathFor(today)
	f, err := os.OpenFile(w.currentPath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return nil, fmt.Errorf("open log file %s: %w", w.currentPath, err)
	}
	w.currentFile = f
	return w, nil
}

func (w *DailyFileWriter) Write(p []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()

	today := w.today()
	if today != w.currentDate {
		if err := w.rotateLocked(today); err != nil {
			return 0, err
		}
	}
	return w.currentFile.Write(p)
}

func (w *DailyFileWriter) Close() error {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.currentFile != nil {
		return w.currentFile.Close()
	}
	return nil
}

func (w *DailyFileWriter) today() string {
	return time.Now().Format("2006-01-02")
}

func (w *DailyFileWriter) pathFor(date string) string {
	return filepath.Join(w.dir, fmt.Sprintf("%s_%s.log", date, w.source))
}

func (w *DailyFileWriter) rotateLocked(today string) error {
	if w.currentFile != nil {
		_ = w.currentFile.Close()
	}
	olderDir := filepath.Join(w.dir, "older")
	if err := os.MkdirAll(olderDir, 0o755); err != nil {
		return err
	}
	target := filepath.Join(olderDir, filepath.Base(w.currentPath))
	if err := os.Rename(w.currentPath, target); err != nil && !os.IsNotExist(err) {
		return err
	}
	w.currentDate = today
	w.currentPath = w.pathFor(today)
	f, err := os.OpenFile(w.currentPath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	w.currentFile = f
	return nil
}

func (w *DailyFileWriter) archivePrevious(today string) error {
	entries, err := os.ReadDir(w.dir)
	if err != nil {
		return err
	}
	olderDir := filepath.Join(w.dir, "older")
	for _, entry := range entries {
		if entry.IsDir() {
			continue
		}
		m := dateSourceRE.FindStringSubmatch(entry.Name())
		if m == nil || m[2] != w.source {
			continue
		}
		if m[1] == today {
			continue
		}
		src := filepath.Join(w.dir, entry.Name())
		dst := filepath.Join(olderDir, entry.Name())
		if err := os.Rename(src, dst); err != nil {
			return err
		}
	}
	return nil
}
