package config

import (
	"fmt"
	"os"
	"sort"

	"gopkg.in/yaml.v3"
)

const DEFAULT_GROUP_ID = "(default)"
const DEFAULT_UNLOAD_TIMEOUT = 10
const (
	LogToStdoutProxy    = "proxy"
	LogToStdoutUpstream = "upstream"
	LogToStdoutBoth     = "both"
	LogToStdoutNone     = "none"
)

type MacroEntry struct {
	Name  string
	Value any
}

type MacroList []MacroEntry

// UnmarshalYAML implements custom YAML unmarshaling that preserves macro definition order
func (ml *MacroList) UnmarshalYAML(value *yaml.Node) error {
	if value.Kind != yaml.MappingNode {
		return fmt.Errorf("macros must be a mapping")
	}

	// yaml.Node.Content for a mapping contains alternating key/value nodes
	entries := make([]MacroEntry, 0, len(value.Content)/2)
	for i := 0; i < len(value.Content); i += 2 {
		keyNode := value.Content[i]
		valueNode := value.Content[i+1]

		var name string
		if err := keyNode.Decode(&name); err != nil {
			return fmt.Errorf("failed to decode macro name: %w", err)
		}

		var val any
		if err := valueNode.Decode(&val); err != nil {
			return fmt.Errorf("failed to decode macro value for '%s': %w", name, err)
		}

		entries = append(entries, MacroEntry{Name: name, Value: val})
	}

	*ml = entries
	return nil
}

// MarshalYAML renders the list back as an ordered mapping, the shape it is
// written in, rather than the default slice-of-structs. Only diagnostics such
// as the config__get_config tool marshal a Config, but when they do the macro
// block should read like the source file.
func (ml MacroList) MarshalYAML() (interface{}, error) {
	node := &yaml.Node{Kind: yaml.MappingNode, Tag: "!!map"}
	for _, entry := range ml {
		var key, value yaml.Node
		if err := key.Encode(entry.Name); err != nil {
			return nil, fmt.Errorf("encoding macro name %q: %w", entry.Name, err)
		}
		if err := value.Encode(entry.Value); err != nil {
			return nil, fmt.Errorf("encoding macro value for %q: %w", entry.Name, err)
		}
		node.Content = append(node.Content, &key, &value)
	}
	return node, nil
}

// Get retrieves a macro value by name
func (ml MacroList) Get(name string) (any, bool) {
	for _, entry := range ml {
		if entry.Name == name {
			return entry.Value, true
		}
	}
	return nil, false
}

type GroupConfig struct {
	Swap       bool     `yaml:"swap"`
	Exclusive  bool     `yaml:"exclusive"`
	Persistent bool     `yaml:"persistent"`
	Members    []string `yaml:"members"`
}

// set default values for GroupConfig
func (c *GroupConfig) UnmarshalYAML(unmarshal func(interface{}) error) error {
	type rawGroupConfig GroupConfig
	defaults := rawGroupConfig{
		Swap:       true,
		Exclusive:  true,
		Persistent: false,
		Members:    []string{},
	}

	if err := unmarshal(&defaults); err != nil {
		return err
	}

	*c = GroupConfig(defaults)
	return nil
}

type HooksConfig struct {
	OnStartup HookOnStartup `yaml:"on_startup"`
}

type HookOnStartup struct {
	Preload []string `yaml:"preload"`
	Profile string   `yaml:"profile"`
}

type Store struct {
	Path string `yaml:"path"`
}

type UIConfig struct {
	Activity UIActivityConfig `yaml:"activity" json:"activity"`
}

type UIActivityConfig struct {
	SessionID []string `yaml:"session_id" json:"session_id"`
}

// ProfileConfig describes a runtime-selectable set of model ID rewrites.
// Empty pin targets disable the corresponding model ID while the profile is
// active. YAML null values decode to the same empty string representation.
type ProfileConfig struct {
	Description string            `yaml:"description" json:"description"`
	Pins        map[string]string `yaml:"pins" json:"pins"`
}

// UnmarshalYAML rejects the removed list-shaped profile syntax with a useful
// migration error while allowing null pin values to normalize to empty strings.
func (c *ProfileConfig) UnmarshalYAML(value *yaml.Node) error {
	if value.Kind != yaml.MappingNode {
		return fmt.Errorf("profile must be a mapping with description and pins; the legacy list syntax is no longer supported")
	}
	type rawProfileConfig ProfileConfig
	var raw rawProfileConfig
	if err := value.Decode(&raw); err != nil {
		return err
	}
	*c = ProfileConfig(raw)
	return nil
}

