// Copyright 2026 NoMercy Entertainment. SPDX-License-Identifier: MIT
package run

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	runnerv1 "code.forgejo.org/forgejo/actions-proto/runner/v1"
	clientmocks "code.forgejo.org/forgejo/runner/v13/internal/pkg/client/mocks"
	"code.forgejo.org/forgejo/runner/v13/internal/pkg/config"
	"code.forgejo.org/forgejo/runner/v13/internal/pkg/report"
	"connectrpc.com/connect"
	"github.com/stretchr/testify/mock"
	"google.golang.org/protobuf/types/known/structpb"
)

const testAdmissionInstance = "https://forgejo.example.test"
const testAdmissionPolicy = `{"version":1,"instance":"https://forgejo.example.test","allowed_owner_ids":[1001],"trusted_source_owner_ids":[2001]}`

func TestAdmissionStartupProofExercisesCapturedOwnerAndForkPolicy(t *testing.T) {
	policy, err := loadAdmissionPolicy(testAdmissionPolicy, testAdmissionInstance)
	if err != nil {
		t.Fatal(err)
	}
	if err := admissionStartupProof(policy, testAdmissionPolicy); err != nil {
		t.Fatal(err)
	}
	if admissionStartupProof(nil, "") == nil || admissionStartupProof(&admissionPolicy{}, "") == nil {
		t.Fatal("missing protected policy must fail the startup proof")
	}
}

func testAdmissionRepo(id, owner float64, login string) map[string]any {
	return map[string]any{"id": id, "full_name": login + "/project", "owner": map[string]any{"id": owner, "login": login}}
}

func testAdmissionTask(t *testing.T, event string, base map[string]any, pr any) *runnerv1.Task {
	t.Helper()
	payload := map[string]any{"repository": base}
	if pr != nil {
		payload["pull_request"] = pr
	}
	fields, err := structpb.NewStruct(map[string]any{"event_name": event, "repository": base["full_name"],
		"repository_owner": base["owner"].(map[string]any)["login"], "actor": "trusted-actor", "event": payload})
	if err != nil {
		t.Fatal(err)
	}
	return &runnerv1.Task{Id: 42, Context: fields}
}

func testAdmissionPR(base map[string]any, head any) map[string]any {
	return map[string]any{"base": map[string]any{"repo": base}, "head": map[string]any{"repo": head}}
}

