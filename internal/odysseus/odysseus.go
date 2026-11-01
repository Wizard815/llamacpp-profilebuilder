// Package odysseus integrates Odysseus (the Cookbook launcher) with Argos
// so Odysseus stays the author of a model's launch command and Argos stays
// the process supervisor.
//
// Odysseus spawns llama-server in tmux and keeps the command it used in
// cookbook_state.json (task.payload._cmd). This package reads that state over
// Odysseus's HTTP API, derives which Argos model each command belongs to,
// and renders an Argos config fragment containing:
//
//   - one variant model per (model, command), id "<base>--<profile>"
//   - one Argos profile per Odysseus command, whose `pins` rewrite the
//     base model ID to that variant
//
// The base model stays exactly as modelscan generated it, so with no profile
// active -- which is the state at startup and after every config reload --
// Argos runs its own default and the Odysseus commands are opt-in.
//
// Why a variant + profile rather than editing the model: Argos runs
// exactly one process per model ID and allows exactly one active profile, and
// config sources merge with `models` and `profiles` as identity-keyed maps
// (internal/config/merge.go identityMapPaths), so a second file may add new
// model and profile keys but must never redefine one. Generating new IDs keeps
// every source unambiguous.
//
// Two shell constructs have to be translated, because Argos executes the
// command directly (internal/process/process_command.go:511) with no shell:
//
//	export FOO=bar                  -> the model's `env:` list
//	--model "$(printf %s '/p')"     -> --model /p  (exec is not performed)
package odysseus

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"sort"
	"strings"
	"time"

	"gopkg.in/yaml.v3"

	"github.com/Wizard815/Argos/internal/config"
)

// InternalTokenHeader is the header Odysseus' own tool layer uses to reach
// admin-gated routes over loopback. core/middleware.py:
//
//	INTERNAL_TOOL_TOKEN  = os.environ.get("ODYSSEUS_INTERNAL_TOKEN") or secrets.token_hex(32)
//	INTERNAL_TOOL_HEADER = "X-Odysseus-Internal-Token"
//
// and require_admin() returns early when the header matches. Prefer this over a
// session cookie: a cookie expires and belongs to a human, and if
// ODYSSEUS_INTERNAL_TOKEN is unset the token is random per process and never
// persisted, so it must be set explicitly to be usable from outside.
const InternalTokenHeader = "X-Odysseus-Internal-Token"

// Options configures an Odysseus integration run.
type Options struct {
	// BaseURL is Odysseus' root, e.g. http://192.168.20.5:7005 on the host or
	// http://odysseus:7000 from another container on the same Docker network.
	BaseURL string

	// Token is the value of ODYSSEUS_INTERNAL_TOKEN in the Odysseus container.
	// TokenEnv and TokenFile are alternatives; Token wins if set.
	Token     string
	TokenEnv  string
	TokenFile string

	// ProfilePrefix is prepended to every generated profile name, so generated
	// profiles are visually distinct from hand-written ones.
	ProfilePrefix string

	// Statuses filters tasks by task.status. Empty means running + stopped.
	Statuses []string

	// Names maps an Argos base model ID to its configured display name.
	// Generated variants inherit it, so a profile swap does not read as a
	// different model in the UI, the activity table or the log panel.
	Names map[string]string

	// Programs maps an Argos base model ID to the program its own command
	// starts with: everything before the first flag, e.g.
	// ["/app/bin/remote-llama", "rocm"] or ["/opt/llama.cpp/bin/llama-server"].
	// A preset that starts with a bare "llama-server" gets this instead, since
	// the base model's command is how this install actually starts llama.cpp;
	// the Argos container need not have llama-server on its PATH at all.
	Programs map[string][]string

	// ListVariants lists the generated variants in /v1/models, named
	// "<base name> (<label>)"; otherwise they are unlisted.
	ListVariants bool

	// DefaultProgram is the program most base models start with, used for a
	// bare "llama-server" preset whose base model is not in Programs (its ID
	// differs, or it was never scanned).
	DefaultProgram []string

	// Choices maps an Argos base model ID to the Odysseus preset label the
	// operator picked for it. When set, Build emits a single union profile whose
	// pins cover ONLY the chosen models, so a model without a choice keeps
	// Argos's own launch and nothing carries over between models.
	Choices map[string]string

	// ChoicesPath is where per-model choices are persisted, so they survive
	// reloads and restarts. Read back into Options by the caller.
	ChoicesPath string

	// EnsureDio injects "-lm dio" into any command that lacks a load mode.
	// FEATURES.md: "Always pass -lm dio (--load-mode dio). mmap on the model
	// file hangs on this stack."
	EnsureDio bool

	// StateFile reads cookbook_state.json directly off disk instead of over
	// HTTP, for when both containers can see the same file. Preferred, because
	// Odysseus gates /api/cookbook/state behind require_admin AND an
	// AuthMiddleware that only honours the internal tool header for direct
	// loopback clients (app.py:385, _is_trusted_loopback). A request from
	// another container is therefore rejected with 401 however correct the
	// token is, and the bearer-token path authenticates as "api" which then
	// fails the admin check. Reading the file needs no credential at all.
	//
	// When set, BaseURL and the token are unused.
	StateFile string

	Timeout time.Duration
}

