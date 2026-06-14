package logging

import (
	"os"
	"path/filepath"
	"testing"
)

func TestDailyFileWriter_AppendsAndArchives(t *testing.T) {
	tmp := t.TempDir()

	// Seed a stale file from "yesterday" that should be archived on construction.
	stale := filepath.Join(tmp, "2020-01-01_whatsappbridge.log")
	if err := os.WriteFile(stale, []byte("old\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	w, err := NewDailyFileWriter(tmp, "whatsappbridge")
	if err != nil {
		t.Fatalf("NewDailyFileWriter: %v", err)
	}
	defer w.Close()

	if _, err := w.Write([]byte(`{"msg":"hello"}` + "\n")); err != nil {
		t.Fatalf("Write: %v", err)
	}

	// Stale file should now be under older/.
	if _, err := os.Stat(filepath.Join(tmp, "older", "2020-01-01_whatsappbridge.log")); err != nil {
		t.Errorf("stale file not archived: %v", err)
	}

	// Today's file should exist in the root with our line.
	entries, err := os.ReadDir(tmp)
	if err != nil {
		t.Fatal(err)
	}
	found := false
	for _, e := range entries {
		if e.Name() != "older" && !e.IsDir() {
			body, _ := os.ReadFile(filepath.Join(tmp, e.Name()))
			if string(body) == `{"msg":"hello"}`+"\n" {
				found = true
				break
			}
		}
	}
	if !found {
		t.Errorf("today's log entry not found in %v", entries)
	}
}