type Config struct {
	Tailcat            *TailcatConfig `yaml:"tailcat"`
	HealthCheckTimeout int            `yaml:"healthCheckTimeout"`
	// ProfileStateFile remembers the active profile across restarts: it is read
	// on startup and rewritten whenever the active profile changes. Empty
	// disables it, leaving hooks.on_startup.profile as the only source.
	ProfileStateFile string `yaml:"profileStateFile"`
	// ModelTogglesFile persists the per-model GPU toggle mode chosen in the
	// model detail view (standard, single_gpu or multi_model). It must live
	// in the -config-dir so it survives reloads and restarts. Empty disables
	// the detail-view control.
	ModelTogglesFile   string            `yaml:"modelTogglesFile"`
	LogRequests        bool              `yaml:"logRequests"`
	LogLevel           string            `yaml:"logLevel"`
	LogTimeFormat      string            `yaml:"logTimeFormat"`
	LogToStdout        string            `yaml:"logToStdout"`
	MetricsMaxInMemory int               `yaml:"metricsMaxInMemory"`
	CaptureBuffer      int               `yaml:"captureBuffer"`
	Store              *Store            `yaml:"store"`
	UI                 UIConfig          `yaml:"ui"`
	Performance        PerformanceConfig `yaml:"performance"`
	GlobalTTL          int               `yaml:"globalTTL"`
	UnloadTimeout      int               `yaml:"unloadTimeout"`

	Models    map[string]ModelConfig    `yaml:"models"` /* key is model ID */
	Profiles  map[string]ProfileConfig  `yaml:"profiles"`
	Selectors map[string]SelectorConfig `yaml:"selectors"`

	// GlobalConcurrencyLimit caps the number of inference requests served at
	// once across all models. 0 (default) means no limit. See issue #1086.
	GlobalConcurrencyLimit int `yaml:"globalConcurrencyLimit"`

	// routing is the canonical source for swap/scheduling configuration.
	// New code must read Routing, never the backwards-compat fields below.
	Routing RoutingConfig `yaml:"routing"`

	// Groups and Matrix are permanent backwards-compat input fields for the
	// legacy top-level `groups:`/`matrix:` keys. They are normalized into
	// Routing by LoadConfigFromReader. New code must not read them directly.
	Groups map[string]GroupConfig `yaml:"groups"` /* key is group ID */
	Matrix *MatrixConfig          `yaml:"matrix"`

	// for key/value replacements in model's cmd, cmdStop, proxy, checkEndPoint
	Macros MacroList `yaml:"macros"`

	// map aliases to actual model IDs
	aliases map[string]string

	// automatic port assignments
	StartPort int `yaml:"startPort"`

	// Ports is the port each model's ${PORT} resolved to. Not configuration:
	// a reload passes it back so running models keep their ports.
	Ports map[string]int `yaml:"-" json:"-"`

	// hooks, see: #209
	Hooks HooksConfig `yaml:"hooks"`

	// send loading state in reasoning
	SendLoadingState bool `yaml:"sendLoadingState"`

	// present aliases to /v1/models OpenAI API listing
	IncludeAliasesInList bool `yaml:"includeAliasesInList"`

	// support API keys, see issue #433, #50, #251
	RequiredAPIKeys []string `yaml:"apiKeys"`

	// upstream controls behaviour of the /upstream passthrough endpoint
	Upstream UpstreamConfig `yaml:"upstream"`

	// ModelScan configures directory-based model auto-discovery. See
	// internal/modelscan. Local fork addition, not upstream Argos.
	ModelScan ModelScanConfig `yaml:"modelScan"`

	// Odysseus wires Argos to an Odysseus instance so Odysseus can own a
	// model's launch command while Argos owns the process. See
	// internal/odysseus. Local fork addition, not upstream Argos.
	Odysseus *OdysseusConfig `yaml:"odysseus"`

	// tailcatEnabled records whether this process started a Tailcat listener.
	// It is runtime state, not user configuration, so it must never appear in
	// rendered configuration output.
	tailcatEnabled bool
}

// SetTailcatEnabled records whether this process has a Tailcat listener.
// main owns this startup-only setting from -listen-tailcat.
func (c *Config) SetTailcatEnabled(enabled bool) {
	c.tailcatEnabled = enabled
}