func TestAdmissionPolicyRequiresBoundedCompleteInstanceBoundDocument(t *testing.T) {
	bad := map[string]string{
		"missing": "", "oversized": strings.Repeat("x", admissionPolicyMaximum+1),
		"malformed": "{", "null": "null", "array": "[]",
		"unknown":   strings.Replace(testAdmissionPolicy, `"version":1`, `"version":1,"admin":true`, 1),
		"duplicate": strings.Replace(testAdmissionPolicy, `"version":1`, `"version":1,"version":1`, 1),
		"trailing":  testAdmissionPolicy + "{}", "unsupported": strings.Replace(testAdmissionPolicy, `"version":1`, `"version":2`, 1),
		"wrong_instance":    strings.Replace(testAdmissionPolicy, "forgejo.example.test", "other.example.test", 1),
		"http":              strings.Replace(testAdmissionPolicy, "https://", "http://", 1),
		"userinfo":          strings.Replace(testAdmissionPolicy, "https://", "https://actor@", 1),
		"query":             strings.Replace(testAdmissionPolicy, "forgejo.example.test", "forgejo.example.test?grant=all", 1),
		"path":              strings.Replace(testAdmissionPolicy, "forgejo.example.test", "forgejo.example.test/api", 1),
		"port":              strings.Replace(testAdmissionPolicy, "forgejo.example.test", "forgejo.example.test:444", 1),
		"zero":              strings.Replace(testAdmissionPolicy, "[1001]", "[0]", 1),
		"negative":          strings.Replace(testAdmissionPolicy, "[1001]", "[-1]", 1),
		"fractional":        strings.Replace(testAdmissionPolicy, "[1001]", "[1001.5]", 1),
		"inexact":           strings.Replace(testAdmissionPolicy, "[1001]", "[9007199254740992]", 1),
		"duplicate_id":      strings.Replace(testAdmissionPolicy, "[1001]", "[1001,1001]", 1),
		"empty_destination": strings.Replace(testAdmissionPolicy, "[1001]", "[]", 1),
		"null_source":       strings.Replace(testAdmissionPolicy, "[2001]", "null", 1),
		"missing_source":    strings.Replace(testAdmissionPolicy, `,"trusted_source_owner_ids":[2001]`, "", 1),
	}
	for name, value := range bad {
		t.Run(name, func(t *testing.T) {
			if policy, err := loadAdmissionPolicy(value, testAdmissionInstance); err == nil || policy != nil {
				t.Fatal("invalid policy was admitted")
			}
		})
	}
	for _, instance := range []string{testAdmissionInstance, "https://FORGEJO.EXAMPLE.TEST:443/"} {
		if _, err := loadAdmissionPolicy(testAdmissionPolicy, instance); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := loadAdmissionPolicy(strings.Replace(testAdmissionPolicy, "[2001]", "[]", 1), testAdmissionInstance); err != nil {
		t.Fatal("explicit no additional trusted source owners must remain valid")
	}
}

func TestAdmissionRepositoryAndPullRequestOwnerRules(t *testing.T) {
	policy, err := loadAdmissionPolicy(testAdmissionPolicy, testAdmissionInstance)
	if err != nil {
		t.Fatal(err)
	}
	base, own, trusted, outsider := testAdmissionRepo(11, 1001, "allowed"), testAdmissionRepo(12, 1001, "allowed"),
		testAdmissionRepo(13, 2001, "trusted"), testAdmissionRepo(14, 3001, "outside")
	tests := []struct {
		name, event string
		repo        map[string]any
		pr          any
		allowed     bool
	}{
		{"push", "push", base, nil, true}, {"dispatch", "workflow_dispatch", base, nil, true},
		{"schedule", "schedule", base, nil, true}, {"same_repo_pr", "pull_request", base, testAdmissionPR(base, base), true},
		{"same_owner_pr", "pull_request", base, testAdmissionPR(base, own), true},
		{"trusted_fork", "pull_request", base, testAdmissionPR(base, trusted), true},
		{"trusted_pr_target", "pull_request_target", base, testAdmissionPR(base, trusted), true},
		{"outside_fork", "pull_request", base, testAdmissionPR(base, outsider), false},
		{"outside_pr_target", "pull_request_target", base, testAdmissionPR(base, outsider), false},
		{"outside_review", "pull_request_review", base, testAdmissionPR(base, outsider), false},
		{"pr_object_other_event", "issue_comment", base, testAdmissionPR(base, outsider), false},
		{"untrusted_destination", "push", outsider, nil, false},
		{"source_trust_does_not_grant_destination", "push", trusted, nil, false},
		{"deleted_head", "pull_request", base, testAdmissionPR(base, nil), false},
		{"missing_head", "pull_request", base, map[string]any{"base": map[string]any{"repo": base}}, false},
		{"missing_pr", "pull_request", base, nil, false},
		{"string_pr", "pull_request", base, "unread", false},
		{"wrong_base", "pull_request", base, testAdmissionPR(outsider, trusted), false},
		{"missing_event", "", base, nil, false},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			observed := policy.allows(testAdmissionTask(t, test.event, test.repo, test.pr)) == nil
			if observed != test.allowed {
				t.Fatalf("allowed=%v, want %v", observed, test.allowed)
			}
		})
	}
}

