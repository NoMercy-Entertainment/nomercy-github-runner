package report

import (
	"encoding/json"
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	runnerv1 "code.forgejo.org/forgejo/actions-proto/runner/v1"
	"connectrpc.com/connect"
	"github.com/stretchr/testify/mock"
	"google.golang.org/protobuf/types/known/structpb"
)

func TestLocalJobEventAllowlistAndResult(t *testing.T) {
	directory := t.TempDir()
	t.Setenv("NOMERCY_RUNNER_ID", "11111111-1111-4111-8111-111111111111")
	t.Setenv("NOMERCY_JOB_EVENT_DIR", directory)
	context, _ := structpb.NewStruct(map[string]any{"job": "test\njob", "repository": "owner/repo", "sha": "abc",
		"run_id": "123", "run_attempt": "2", "token": "MUST-NOT-LEAK", "event": "MUST-NOT-LEAK"})
	event := localEventFor(&runnerv1.Task{Id: 88, Context: context, Secrets: map[string]string{"key": "MUST-NOT-LEAK"}})
	if err := event.emit("started", nil); err != nil { t.Fatal(err) }
	if err := event.emit("finished", localResult(runnerv1.Result_RESULT_FAILURE)); err != nil { t.Fatal(err) }
	files, _ := os.ReadDir(directory)
	if len(files) != 2 { t.Fatalf("expected two immutable events, got %d", len(files)) }
	states := map[string]bool{}
	for _, file := range files {
		body, _ := os.ReadFile(filepath.Join(directory, file.Name()))
		if strings.Contains(string(body), "MUST-NOT-LEAK") { t.Fatal("secret escaped allowlist") }
		var got localJobEvent
		if err := json.Unmarshal(body, &got); err != nil { t.Fatal(err) }
		states[got.State] = true
		if got.Job != "testjob" || got.JobID != "88" || got.RunAttempt != "2" { t.Fatal("incorrect identity") }
		if got.State == "finished" && (got.Result == nil || *got.Result != "failure") { t.Fatal("result was guessed") }
		if !strings.HasSuffix(file.Name(), ".json") { t.Fatal("partial event was exposed") }
	}
	if !states["started"] || !states["finished"] { t.Fatal("missing lifecycle transition") }
}

func TestLocalJobEventsDisabledAndBounded(t *testing.T) {
	t.Setenv("NOMERCY_RUNNER_ID", "invalid")
	t.Setenv("NOMERCY_JOB_EVENT_DIR", t.TempDir())
	if localEventFor(&runnerv1.Task{}) != nil { t.Fatal("unowned producer enabled") }
	if got := boundedJobText(strings.Repeat("界", 200), 150); len([]rune(got)) != 150 { t.Fatal("unbounded unicode") }
	for result, want := range map[runnerv1.Result]string{runnerv1.Result_RESULT_SUCCESS: "success",
		runnerv1.Result_RESULT_FAILURE: "failure", runnerv1.Result_RESULT_CANCELLED: "cancelled", runnerv1.Result_RESULT_SKIPPED: "skipped"} {
		if got := localResult(result); got == nil || *got != want { t.Fatal("result mismatch") }
	}
	if localResult(runnerv1.Result_RESULT_UNSPECIFIED) != nil { t.Fatal("unspecified became success") }
}

func TestCompletionEventPrecedesUpstreamUpload(t *testing.T) {
	directory := t.TempDir()
	t.Setenv("NOMERCY_RUNNER_ID", "11111111-1111-4111-8111-111111111111")
	t.Setenv("NOMERCY_JOB_EVENT_DIR", directory)
	reporter, client, _ := mockReporter(t)
	client.On("UpdateLog", mock.Anything, mock.Anything).Return(func(_ context.Context, request *connect.Request[runnerv1.UpdateLogRequest]) (*connect.Response[runnerv1.UpdateLogResponse], error) {
		files, _ := os.ReadDir(directory)
		found := false
		for _, file := range files {
			body, _ := os.ReadFile(filepath.Join(directory, file.Name()))
			var event localJobEvent
			if err := json.Unmarshal(body, &event); err != nil { t.Fatal(err) }
			if event.State == "finished" {
				found = event.Result != nil && *event.Result == "failure"
			}
			if strings.Contains(string(body), "private error detail") { t.Fatal("execution error escaped event allowlist") }
		}
		if !found { t.Fatal("finish observation was delayed until after forge upload") }
		return connect.NewResponse(&runnerv1.UpdateLogResponse{AckIndex: request.Msg.Index + 1}), nil
	})
	client.On("UpdateTask", mock.Anything, mock.Anything).Return(connect.NewResponse(&runnerv1.UpdateTaskResponse{}), nil)
	if err := reporter.Close(errors.New("private error detail")); err != nil { t.Fatal(err) }
}
