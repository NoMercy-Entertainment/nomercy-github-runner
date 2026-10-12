// Copyright 2026 NoMercy Entertainment. SPDX-License-Identifier: MIT
package run

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"math"
	"net/url"
	"strings"

	runnerv1 "code.forgejo.org/forgejo/actions-proto/runner/v1"
	"google.golang.org/protobuf/types/known/structpb"
)

// Only the protected daemon environment supplies this snapshot. Task Context,
// Secrets, Vars and workflow environment values are never policy authorities.
const admissionPolicyEnvironment = "NOMERCY_FORGEJO_ADMISSION_POLICY"
const admissionPolicyMaximum = 16 * 1024
const admissionIDMaximum = 1<<53 - 1 // Struct protobuf numbers must remain exact.

type admissionDocument struct {
	Version        int      `json:"version"`
	Instance       string   `json:"instance"`
	AllowedOwners  []uint64 `json:"allowed_owner_ids"`
	TrustedSources []uint64 `json:"trusted_source_owner_ids"`
}

type admissionPolicy struct {
	owners  map[uint64]bool
	sources map[uint64]bool
}

func admissionInstance(value string) (string, error) {
	if value == "" || len(value) > 512 || strings.TrimSpace(value) != value {
		return "", errors.New("admission instance is invalid")
	}
	parsed, err := url.Parse(value)
	if err != nil || parsed.Scheme != "https" || parsed.Hostname() == "" || parsed.User != nil ||
		parsed.RawQuery != "" || parsed.Fragment != "" || parsed.Opaque != "" || parsed.RawPath != "" ||
		(parsed.Path != "" && parsed.Path != "/") || (parsed.Port() != "" && parsed.Port() != "443") {
		return "", errors.New("admission instance must be a configured HTTPS origin")
	}
	return "https://" + strings.ToLower(parsed.Hostname()), nil
}

func admissionOwners(ids []uint64, emptyAllowed bool) (map[uint64]bool, error) {
	if ids == nil || len(ids) > 100 || (!emptyAllowed && len(ids) == 0) {
		return nil, errors.New("admission owner IDs are missing or outside their bound")
	}
	owners := make(map[uint64]bool, len(ids))
	for _, id := range ids {
		if id == 0 || id > admissionIDMaximum || owners[id] {
			return nil, errors.New("admission owner IDs must be positive, exact and unique")
		}
		owners[id] = true
	}
	return owners, nil
}

func loadAdmissionPolicy(raw, configuredInstance string) (*admissionPolicy, error) {
	if raw == "" || len(raw) > admissionPolicyMaximum {
		return nil, errors.New("protected admission policy is unavailable or oversized")
	}
	decoder := json.NewDecoder(bytes.NewBufferString(raw))
	opening, err := decoder.Token()
	if err != nil || opening != json.Delim('{') {
		return nil, errors.New("protected admission policy is malformed")
	}
	fields := make(map[string]json.RawMessage, 4)
	for decoder.More() {
		key, err := decoder.Token()
		name, valid := key.(string)
		if err != nil || !valid || fields[name] != nil ||
			(name != "version" && name != "instance" && name != "allowed_owner_ids" && name != "trusted_source_owner_ids") {
			return nil, errors.New("protected admission policy has duplicate or unknown fields")
		}
		var value json.RawMessage
		if err := decoder.Decode(&value); err != nil {
			return nil, errors.New("protected admission policy is malformed")
		}
		fields[name] = value
	}
	closing, err := decoder.Token()
	if err != nil || closing != json.Delim('}') || len(fields) != 4 {
		return nil, errors.New("protected admission policy fields are missing")
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		return nil, errors.New("protected admission policy has trailing content")
	}
	encoded, _ := json.Marshal(fields)
	var document admissionDocument
	if err := json.Unmarshal(encoded, &document); err != nil {
		return nil, errors.New("protected admission policy is malformed")
	}
	if document.Version != 1 {
		return nil, errors.New("protected admission policy version is invalid")
	}
	instance, err := admissionInstance(document.Instance)
	if err != nil {
		return nil, err
	}
	configured, err := admissionInstance(configuredInstance)
	if err != nil || instance != configured {
		return nil, errors.New("protected admission policy names another Forgejo instance")
	}
	owners, err := admissionOwners(document.AllowedOwners, false)
	if err != nil {
		return nil, err
	}
	sources, err := admissionOwners(document.TrustedSources, true)
	if err != nil {
		return nil, err
	}
	return &admissionPolicy{owners: owners, sources: sources}, nil
}

func admissionField(fields map[string]*structpb.Value, path ...string) *structpb.Value {
	var value *structpb.Value
	for index, key := range path {
		value = fields[key]
		if value == nil {
			return nil
		}
		if index != len(path)-1 {
			object, ok := value.Kind.(*structpb.Value_StructValue)
			if !ok || object.StructValue == nil {
				return nil
			}
			fields = object.StructValue.Fields
		}
	}
	return value
}