// TailcatEnabled reports whether this process has a Tailcat listener.
func (c Config) TailcatEnabled() bool {
	return c.tailcatEnabled
}

// RoutingConfig is the canonical, normalized routing/scheduling configuration.
type RoutingConfig struct {
	Scheduler SchedulerConfig `yaml:"scheduler"`
	Router    RouterConfig    `yaml:"router"`
}

type SchedulerConfig struct {
	Use      string            `yaml:"use"` // default "fifo"
	Settings SchedulerSettings `yaml:"settings"`
}

type SchedulerSettings struct {
	Fifo FifoConfig `yaml:"fifo"`
}

type FifoConfig struct {
	Priority map[string]int `yaml:"priority"` // model ID -> priority, default 0
}

type RouterConfig struct {
	Use      string         `yaml:"use"` // "group" (default) | "matrix" | "pool"
	Settings RouterSettings `yaml:"settings"`
}

type RouterSettings struct {
	Groups map[string]GroupConfig `yaml:"groups"`
	Matrix *MatrixConfig          `yaml:"matrix"`
	Pool   PoolConfig             `yaml:"pool"`
}

// PoolConfig configures the pool router. Every model belongs to one shared
// pool and models load side by side while their VRAM fits; eviction only
// happens when a new model does not fit.
type PoolConfig struct {
	// VRAMGiB is the pool's total VRAM budget. 0 detects it from the GPUs at
	// startup (the sum of every card's total VRAM).
	VRAMGiB float64 `yaml:"vramGiB"`

	// ReserveGiB is kept free for KV cache growth, compute buffers and other
	// processes. Default 2.
	ReserveGiB float64 `yaml:"reserveGiB"`

	// ProtectSeconds shields recently used models: a model that served a
	// request within this window is evicted only when nothing else can make
	// room. Default 600.
	ProtectSeconds int `yaml:"protectSeconds"`

	// ProtectWaits makes the protection hold: a request for another model
	// waits rather than unload a model used within ProtectSeconds, so a chat
	// or agent that pauses between prompts does not lose its model (and its
	// prompt cache) to a reload. Go next on Schedule -> Live sends a waiting
	// request on anyway. Default true; false only orders evictions.
	ProtectWaits *bool `yaml:"protectWaits"`

	// Persistent lists models that are never evicted to make room for another
	// model. They still stop on TTL, unload or shutdown.
	Persistent []string `yaml:"persistent"`

	// Cards lists each GPU's VRAM in GiB, numbered from 0 the way
	// HIP_VISIBLE_DEVICES numbers them. Empty detects the cards at startup
	// when vramGiB is 0. It replaces vramGiB; set one or the other.
	Cards []float64 `yaml:"cards"`

	// Placement is "auto" (default) or "off". Auto puts each model on the
	// card, or cards, with room and sets HIP_VISIBLE_DEVICES when the model
	// starts. It needs per-card sizes, from cards or from detection. Off
	// plans by total VRAM only and leaves HIP_VISIBLE_DEVICES alone.
	Placement string `yaml:"placement"`

	// Sizes maps a model ID to its VRAM in GiB. It lives here, not under
	// models.*.vram, so a separate config file can supply sizes without
	// redefining the models (a model defined in two sources is an error).
	// models.*.vram wins over this when both are set; either wins over the
	// estimate from the model file.
	Sizes map[string]float64 `yaml:"sizes"`

	// AnswerFirst keeps a chat that is answering at full speed: a long new
	// prompt for one of these models waits while another chat on the same
	// model is generating, because llama.cpp would read that prompt in the
	// same batches and the answer would drop to a token or two a second.
	AnswerFirst AnswerFirstConfig `yaml:"answerFirst"`

	// OneAtATime lists models that get one request at a time: the rest wait
	// in Argos, in order. Two chats on a model each run at the speed one has
	// alone, one after the other; with -np 2 each still keeps its own slot's
	// cache. It replaces answer first for these models.
	OneAtATime []string `yaml:"oneAtATime"`

	// ShareGuard holds a model's new request while another model on the same
	// GPU is answering, when that other model was seen (in its log) to slow
	// down a lot beside it.
	ShareGuard ShareGuardConfig `yaml:"shareGuard"`

	// CPUFallback lists models that run on the CPU when they do not fit on
	// the GPUs beside what is loaded, instead of unloading it: small helpers
	// such as the assistant's chat and embedding models. They are started
	// with every GPU hidden, so llama.cpp finds none and runs on the CPU.
	CPUFallback []string `yaml:"cpuFallback"`
}