// ResolveToken returns the token from Token, TokenEnv or TokenFile.
func (o Options) ResolveToken() (string, error) {
	if strings.TrimSpace(o.Token) != "" {
		return strings.TrimSpace(o.Token), nil
	}
	if o.TokenEnv != "" {
		if v := strings.TrimSpace(os.Getenv(o.TokenEnv)); v != "" {
			return v, nil
		}
	}
	if o.TokenFile != "" {
		b, err := os.ReadFile(o.TokenFile)
		if err != nil {
			return "", fmt.Errorf("reading token file: %w", err)
		}
		if v := strings.TrimSpace(string(b)); v != "" {
			return v, nil
		}
	}
	return "", fmt.Errorf("no Odysseus token: set odysseus.token, or odysseus.tokenEnv / odysseus.tokenFile. " +
		"Note that without ODYSSEUS_INTERNAL_TOKEN set in the Odysseus container its token is random per process and cannot be read from outside")
}

// Payload is the task's launch detail; _cmd is the command Odysseus actually ran.
type Payload struct {
	RepoID string `json:"repo_id"`
	Cmd    string `json:"_cmd"`
}

// Task is one entry in cookbook_state.json's tasks list.
type Task struct {
	ID      string  `json:"id"`
	Name    string  `json:"name"`
	ModelID string  `json:"modelId"`
	Status  string  `json:"status"`
	TS      int64   `json:"ts"`
	Payload Payload `json:"payload"`
}

// Preset is one entry in cookbook_state.json's `presets` list: the Cookbook's
// "Save" button, capped at five per model. This is the durable source -- a task
// only carries a command while it is tracked, and once Odysseus stops it the
// entry moves to removedTasks, which records only an id and a timestamp.
//
// The shape is built in static/js/cookbookServe.js (_saveCurrentConfig):
//
//	{name, model, cmd, remoteHost, port, label, fields}
//
// and _redactStoredCommand only masks tokens, so cmd arrives verbatim.
type Preset struct {
	Name       string `json:"name"`
	Model      string `json:"model"`
	Cmd        string `json:"cmd"`
	RemoteHost string `json:"remoteHost"`
	Port       string `json:"port"`
	Label      string `json:"label"`
}

// State is the part of cookbook_state.json this package reads.
type State struct {
	Tasks   []Task   `json:"tasks"`
	Presets []Preset `json:"presets"`
}

// SanitizeProfileLabel turns an Odysseus preset label into an Argos profile
// name fragment: lowercase, alphanumerics, dot, dash and underscore only.
func SanitizeProfileLabel(label string) string {
	l := strings.ToLower(strings.TrimSpace(label))
	l = reNonID.ReplaceAllString(l, "-")
	l = strings.Trim(l, "-")
	if len(l) > 40 {
		l = strings.Trim(l[:40], "-")
	}
	return l
}