func admissionText(value *structpb.Value, maximum int) string {
	if value == nil {
		return ""
	}
	text, ok := value.Kind.(*structpb.Value_StringValue)
	if !ok || text.StringValue == "" || len(text.StringValue) > maximum ||
		strings.TrimSpace(text.StringValue) != text.StringValue || strings.IndexFunc(text.StringValue, func(r rune) bool { return r < 32 || r == 127 }) >= 0 {
		return ""
	}
	return text.StringValue
}

func admissionID(value *structpb.Value) uint64 {
	if value == nil {
		return 0
	}
	number, ok := value.Kind.(*structpb.Value_NumberValue)
	if !ok || math.IsNaN(number.NumberValue) || math.IsInf(number.NumberValue, 0) ||
		number.NumberValue < 1 || number.NumberValue > admissionIDMaximum || math.Trunc(number.NumberValue) != number.NumberValue {
		return 0
	}
	return uint64(number.NumberValue)
}

type admissionRepository struct {
	id, owner   uint64
	name, login string
}

func admissionRepo(value *structpb.Value) (admissionRepository, bool) {
	if value == nil {
		return admissionRepository{}, false
	}
	object, ok := value.Kind.(*structpb.Value_StructValue)
	if !ok || object.StructValue == nil {
		return admissionRepository{}, false
	}
	fields := object.StructValue.Fields
	repo := admissionRepository{id: admissionID(fields["id"]), owner: admissionID(admissionField(fields, "owner", "id")),
		name: admissionText(fields["full_name"], 256), login: admissionText(admissionField(fields, "owner", "login"), 100)}
	parts := strings.Split(repo.name, "/")
	return repo, repo.id != 0 && repo.owner != 0 && repo.login != "" && len(parts) == 2 &&
		parts[1] != "" && strings.EqualFold(parts[0], repo.login)
}

func admissionPRLinked(fields map[string]*structpb.Value) (bool, error) {
	linked := false
	if marker := admissionField(fields, "event", "is_pull"); marker != nil {
		flag, valid := marker.Kind.(*structpb.Value_BoolValue)
		if !valid {
			return false, errors.New("admission pull request marker is malformed")
		}
		linked = flag.BoolValue
	}
	if issue := admissionField(fields, "event", "issue"); issue != nil {
		object, valid := issue.Kind.(*structpb.Value_StructValue)
		if !valid || object.StructValue == nil {
			return false, errors.New("admission issue metadata is malformed")
		}
		if marker := object.StructValue.Fields["pull_request"]; marker != nil {
			stub, valid := marker.Kind.(*structpb.Value_StructValue)
			if !valid || stub.StructValue == nil {
				return false, errors.New("admission pull request issue marker is malformed")
			}
			linked = true
		}
	}
	return linked, nil
}

func (policy *admissionPolicy) allows(task *runnerv1.Task) error {
	if policy == nil || task == nil || task.Id <= 0 || task.Context == nil {
		return errors.New("admission task context or protected policy is unavailable")
	}
	fields := task.Context.Fields
	event := admissionText(fields["event_name"], 100)
	repository := admissionText(fields["repository"], 256)
	owner := admissionText(fields["repository_owner"], 100)
	base, valid := admissionRepo(admissionField(fields, "event", "repository"))
	if !valid || event == "" || !strings.EqualFold(repository, base.name) || !strings.EqualFold(owner, base.login) {
		return errors.New("admission repository identity is missing or inconsistent")
	}
	if !policy.owners[base.owner] {
		return errors.New("admission destination owner is not allowed")
	}
	linked, err := admissionPRLinked(fields)
	if err != nil {
		return err
	}
	pr := admissionField(fields, "event", "pull_request")
	if pr == nil && !linked && !strings.HasPrefix(event, "pull_request") {
		return nil
	}
	prObject, ok := pr.GetKind().(*structpb.Value_StructValue)
	if !ok || prObject.StructValue == nil {
		return errors.New("admission pull request origin is missing or malformed")
	}
	prBase, valid := admissionRepo(admissionField(prObject.StructValue.Fields, "base", "repo"))
	if !valid || prBase.id != base.id || prBase.owner != base.owner || !strings.EqualFold(prBase.name, base.name) {
		return errors.New("admission pull request base differs from its destination")
	}
	head, valid := admissionRepo(admissionField(prObject.StructValue.Fields, "head", "repo"))
	if !valid {
		return errors.New("admission pull request head is deleted, missing or malformed")
	}
	if head.id == base.id && (head.owner != base.owner || !strings.EqualFold(head.name, base.name)) {
		return errors.New("admission pull request head identity is inconsistent")
	}
	if head.owner == base.owner || policy.owners[head.owner] || policy.sources[head.owner] {
		return nil
	}
	return errors.New("admission pull request source owner is not trusted")
}

func (r *Runner) admit(task *runnerv1.Task) error {
	if r.admissionErr != nil {
		return r.admissionErr
	}
	return r.admission.allows(task)
}

// NewReporter requires a context to mask its tokens. Preserve only the task ID
// of an invalid nil-context task so it can be failed normally, without parsing
// or executing its workflow or allowing nil context to crash the daemon.
func admissionReportableTask(task *runnerv1.Task) *runnerv1.Task {
	if task == nil || task.Context != nil {
		return task
	}
	return &runnerv1.Task{Id: task.Id, Context: &structpb.Struct{}}
}