// ShareGuardConfig is the pool's share guard.
type ShareGuardConfig struct {
	Enabled bool `yaml:"enabled"`
	// MaxSlowdownPercent: a model that answers more than this much slower
	// beside another is protected from it. Default 50.
	MaxSlowdownPercent int `yaml:"maxSlowdownPercent"`
	// MaxWaitSeconds is how long a request waits at most. Default 120.
	MaxWaitSeconds int `yaml:"maxWaitSeconds"`
}

const (
	ShareGuardDefaultMaxSlowdownPercent = 50
	ShareGuardDefaultMaxWaitSeconds     = 120
)

// OneAtATimeFor reports whether a model gets one request at a time.
func (p PoolConfig) OneAtATimeFor(model string) bool {
	for _, m := range p.OneAtATime {
		if m == model {
			return true
		}
	}
	return false
}

// AnswerFirstConfig is the pool's answer-first setting.
type AnswerFirstConfig struct {
	// Models it applies to, by ID or alias. Empty turns it off.
	Models []string `yaml:"models"`

	// MinPromptTokens is the shortest new prompt part that waits; shorter
	// ones (a turn of a conversation the model already has cached) go
	// straight through. Estimated from the request at about four bytes a
	// token. Default 4000.
	MinPromptTokens int `yaml:"minPromptTokens"`

	// MaxWaitSeconds is how long a prompt waits at most before it goes
	// anyway. Default 300.
	MaxWaitSeconds int `yaml:"maxWaitSeconds"`
}

// AnswerFirstDefaultMinPromptTokens and AnswerFirstDefaultMaxWaitSeconds apply
// when answerFirst leaves them unset.
const (
	AnswerFirstDefaultMinPromptTokens = 4000
	AnswerFirstDefaultMaxWaitSeconds  = 300
)

// Applies reports whether answer-first holds prompts for this model ID.
func (a AnswerFirstConfig) Applies(model string) bool {
	for _, m := range a.Models {
		if m == model {
			return true
		}
	}
	return false
}

// PoolDefaultReserveGiB and PoolDefaultProtectSeconds apply when the pool
// config leaves those fields unset.
const (
	PoolDefaultReserveGiB     = 2
	PoolDefaultProtectSeconds = 600
)

func (c *Config) RealModelName(search string) (string, bool) {
	if _, found := c.Models[search]; found {
		return search, true
	} else if name, found := c.aliases[search]; found {
		return name, found
	} else {
		return "", false
	}
}

func (c *Config) FindConfig(modelName string) (ModelConfig, string, bool) {
	if realName, found := c.RealModelName(modelName); !found {
		return ModelConfig{}, "", false
	} else {
		return c.Models[realName], realName, true
	}
}

// ResolveBaseModel resolves a model ID or alias without applying profiles.
func (c *Config) ResolveBaseModel(search string) (string, bool) {
	return c.RealModelName(search)
}

func LoadConfig(path string) (Config, error) {
	file, err := os.Open(path)
	if err != nil {
		return Config{}, err
	}
	defer file.Close()
	return LoadConfigFromReader(file)
}

// rewrites the yaml to include a default group with any orphaned models
func AddDefaultGroupToConfig(config Config) Config {

	if config.Groups == nil {
		config.Groups = make(map[string]GroupConfig)
	}

	defaultGroup := GroupConfig{
		Swap:      true,
		Exclusive: true,
		Members:   []string{},
	}
	// if groups is empty, create a default group and put
	// all models into it
	if len(config.Groups) == 0 {
		for modelName := range config.Models {
			defaultGroup.Members = append(defaultGroup.Members, modelName)
		}
	} else {
		// iterate over existing group members and add non-grouped models into the default group
		for modelName := range config.Models {
			foundModel := false
		found:
			// search for the model in existing groups
			for _, groupConfig := range config.Groups {
				for _, member := range groupConfig.Members {
					if member == modelName {
						foundModel = true
						break found
					}
				}
			}

			if !foundModel {
				defaultGroup.Members = append(defaultGroup.Members, modelName)
			}
		}
	}

	sort.Strings(defaultGroup.Members) // make consistent ordering for testing
	config.Groups[DEFAULT_GROUP_ID] = defaultGroup

	return config
}