// FetchState reads cookbook_state.json over the Odysseus HTTP API.
func FetchState(ctx context.Context, opts Options) (State, error) {
	var st State

	// File source: no credential, no network, no middleware.
	if f := strings.TrimSpace(opts.StateFile); f != "" {
		raw, err := os.ReadFile(f)
		if err != nil {
			return st, fmt.Errorf("reading odysseus.stateFile %s: %w", f, err)
		}
		if err := json.Unmarshal(raw, &st); err != nil {
			return st, fmt.Errorf("parsing odysseus.stateFile %s: %w", f, err)
		}
		return st, nil
	}

	token, err := opts.ResolveToken()
	if err != nil {
		return st, err
	}
	if strings.TrimSpace(opts.BaseURL) == "" {
		return st, fmt.Errorf("odysseus.baseURL is required")
	}
	timeout := opts.Timeout
	if timeout <= 0 {
		timeout = 20 * time.Second
	}
	url := strings.TrimRight(opts.BaseURL, "/") + "/api/cookbook/state"

	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return st, err
	}
	req.Header.Set(InternalTokenHeader, token)
	req.Header.Set("Accept", "application/json")

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return st, fmt.Errorf("calling %s: %w", url, err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if err != nil {
		return st, fmt.Errorf("reading %s: %w", url, err)
	}
	if resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden {
		return st, fmt.Errorf("%s: HTTP %d -- Odysseus rejected the token. Set ODYSSEUS_INTERNAL_TOKEN "+
			"in the Odysseus container so the value is known outside it, or supply a matching token", url, resp.StatusCode)
	}
	if resp.StatusCode != http.StatusOK {
		return st, fmt.Errorf("%s: HTTP %d: %s", url, resp.StatusCode, truncate(string(body), 200))
	}
	if err := json.Unmarshal(body, &st); err != nil {
		return st, fmt.Errorf("parsing %s: %w", url, err)
	}
	return st, nil
}

var (
	reExport = regexp.MustCompile(`^\s*export\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$`)
	// $(printf %s 'X') / $(printf "%s" 'X') / $(printf '%s' "X") -> X
	rePrintf = regexp.MustCompile(`\$\(\s*printf\s+(?:%s|["']%s["'])\s+(?:["']([^"']*)["']|([^\s)]+))\s*\)`)
	rePort   = regexp.MustCompile(`(--port\s+)\d+`)
	reCont   = regexp.MustCompile(`\\\s*\n\s*`)
	reNonID  = regexp.MustCompile(`[^a-zA-Z0-9._-]+`)
	reEnvKey = regexp.MustCompile(`^[A-Z_][A-Z0-9_]*=`)
)

// SanitizeModelID mirrors modelscan's sanitizeName so a derived ID matches the
// ID modelscan already generated from the same file. This is the join key: an
// Odysseus task names a HuggingFace repo (unsloth/Qwen3.6-35B-A3B-MTP-GGUF),
// which is not an Argos model ID, while the .gguf path in the command is.
func SanitizeModelID(name string) string {
	id := strings.ToLower(name)
	id = reNonID.ReplaceAllString(id, "-")
	id = strings.Trim(id, "-")
	if id == "" {
		id = "model"
	}
	return id
}

// Parsed is a normalised Odysseus command.
type Parsed struct {
	Argv       []string
	Env        []string
	ModelID    string // derived Argos base model ID
	ModelPath  string
	MMProjPath string // --mmproj, when the model is multimodal
	Warnings   []string
}