func TestAdmissionMalformedTaskAndIdentityNeverGrantTrust(t *testing.T) {
	policy, err := loadAdmissionPolicy(testAdmissionPolicy, testAdmissionInstance)
	if err != nil {
		t.Fatal(err)
	}
	for _, task := range []*runnerv1.Task{nil, {Id: 42}, {Id: 42, Context: &structpb.Struct{}}, {Id: 0, Context: &structpb.Struct{}}} {
		if policy.allows(task) == nil {
			t.Fatal("missing context admitted")
		}
	}
	changes := map[string]func(*runnerv1.Task){
		"repository_name": func(task *runnerv1.Task) {
			task.Context.Fields["repository"] = structpb.NewStringValue("outside/project")
		},
		"repository_owner": func(task *runnerv1.Task) {
			task.Context.Fields["repository_owner"] = structpb.NewStringValue("outside")
		},
		"event_string": func(task *runnerv1.Task) { task.Context.Fields["event"] = structpb.NewStringValue("unread") },
		"owner_id_string": func(task *runnerv1.Task) {
			admissionField(task.Context.Fields, "event", "repository", "owner").GetStructValue().Fields["id"] = structpb.NewStringValue("1001")
		},
		"fractional_id": func(task *runnerv1.Task) {
			admissionField(task.Context.Fields, "event", "repository").GetStructValue().Fields["id"] = structpb.NewNumberValue(11.5)
		},
		"owner_login": func(task *runnerv1.Task) {
			admissionField(task.Context.Fields, "event", "repository", "owner").GetStructValue().Fields["login"] = structpb.NewStringValue("other")
		},
		"oversized_name": func(task *runnerv1.Task) {
			task.Context.Fields["repository"] = structpb.NewStringValue(strings.Repeat("x", 257))
		},
	}
	for name, change := range changes {
		t.Run(name, func(t *testing.T) {
			task := testAdmissionTask(t, "push", testAdmissionRepo(11, 1001, "allowed"), nil)
			change(task)
			if policy.allows(task) == nil {
				t.Fatal("inconsistent identity admitted")
			}
		})
	}
	outside := testAdmissionTask(t, "push", testAdmissionRepo(14, 3001, "outside"), nil)
	outside.Context.Fields["actor"] = structpb.NewStringValue("allowed")
	outside.Context.Fields[admissionPolicyEnvironment] = structpb.NewStringValue(testAdmissionPolicy)
	outside.Secrets = map[string]string{admissionPolicyEnvironment: testAdmissionPolicy, "ADMIN": "true"}
	outside.Vars = map[string]string{admissionPolicyEnvironment: testAdmissionPolicy, "ROLE": "admin"}
	if policy.allows(outside) == nil {
		t.Fatal("actor or task-provided policy granted owner trust")
	}
}

func TestAdmissionPRCommentMarkersRequireCompleteOrigin(t *testing.T) {
	policy, err := loadAdmissionPolicy(testAdmissionPolicy, testAdmissionInstance)
	if err != nil {
		t.Fatal(err)
	}
	base := testAdmissionRepo(11, 1001, "allowed")
	tests := []struct {
		name                    string
		isPull, issue           any
		marker, fullPR, allowed bool
	}{
		{"regular_issue", false, map[string]any{"number": 1}, true, false, true},
		{"forgejo_pr_flag", true, map[string]any{"number": 1}, true, false, false},
		{"pr_stub", nil, map[string]any{"pull_request": map[string]any{"url": "https://forgejo.example.test/pr/1"}}, false, false, false},
		{"empty_pr_stub", nil, map[string]any{"pull_request": map[string]any{}}, false, false, false},
		{"null_pr_stub", nil, map[string]any{"pull_request": nil}, false, false, false},
		{"string_pr_stub", nil, map[string]any{"pull_request": "unread"}, false, false, false},
		{"string_flag", "true", map[string]any{"number": 1}, true, false, false},
		{"null_flag", nil, map[string]any{"number": 1}, true, false, false},
		{"malformed_issue", false, "unread", true, false, false},
		{"resolved_trusted_pr", true, map[string]any{"pull_request": map[string]any{}}, true, true, true},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			var pr any
			if test.fullPR {
				pr = testAdmissionPR(base, testAdmissionRepo(13, 2001, "trusted"))
			}
			task := testAdmissionTask(t, "issue_comment", base, pr)
			event := task.Context.Fields["event"].GetStructValue()
			if test.marker {
				marker, err := structpb.NewValue(test.isPull)
				if err != nil {
					t.Fatal(err)
				}
				event.Fields["is_pull"] = marker
			}
			issue, err := structpb.NewValue(test.issue)
			if err != nil {
				t.Fatal(err)
			}
			event.Fields["issue"] = issue
			if (policy.allows(task) == nil) != test.allowed {
				t.Fatal("PR-linked comment bypassed complete origin proof")
			}
		})
	}
}

