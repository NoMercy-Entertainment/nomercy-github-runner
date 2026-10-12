// Copyright 2026 NoMercy Entertainment. SPDX-License-Identifier: MIT
package report

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"

	runnerv1 "code.forgejo.org/forgejo/actions-proto/runner/v1"
)

// Local events carry only this allowlist. Task secrets, payloads and outputs
// never reach the spool; these observations do not grant forge authority.
type localJobEvent struct {
	EventID string `json:"event_id"`
	RunnerID string `json:"runner_id"`
	State string `json:"state"`
	Job string `json:"job"`
	Repo string `json:"repo"`
	Commit string `json:"commit"`
	RunID string `json:"run_id"`
	RunAttempt string `json:"run_attempt"`
	JobID string `json:"job_id"`
	At string `json:"at"`
	Result *string `json:"result"`
}

var runnerUUID = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)

func boundedJobText(value string, maximum int) string {
	value = strings.Map(func(r rune) rune { if r < 32 || r == 127 { return -1 }; return r }, value)
	runes := []rune(value)
	if len(runes) > maximum { runes = runes[:maximum] }
	return string(runes)
}

func localEventFor(task *runnerv1.Task) *localJobEvent {
	rid, directory := os.Getenv("NOMERCY_RUNNER_ID"), os.Getenv("NOMERCY_JOB_EVENT_DIR")
	if !runnerUUID.MatchString(rid) || !filepath.IsAbs(directory) { return nil }
	field := func(key string) string {
		if task.Context == nil { return "" }
		return boundedJobText(task.Context.Fields[key].GetStringValue(), 150)
	}
	return &localJobEvent{RunnerID: rid, JobID: strconv.FormatInt(task.Id, 10),
		Job: field("job"), Repo: field("repository"), Commit: field("sha"),
		RunID: field("run_id"), RunAttempt: field("run_attempt")}
}

func (event *localJobEvent) emit(state string, result *string) error {
	if event == nil { return nil }
	directory := os.Getenv("NOMERCY_JOB_EVENT_DIR")
	if !filepath.IsAbs(directory) { return errors.New("job event directory is not absolute") }
	if err := os.MkdirAll(directory, 0700); err != nil { return err }
	info, err := os.Lstat(directory)
	if err != nil { return err }
	if !info.IsDir() || info.Mode() & os.ModeSymlink != 0 { return errors.New("unsafe event spool") }
	// Bound disk use if the consumer is offline. Never overwrite an event.
	handle, err := os.Open(directory)
	if err != nil { return err }
	entries, err := handle.ReadDir(257)
	handle.Close()
	if err != nil && !errors.Is(err, io.EOF) { return err }
	if len(entries) >= 256 { return errors.New("job event spool is full") }
	var random [16]byte
	if _, err = rand.Read(random[:]); err != nil { return err }
	copy := *event
	copy.EventID = "forgejo:" + hex.EncodeToString(random[:])
	copy.State, copy.Result, copy.At = state, result, time.Now().UTC().Format(time.RFC3339Nano)
	body, err := json.Marshal(copy)
	if err != nil { return err }
	if len(body) > 4096 { return errors.New("oversized job event") }
	file, err := os.CreateTemp(directory, ".event-")
	if err != nil { return err }
	defer os.Remove(file.Name())
	if err = file.Chmod(0600); err == nil { _, err = file.Write(body) }
	if err == nil { err = file.Sync() }
	closeErr := file.Close()
	if err != nil { return err }
	if closeErr != nil { return closeErr }
	return os.Rename(file.Name(), filepath.Join(directory, hex.EncodeToString(random[:])+".json"))
}

func localResult(result runnerv1.Result) *string {
	value := map[runnerv1.Result]string{
		runnerv1.Result_RESULT_SUCCESS: "success", runnerv1.Result_RESULT_FAILURE: "failure",
		runnerv1.Result_RESULT_CANCELLED: "cancelled", runnerv1.Result_RESULT_SKIPPED: "skipped",
	}[result]
	if value == "" { return nil }
	return &value
}