// ParseCommand normalises one Odysseus command: pulls the export lines out to
// env, joins backslash continuations, unwraps command substitution, and hands
// the port to Argos's ${PORT} macro.
func ParseCommand(cmd string, ensureDio bool) (Parsed, error) {
	var p Parsed

	var body []string
	for _, raw := range strings.Split(cmd, "\n") {
		line := strings.TrimSpace(raw)
		if line == "" {
			continue
		}
		if m := reExport.FindStringSubmatch(line); m != nil {
			entry := m[1] + "=" + strings.Trim(strings.TrimSpace(m[2]), `'"`)
			if !reEnvKey.MatchString(entry) {
				return p, fmt.Errorf("env %q does not match Argos's ^[A-Z_][A-Z0-9_]*=.*$", entry)
			}
			p.Env = withEnv(p.Env, entry)
			continue
		}
		if strings.HasPrefix(line, "export ") {
			return p, fmt.Errorf("unparsed export line %q", line)
		}
		body = append(body, raw)
	}
	if len(body) == 0 {
		return p, fmt.Errorf("no command after the export lines")
	}

	joined := reCont.ReplaceAllString(strings.Join(body, "\n"), " ")
	joined = strings.TrimSpace(strings.TrimRight(joined, "\\ \t"))

	joined = rePrintf.ReplaceAllStringFunc(joined, func(s string) string {
		m := rePrintf.FindStringSubmatch(s)
		if m[1] != "" {
			return m[1]
		}
		return m[2]
	})
	if strings.Contains(joined, "$(") || strings.Contains(joined, "`") {
		return p, fmt.Errorf("command substitution left in command (Argos has no shell): %q", truncate(joined, 120))
	}

	// $$ escapes the $ -- within a Go regexp replacement, "${PORT}" would be
	// read as a *named group* reference and expand to nothing, silently
	// deleting the port Argos needs for its health check.
	joined = rePort.ReplaceAllString(joined, "${1}$${PORT}")

	// Split with Argos's own splitter, so what this package parses is
	// exactly what Argos will parse again at launch time.
	argv, err := config.SanitizeCommand(joined)
	if err != nil {
		return p, err
	}
	if len(argv) == 0 {
		return p, fmt.Errorf("empty command")
	}

	// The environment is also written inline, as
	// `HSA_OVERRIDE_GFX_VERSION=9.0.6 HIP_VISIBLE_DEVICES=0,1 llama-server ...`.
	// Argos runs the command with no shell, so a leading assignment left in
	// argv becomes argv[0] and the model cannot start at all: move them to env,
	// where the export lines above already go, so the device pinning they carry
	// survives too.
	argv, p.Env = takeEnvPrefixes(argv, p.Env)
	if len(argv) == 0 {
		return p, fmt.Errorf("command is only environment assignments")
	}
	// llama-server by name or by path, or the launcher that starts it in another
	// container (the Profiles page's server pick writes that), are all expected.
	if prog := filepath.Base(argv[0]); prog != "llama-server" && prog != "remote-llama" {
		p.Warnings = append(p.Warnings, fmt.Sprintf("command starts with %q, not 'llama-server'", argv[0]))
	}

	// locate the model and optional projector before injecting anything, so the
	// argv indices stay meaningful
	for i, tok := range argv {
		if i+1 >= len(argv) {
			break
		}
		switch tok {
		case "-m", "--model":
			p.ModelPath = argv[i+1]
			if strings.EqualFold(filepath.Ext(p.ModelPath), ".gguf") {
				p.ModelID = SanitizeModelID(strings.TrimSuffix(
					filepath.Base(p.ModelPath), filepath.Ext(p.ModelPath)))
			}
		case "--mmproj", "-mm", "--mmproj-url":
			p.MMProjPath = argv[i+1]
		}
	}
	if p.ModelID == "" {
		return p, fmt.Errorf("no .gguf model path in the command, so it cannot be mapped to an Argos model")
	}
	if p.MMProjPath == "" {
		// a multimodal model without --mmproj cannot see images; flag it rather
		// than silently launching a text-only server
		if isLikelyVisionID(p.ModelID) {
			p.Warnings = append(p.Warnings, "model looks multimodal but the command has no --mmproj, "+
				"so it will start without vision")
		}
	}

	if ensureDio && indexOf(argv, "-lm") < 0 && indexOf(argv, "--load-mode") < 0 {
		// at the end, like --port: right after the program it went in front of a
		// wrapper's own first word (remote-llama rocm ...), which then read
		// "-lm" as the backend name
		argv = append(argv, "-lm", "dio")
	}
	if !hasPort(argv) {
		argv = append(argv, "--port", "${PORT}")
		p.Warnings = append(p.Warnings, "no --port in the command; appended --port ${PORT} so Argos can health-check it")
	}
	p.Argv = argv
	return p, nil
}

// takeEnvPrefixes moves the leading VAR=value arguments off argv and onto env,
// preserving their order. The same ^[A-Z_][A-Z0-9_]*= rule the export lines are
// held to applies here, so anything that is not a valid Argos env entry --
// a lowercase name, a flag like --model=x.gguf (whose name is not uppercase) --
// stays in argv and is still reported as a command that does not start with
// llama-server, rather than being silently treated as environment.
func takeEnvPrefixes(argv, env []string) ([]string, []string) {
	for len(argv) > 0 && reEnvKey.MatchString(argv[0]) {
		env = withEnv(env, argv[0])
		argv = argv[1:]
	}
	return argv, env
}