func TestAdmissionSnapshotIgnoresLaterEnvironmentAndFailsBeforeWorkflow(t *testing.T) {
	t.Setenv(admissionPolicyEnvironment, testAdmissionPolicy)
	cli := clientmocks.NewMockClient(t)
	cli.On("Address").Return(testAdmissionInstance)
	r := NewRunner(&config.Config{}, "test", nil, cli, nil)
	t.Setenv(admissionPolicyEnvironment, strings.ReplaceAll(testAdmissionPolicy, "1001", "3001"))
	out := testAdmissionTask(t, "push", testAdmissionRepo(14, 3001, "outside"), nil)
	out.WorkflowPayload = []byte("INVALID workflow which must never be parsed")
	if err := r.admit(out); err == nil {
		t.Fatal("job-time environment replaced constructor snapshot")
	}
	reporter := report.NewReporter(context.Background(), func() {}, cli, out, time.Hour, &config.Retry{MaxRetries: 1})
	if err := r.run(context.Background(), out, reporter); err == nil || !strings.Contains(err.Error(), "admission destination") {
		t.Fatal("denial did not precede parsing an invalid workflow")
	}
	if err := r.admission.allows(testAdmissionTask(t, "push", testAdmissionRepo(11, 1001, "allowed"), nil)); err != nil {
		t.Fatal(err)
	}
}

func TestAdmissionDeniedRunReportsFailureAndDoesNotExecuteAlways(t *testing.T) {
	for _, mode := range []string{"outside_owner", "nil_context", "missing_policy", "malformed_policy"} {
		t.Run(mode, func(t *testing.T) {
			spool := t.TempDir()
			t.Setenv("NOMERCY_RUNNER_ID", "11111111-1111-4111-8111-111111111111")
			t.Setenv("NOMERCY_JOB_EVENT_DIR", spool)
			policy, err := loadAdmissionPolicy(testAdmissionPolicy, testAdmissionInstance)
			if err != nil {
				t.Fatal(err)
			}
			cli := clientmocks.NewMockClient(t)
			var final *runnerv1.TaskState
			cli.On("UpdateLog", mock.Anything, mock.Anything).Return(func(_ context.Context, request *connect.Request[runnerv1.UpdateLogRequest]) (*connect.Response[runnerv1.UpdateLogResponse], error) {
				return connect.NewResponse(&runnerv1.UpdateLogResponse{AckIndex: request.Msg.Index + int64(len(request.Msg.Rows))}), nil
			})
			cli.On("UpdateTask", mock.Anything, mock.Anything).Return(func(_ context.Context, request *connect.Request[runnerv1.UpdateTaskRequest]) (*connect.Response[runnerv1.UpdateTaskResponse], error) {
				final = request.Msg.State
				return connect.NewResponse(&runnerv1.UpdateTaskResponse{}), nil
			})
			sentinel := filepath.Join(t.TempDir(), "always-ran")
			quoted, _ := json.Marshal(sentinel)
			task := testAdmissionTask(t, "push", testAdmissionRepo(14, 3001, "outside"), nil)
			task.WorkflowPayload = []byte("on: [push]\njobs:\n  deny:\n    runs-on: host\n    steps:\n      - if: always()\n        run: touch " + string(quoted) + "\n")
			if mode == "nil_context" {
				task.Context = nil
			}
			r := &Runner{admission: policy, client: cli, cfg: &config.Config{Runner: config.Runner{Timeout: time.Minute, ReportInterval: time.Hour, ReportRetry: config.Retry{MaxRetries: 1}}}}
			if mode == "missing_policy" || mode == "malformed_policy" {
				raw := ""
				if mode == "malformed_policy" {
					raw = "{"
				}
				r.admission, r.admissionErr = loadAdmissionPolicy(raw, testAdmissionInstance)
			}
			r.Run(context.Background(), task)
			if final == nil || final.Result != runnerv1.Result_RESULT_FAILURE || final.Id != 42 || len(final.Steps) != 0 {
				t.Fatal("denied task did not finish as failure before steps")
			}
			if _, err := os.Stat(sentinel); !os.IsNotExist(err) {
				t.Fatal("always step executed")
			}
			entries, err := os.ReadDir(spool)
			if err != nil || len(entries) != 2 {
				t.Fatal("native start/finish events were not preserved")
			}
			finished := false
			for _, entry := range entries {
				body, err := os.ReadFile(filepath.Join(spool, entry.Name()))
				if err != nil {
					t.Fatal(err)
				}
				var event map[string]any
				if json.Unmarshal(body, &event) != nil {
					t.Fatal("invalid native event")
				}
				if event["state"] == "finished" {
					finished = event["result"] == "failure" && event["job_id"] == "42"
				}
			}
			if !finished {
				t.Fatal("denial did not emit an exact native failure result")
			}
		})
	}
}