// withEnv adds one KEY=value assignment, replacing any existing assignment for
// the same key. Both the export lines and the inline prefixes land in this list,
// so a variable written both ways - `export HIP_VISIBLE_DEVICES=0,1` and an
// inline `HIP_VISIBLE_DEVICES=0,1 llama-server ...` - otherwise reaches
// Argos twice. Replacing matches what a shell does (the later assignment
// wins) and keeps the first occurrence's position, so the env stays in the order
// the operator wrote it.
func withEnv(env []string, entry string) []string {
	key, _, _ := strings.Cut(entry, "=")
	for i, existing := range env {
		if k, _, ok := strings.Cut(existing, "="); ok && k == key {
			env[i] = entry
			return env
		}
	}
	return append(env, entry)
}

// SplitCommand joins argv back into an Argos cmd string, quoting only what
// shlex actually treats specially (Argos splits with shlex.Posix.Split).
func SplitCommand(argv []string) string {
	out := make([]string, len(argv))
	for i, a := range argv {
		if strings.ContainsAny(a, " \t\n'\"\\") || strings.HasPrefix(a, "#") {
			out[i] = "'" + strings.ReplaceAll(a, "'", `'\''`) + "'"
		} else {
			out[i] = a
		}
	}
	return strings.Join(out, " ")
}

func isLikelyVisionID(id string) bool {
	l := strings.ToLower(id)
	for _, m := range []string{"-vl", "vl-", "vision", "llava", "qwen2-vl", "qwen2.5-vl", "gemma-3", "pixtral", "minicpm-v"} {
		if strings.Contains(l, m) {
			return true
		}
	}
	return false
}

func hasPort(argv []string) bool {
	for _, a := range argv {
		if a == "--port" || a == "-p" || strings.HasPrefix(a, "--port=") {
			return true
		}
	}
	return false
}

func indexOf(argv []string, v string) int {
	for i, a := range argv {
		if a == v {
			return i
		}
	}
	return -1
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}

// Result is what Build produces: the config fragment's pieces plus advisory
// warnings for the operator.
type Result struct {
	Models   map[string]map[string]any
	Profiles map[string]map[string]any
	Routing  map[string][]string
	Warnings []string

	// ProfileName is the single union profile Build generates from Choices, or
	// empty when no choices were given. One profile, pins only for chosen
	// models -- that is what makes the behaviour per-model rather than global.
	ProfileName string
	// ChoicesResolved maps model ID -> the variant ID its choice resolved to.
	ChoicesResolved map[string]string
}

// ProfileName is the name of the union profile. The operator's profilePrefix
// keeps generated names recognisable; "odysseus" is the fallback.
func (o Options) ProfileName() string {
	if strings.TrimSpace(o.ProfilePrefix) != "" {
		return strings.TrimSpace(o.ProfilePrefix)
	}
	return "odysseus"
}

// Build converts Odysseus' saved launch configs and currently-tracked tasks
// into Argos variant models, plus -- when the operator has picked a preset
// for specific models -- one union profile whose pins cover only those models.
//
// Variant names carry the Odysseus label verbatim (sanitised to a safe ID), so
// a config saved as "turbo4" becomes the variant "<base>--turbo4" and the
// profile pin points the base model at exactly that.
//
// Models without a choice get no pin, so they keep Argos's own launch:
// nothing carries over between models. This is what makes the feature
// per-model, where Argos's own profile engine alone would make one active
// profile rewrite every pinned model at once.
func Build(state State, opts Options) (*Result, error) {
	prefix := opts.ProfilePrefix
	if prefix == "" {
		prefix = "ody"
	}
	allowed := map[string]bool{}
	for _, s := range opts.Statuses {
		allowed[s] = true
	}
	if len(allowed) == 0 {
		allowed["running"] = true
		allowed["stopped"] = true
	}

	profileName := ""
	if len(opts.Choices) > 0 {
		profileName = opts.ProfileName()
	}
	res := &Result{
		Models:          map[string]map[string]any{},
		Profiles:        map[string]map[string]any{},
		Routing:         map[string][]string{},
		ProfileName:     profileName,
		ChoicesResolved: map[string]string{},
	}

	// seen: model -> command key -> variant ID, so the same command is not
	// registered twice for one model.
	seen := map[string]map[string]string{}
	variantIDFor := func(modelID, suffix string) string { return modelID + "--" + suffix }

	register := func(p Parsed, suffix, warnSource string) (string, bool) {
		if len(p.Argv) > 0 && p.Argv[0] == "llama-server" {
			prog := opts.Programs[p.ModelID]
			if len(prog) == 0 {
				prog = opts.DefaultProgram
			}
			if len(prog) > 0 && !(len(prog) == 1 && prog[0] == "llama-server") {
				p.Argv = append(slices.Clone(prog), p.Argv[1:]...)
			} else if len(opts.Programs) > 0 {
				p.Warnings = append(p.Warnings, "starts with a bare 'llama-server' and no base model shows another program to run it with; it fails if llama-server is not on Argos's PATH")
			}
		}
		if seen[p.ModelID] == nil {
			seen[p.ModelID] = map[string]string{}
		}
		if _, dup := seen[p.ModelID][p.Argv0Key()]; dup {
			return "", false
		}
		id := variantIDFor(p.ModelID, suffix)
		seen[p.ModelID][p.Argv0Key()] = id

		entry := map[string]any{
			"cmd":      SplitCommand(p.Argv),
			"unlisted": !opts.ListVariants,
		}
		// A variant is the same model with different flags, so it carries the
		// base model's display name: otherwise a profile swap reads as a
		// different model in the UI, the activity table and the log panel.
		// Listed, it has to stand apart from the base in a client's model
		// picker, so the profile's label goes in the name.
		name := opts.Names[p.ModelID]
		if opts.ListVariants {
			if name == "" {
				name = p.ModelID
			}
			name = fmt.Sprintf("%s (%s)", name, suffix)
		}
		if name != "" {
			entry["name"] = name
		}
		if len(p.Env) > 0 {
			entry["env"] = p.Env
		}
		res.Models[id] = entry
		res.Warnings = append(res.Warnings, prefixWarnings(warnSource, p.Warnings)...)
		return id, true
	}

	// presets first: they are the durable, user-named source.
	for i, pr := range state.Presets {
		cmd := strings.TrimSpace(pr.Cmd)
		if cmd == "" {
			continue
		}
		p, err := ParseCommand(cmd, opts.EnsureDio)
		if err != nil {
			res.Warnings = append(res.Warnings, fmt.Sprintf("preset %q: %v", pr.Label, err))
			continue
		}
		label := SanitizeProfileLabel(pr.Label)
		if label == "" {
			label = fmt.Sprintf("saved-%d", i+1)
		}
		register(p, label, "preset "+pr.Label)
	}

	// tasks are the fallback: a task only holds a command while it is tracked.
	tasks := append([]Task(nil), state.Tasks...)
	sort.SliceStable(tasks, func(i, j int) bool { return tasks[i].TS > tasks[j].TS })
	for _, t := range tasks {
		cmd := strings.TrimSpace(t.Payload.Cmd)
		if cmd == "" {
			continue
		}
		if !allowed[t.Status] {
			continue
		}
		p, err := ParseCommand(cmd, opts.EnsureDio)
		if err != nil {
			res.Warnings = append(res.Warnings, fmt.Sprintf("task %s: %v", t.ID, err))
			continue
		}
		n := len(seen[p.ModelID]) + 1
		suffix := prefix
		if n > 1 {
			suffix = fmt.Sprintf("%s-%d", prefix, n)
		}
		register(p, suffix, t.ID)
	}

	// the union profile: one pin per model the operator actually chose.
	if len(opts.Choices) > 0 {
		pins := map[string]any{}
		for modelID, label := range opts.Choices {
			label = SanitizeProfileLabel(label)
			variant := variantIDFor(modelID, label)
			mc, ok := res.Models[variant]
			if !ok {
				res.Warnings = append(res.Warnings, fmt.Sprintf(
					"choice for %s: no variant %q exists (label %q has no saved config for this model) -- ignored",
					modelID, variant, label))
				continue
			}
			pins[modelID] = variant
			res.ChoicesResolved[modelID] = variant
			_ = mc
		}
		if len(pins) > 0 {
			res.Profiles[profileName] = map[string]any{
				"description": fmt.Sprintf(
					"Per-model launch overrides chosen in Argos from Odysseus saved configs (%d model(s))",
					len(pins)),
				"pins": pins,
			}
		} else {
			res.ProfileName = ""
		}
	}

	return res, nil
}

// ---------- per-model choice persistence ----------

// ChoiceState is the on-disk record of per-model profile choices. Stored beside
// the generated fragment (i.e. in -config-dir, which is a mounted volume), so
// choices survive config reloads and container restarts.
type ChoiceState struct {
	// Model -> Odysseus preset label. An empty or missing entry means "use
	// Argos's own launch for this model".
	Choices map[string]string `json:"choices"`
}

// LoadChoices reads the choice file. A missing file is not an error -- it just
// means nothing has been chosen yet.
func LoadChoices(path string) (ChoiceState, error) {
	var cs ChoiceState
	raw, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return ChoiceState{Choices: map[string]string{}}, nil
		}
		return cs, fmt.Errorf("reading choices %s: %w", path, err)
	}
	if err := json.Unmarshal(raw, &cs); err != nil {
		return cs, fmt.Errorf("parsing choices %s: %w", path, err)
	}
	if cs.Choices == nil {
		cs.Choices = map[string]string{}
	}
	return cs, nil
}

// SaveChoices writes the choice file atomically, so a reader never sees a
// half-written file.
func SaveChoices(path string, cs ChoiceState) error {
	if cs.Choices == nil {
		cs.Choices = map[string]string{}
	}
	data, err := json.MarshalIndent(cs, "", "  ")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return fmt.Errorf("creating choices dir: %w", err)
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, append(data, '\n'), 0o644); err != nil {
		return fmt.Errorf("writing choices %s: %w", path, err)
	}
	return os.Rename(tmp, path)
}

// Argv0Key returns a stable identity for a parsed command, used to skip
// duplicate tasks that re-launched the same command.
// Argv0Key identifies a command for de-duplication: its env and its argv. Two
// presets with the same flags but different env (HIP_VISIBLE_DEVICES=0 and =1)
// are different launches and must stay two variants.
// ProgramOf is the program a model command starts with: the words before its
// first flag, after any leading VAR=value assignments. Nil when there is none.
// CommonProgram is the program most of these commands start with, ignoring a
// bare "llama-server" (that is what a preset already has). Nil when none.
func CommonProgram(cmds []string) []string {
	count := map[string]int{}
	words := map[string][]string{}
	for _, c := range cmds {
		prog := ProgramOf(c)
		if len(prog) == 0 || (len(prog) == 1 && prog[0] == "llama-server") {
			continue
		}
		k := strings.Join(prog, " ")
		count[k]++
		words[k] = prog
	}
	best := ""
	for k, n := range count {
		if n > count[best] || (n == count[best] && k < best) {
			best = k
		}
	}
	return words[best]
}

func ProgramOf(cmd string) []string {
	var prog []string
	for _, w := range strings.Fields(strings.ReplaceAll(cmd, "\\\n", " ")) {
		if strings.HasPrefix(w, "-") {
			break
		}
		if len(prog) == 0 && reEnvKey.MatchString(w) {
			continue
		}
		prog = append(prog, w)
	}
	return prog
}

func (p Parsed) Argv0Key() string {
	return strings.Join(p.Env, "\x00") + "\x01" + strings.Join(p.Argv, "\x00")
}

func prefixWarnings(taskID string, in []string) []string {
	out := make([]string, 0, len(in))
	for _, w := range in {
		out = append(out, fmt.Sprintf("task %s: %s", taskID, w))
	}
	return out
}

// Render produces the config fragment. Shape matches what Argos's
// -config-dir merge expects: a `models:` identity map and a `profiles:`
// identity map, both of which may add new keys but never redefine an existing
// one.
func Render(res *Result) ([]byte, error) {
	var b strings.Builder
	b.WriteString("# Generated by Argos's Odysseus integration -- do not hand-edit.\n" +
		"# Source: Odysseus /api/cookbook/state (task.payload._cmd). Regenerated on refresh.\n" +
		"# The base models, and therefore Argos's own default launch, are untouched:\n" +
		"# these variants are only used while one of these profiles is active.\n\n")

	doc := map[string]any{}
	if len(res.Models) > 0 {
		doc["models"] = res.Models
	}
	if len(res.Profiles) > 0 {
		doc["profiles"] = res.Profiles
	}
	enc, err := yaml.Marshal(doc)
	if err != nil {
		return nil, fmt.Errorf("rendering odysseus fragment: %w", err)
	}
	b.Write(enc)
	return []byte(b.String()), nil
}
