package server

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/Wizard815/Argos/internal/chain"
	"github.com/Wizard815/Argos/internal/config"
	"github.com/Wizard815/Argos/internal/docagent"
	"github.com/Wizard815/Argos/internal/event"
	"github.com/Wizard815/Argos/internal/hw"
	"github.com/Wizard815/Argos/internal/logmon"
	"github.com/Wizard815/Argos/internal/mcptools"
	"github.com/Wizard815/Argos/internal/modelctl"
	"github.com/Wizard815/Argos/internal/odysseus"
	"github.com/Wizard815/Argos/internal/perf"
	"github.com/Wizard815/Argos/internal/process"
	"github.com/Wizard815/Argos/internal/router"
	"github.com/Wizard815/Argos/internal/router/scheduler"
	"github.com/Wizard815/Argos/internal/store"
	"github.com/Wizard815/Argos/internal/swaputil"
)

// Server owns the HTTP mux, cross-cutting middleware, and the model dispatch.
// It supersedes router.Server: it builds the router directly and dispatches to
// it itself.
type Server struct {
	cfg config.Config

	muxlog      *logmon.Monitor
	proxylog    *logmon.Monitor
	upstreamlog *logmon.Monitor

	perf     *perf.Monitor
	inflight *inflightTracker
	// answerFirst holds long new prompts while another chat on the model answers
	answerFirst *answerFirstState
	// gate is the waiting room in front of each model (one at a time, answer first)
	gate *slotGate

	// updatePending: models whose new settings wait for them to go idle (a
	// reload put off), since when
	pendingMu     sync.Mutex
	updatePending map[string]time.Time
	metrics       *metricsMonitor
	store         *store.Store
	build         BuildInfo
	hardware      *hw.HardwareSnapshot

	// reference is Argos's own embedded documentation, served to the
	// Playground's agentic chat and to external MCP clients through /api/mcp.
	// It is immutable and independent of cfg, so the same library is shared
	// across the Server instances a hot config reload creates. A nil value
	// disables the endpoint; Docs methods are nil-receiver safe.
	reference *docagent.Docs

	// tools is the MCP tool surface served at /api/mcp. Providers are
	// aggregated here rather than enumerated in the handler, so a future
	// provider that proxies an upstream MCP endpoint plugs in without
	// touching the transport.
	tools *mcptools.Registry

	profileMu     sync.RWMutex
	activeProfile string

	// modelModes holds each model's GPU toggle mode (standard, single_gpu or
	// multi_model) chosen in the model detail view. Guarded by modelModesMu.
	// Loaded from cfg.ModelTogglesFile at startup and rewritten on every
	// change, so it survives reloads and restarts.
	modelModesMu sync.RWMutex
	modelModes   map[string]string

	// cfgPath and cfgDir are the -config and -config-dir paths, recorded by
	// main via SetConfigPaths. The Odysseus integration needs them to enumerate
	// the other config sources when guarding a generated fragment against
	// duplicate identity keys.
	cfgPath string
	cfgDir  string

	// odysseus holds the integration's runtime bookkeeping (last run, counts,
	// last error) and serialises refreshes.
	odysseus odysseusState

	local router.LocalRouter

	mux     *http.ServeMux
	handler http.Handler

	shutdownCtx    context.Context
	shutdownFn     context.CancelFunc
	shuttingDown   atomic.Bool
	tailcatAddress atomic.Pointer[string]
}

func (s *Server) SetTailcatAddress(address string) {
	value := address
	s.tailcatAddress.Store(&value)
}

func (s *Server) TailcatAddress() string {
	if value := s.tailcatAddress.Load(); value != nil {
		return *value
	}
	return ""
}

type tailcatRequestContextKey struct{}

func isTailcatRequest(ctx context.Context) bool {
	marked, _ := ctx.Value(tailcatRequestContextKey{}).(bool)
	return marked
}

// ActiveProfile returns the active runtime profile, or an empty string when no
// profile is active.
func (s *Server) ActiveProfile() string {
	s.profileMu.RLock()
	defer s.profileMu.RUnlock()
	return s.activeProfile
}

// setActiveProfile updates the runtime selection. An empty name deactivates
// profiles. It returns whether the selection changed.
func (s *Server) setActiveProfile(name string) (bool, error) {
	if name != "" {
		if _, ok := s.cfg.Profiles[name]; !ok {
			return false, fmt.Errorf("profile %q not found", name)
		}
	}
	s.profileMu.Lock()
	if s.activeProfile == name {
		s.profileMu.Unlock()
		return false, nil
	}
	s.activeProfile = name
	s.profileMu.Unlock()

	s.proxylog.Infof("active profile changed to %q", name)
	event.Emit(swaputil.ProfileChangedEvent{Active: name})
	if s.cfg.ProfileStateFile != "" {
		if err := saveProfileState(s.cfg.ProfileStateFile, name); err != nil {
			s.proxylog.Warnf("profile state: %v", err)
		}
	}
	return true, nil
}

// modelPostJSONRoutes are endpoints with a model id in the JSON request body.
var modelPostJSONRoutes = []string{
	"/v1/chat/completions",
	"/v1/responses",
	"/v1/completions",
	"/v1/messages",
	"/v1/messages/count_tokens",
	"/v1/embeddings",
	"/reranking",
	"/rerank",
	"/v1/rerank",
	"/v1/reranking",
	"/infill",
	"/completion",
	"/v1/audio/speech",
	"/v1/audio/voices",
	"/v1/images/generations",
	"/sdapi/v1/txt2img",
	"/sdapi/v1/img2img",

	// audio.cpp generic task API
	"/audioapi/v1/tasks/run",

	// versionless routes, the /v/ is stripped before the request is forwarded upstream
	// see issue #728
	"/v/chat/completions",
	"/v/responses",
	"/v/completions",
	"/v/messages",
	"/v/messages/count_tokens",
	"/v/embeddings",
	"/v/rerank",
	"/v/reranking",
}

// modelPostFormRoutes are multipart/form-data endpoints with a model id in the form data
var modelPostFormRoutes = []string{
	"/v1/audio/transcriptions",
	"/v1/images/edits",
}

// modelGetRoutes are model-dispatched GET endpoints (the model arrives as a
// query parameter).
var modelGetRoutes = []string{
	"/v1/audio/voices",
	"/sdapi/v1/loras",
	"/props",
}

// isMetricsRecordPath reports whether path is one of the model-dispatched
// endpoints that the metrics middleware records in the activity log.
func isMetricsRecordPath(path string) bool {
	for _, p := range modelPostJSONRoutes {
		if p == path {
			return true
		}
	}
	for _, p := range modelPostFormRoutes {
		if p == path {
			return true
		}
	}
	for _, p := range modelGetRoutes {
		if p == path {
			return true
		}
	}
	return false
}

// BuildInfo carries version metadata surfaced by GET /api/version.
type BuildInfo struct {
	Version string
	Commit  string
	Date    string
}

func New(cfg config.Config, muxlog *logmon.Monitor, proxylog *logmon.Monitor, upstreamlog *logmon.Monitor, perfMon *perf.Monitor, st *store.Store, build BuildInfo, hardware *hw.HardwareSnapshot, refs *docagent.Docs) (*Server, error) {
	var local router.LocalRouter
	var err error

	// Load the per-model GPU modes before the routers are built: a multi_model
	// model is expanded into per-GPU copies fronted by a spillover selector, and
	// the router has to be created from the expanded config.
	modes, err := loadModelToggles(cfg.ModelTogglesFile)
	if err != nil {
		proxylog.Warnf("model toggles: %v", err)
		modes = map[string]string{}
	}
	// The expansion builds the copies from the model's effective launch, so the
	// odysseus choices and the remembered active profile have to be known first.
	choices := map[string]string{}
	if cfg.Odysseus != nil && cfg.Odysseus.ChoicesPath != "" {
		if cs, err := odysseus.LoadChoices(cfg.Odysseus.ChoicesPath); err == nil {
			choices = cs.Choices
		}
	}
	pins := map[string]string{}
	if active, _, _ := loadProfileState(cfg.ProfileStateFile); active != "" {
		if p, ok := cfg.Profiles[active]; ok {
			pins = p.Pins
		}
	}
	applyMultiModel(&cfg, modes, pins, choices, listGPUs(drmRoot))

	switch cfg.Routing.Router.Use {
	case "matrix":
		local, err = router.NewMatrix(cfg, proxylog, upstreamlog)
		if err != nil {
			return nil, fmt.Errorf("creating matrix router: %w", err)
		}
	case "pool":
		local, err = router.NewPool(cfg, proxylog, upstreamlog)
		if err != nil {
			return nil, fmt.Errorf("creating pool router: %w", err)
		}
	default: // "group"
		local, err = router.NewGroup(cfg, proxylog, upstreamlog)
		if err != nil {
			return nil, fmt.Errorf("creating group router: %w", err)
		}
	}

	if st == nil {
		return nil, fmt.Errorf("store is required")
	}

	shutdownCtx, shutdownFn := context.WithCancel(context.Background())
	s := &Server{
		cfg:           cfg,
		muxlog:        muxlog,
		proxylog:      proxylog,
		upstreamlog:   upstreamlog,
		perf:          perfMon,
		inflight:      newInflightTracker(),
		answerFirst:   newAnswerFirst(),
		metrics:       newMetricsMonitor(proxylog, cfg.MetricsMaxInMemory, cfg.CaptureBuffer, st),
		store:         st,
		build:         build,
		hardware:      hardware,
		reference:     refs,
		activeProfile: cfg.Hooks.OnStartup.Profile,
		local:         local,
		shutdownCtx:   shutdownCtx,
		shutdownFn:    shutdownFn,
	}
	// Bring back the profile the operator last selected. Without this, a restart
	// or a container recreate silently falls back to "no profile".
	s.gate = newSlotGate(s.inflight.SetHeld)
	go s.watchSharing()
	s.restoreProfileState()
	// crashes: a model that dies on every request is stopped from reloading forever
	router.WatchExits()
	scheduler.Marks.SetNote(s.inflight.SetHeld) // the queue says why a request waits
	if s.perf != nil {
		router.SetCardUsedSource(s.liveCardUsed)
	}
	// SysProvider is constructed here because this is where perf and hardware
	// are in scope; wiring those in later is a change to internal/mcptools.
	tools, err := mcptools.New(
		docagent.NewDocsProvider(refs),
		mcptools.NewSysProvider(nil),
		config.NewConfigProvider(cfg),
		// s, not a snapshot: the provider reports which models are loaded and
		// which profile is active, and both change while the server runs.
		modelctl.NewProvider(s),
		// the scheduler's live view: cards, prefill, queue, running requests
		&scheduleProvider{s: s},
	)
	if err != nil {
		return nil, fmt.Errorf("building the MCP tool registry: %w", err)
	}
	s.tools = tools

	// Per-model Odysseus choices must be known before the first modelStatus is
	// served, so the detail view shows the effective launch on first render.
	if s.cfg.Odysseus != nil && s.cfg.Odysseus.ChoicesPath != "" {
		if cs, err := odysseus.LoadChoices(s.cfg.Odysseus.ChoicesPath); err == nil {
			s.setChoices(cs.Choices)
		} else {
			proxylog.Warnf("odysseus: loading choices: %v", err)
		}
	}

	s.restoreModelToggles(modes)
	// Install the launch-time policy that applies each model's GPU mode. It is
	// consulted just before an upstream starts, so a mode change takes effect
	// on the next launch without touching the configured command.
	process.SetLaunchEnvPolicy(s.launchEnv)

	s.routes()
	s.startPreload()
	s.startOdysseusRefresh()
	return s, nil
}

// modelHandler dispatches a model-routed request to the router. The model is
// resolved once via swaputil.FetchContext.
func (s *Server) modelHandler(w http.ResponseWriter, r *http.Request) {
	stripVersionPrefix(r)
	stripAudioAPIPrefix(r)

	data, err := swaputil.FetchContext(r, s.cfg)
	if err != nil {
		swaputil.SendError(w, r, swaputil.ErrNoModelInContext)
		return
	}

	if !s.local.Handles(data.ModelID) {
		swaputil.SendError(w, r, router.ErrNoRouterFound)
		return
	}
	if cr, blocked := router.Crashes.Blocked(data.ModelID); blocked {
		swaputil.SendResponse(w, r, http.StatusServiceUnavailable, fmt.Sprintf(
			"%s crashed %d times in the last 10 minutes and is not started again on a request. Last crash: %s. "+
				"Fix its command, or load it by hand from Schedule -> Live to try again.", data.ModelID, cr.Recent, cr.Line))
		return
	}
	s.proxylog.Debugf("dispatch: using local process for model: %s", data.ModelID)
	s.local.ServeHTTP(w, r)
}

// stripVersionPrefix rewrites versionless /v/... requests to their /... form
// before forwarding upstream (issue #728).
func stripVersionPrefix(r *http.Request) {
	if strings.HasPrefix(r.URL.Path, "/v/") {
		r.URL.Path = strings.TrimPrefix(r.URL.Path, "/v")
	}
}

// stripAudioAPIPrefix rewrites /audioapi/... requests to their /... form
// before forwarding upstream, so /audioapi/v1/tasks/run reaches the upstream
// as /v1/tasks/run.
func stripAudioAPIPrefix(r *http.Request) {
	r.URL.Path = strings.TrimPrefix(r.URL.Path, "/audioapi")
}

// routes builds the mux, registers every route, and wraps the mux with the
// global CORS middleware.
func (s *Server) routes() {

	authMW := CreateAuthMiddleware(s.cfg)
	modelMWs := []chain.Middleware{authMW}
	// globalConcurrencyLimit guards the top of the inference chain; a limit of
	// 0 (the default) means no limit, so the handler is left out of the chain
	// entirely rather than wrapping every request in a no-op semaphore.
	if s.cfg.GlobalConcurrencyLimit > 0 {
		modelMWs = append(modelMWs, CreateConcurrencyLimitMiddleware(s.cfg.GlobalConcurrencyLimit))
	}
	modelMWs = append(modelMWs,
		CreateProfileMiddleware(s),
		CreateSelectorMiddleware(s),
		CreateRequestContextMiddleware(s.cfg),
		CreateInflightMiddleware(s.inflight, s.cfg),
		CreateAnswerFirstMiddleware(s),
		CreateFilterMiddleware(s.cfg),
		CreateFormFilterMiddleware(s.cfg),
		CreateMetricsMiddleware(s.metrics, s.cfg),
	)
	modelChain := chain.New(modelMWs...)
	// Custom endpoints only need auth.
	apiChain := chain.New(authMW)

	mux := http.NewServeMux()
	dispatch := http.HandlerFunc(s.modelHandler)

	for _, path := range modelPostJSONRoutes {
		mux.Handle("POST "+path, modelChain.Then(dispatch))
	}
	for _, path := range modelPostFormRoutes {
		mux.Handle("POST "+path, modelChain.Then(dispatch))
	}
	for _, path := range modelGetRoutes {
		mux.Handle("GET "+path, modelChain.Then(dispatch))
	}

	// Argos API + custom endpoints.
	mux.Handle("GET /v1/models", apiChain.ThenFunc(s.handleListModels))
	mux.Handle("GET /models", apiChain.ThenFunc(s.handleListModels))
	mux.Handle("GET /logs", apiChain.ThenFunc(s.handleLogs))
	mux.Handle("GET /logs/stream", apiChain.ThenFunc(s.handleLogStream))
	mux.Handle("GET /logs/stream/{logMonitorID...}", apiChain.ThenFunc(s.handleLogStream))
	// One-shot read of a single monitor's history. The literal "stream" segment
	// above is more specific than this wildcard, so it keeps matching first.
	mux.Handle("GET /logs/{logMonitorID...}", apiChain.ThenFunc(s.handleModelLogs))

	mux.HandleFunc("GET /health", handleHealth)
	mux.HandleFunc("GET /wol-health", handleHealth)
	mux.HandleFunc("GET /{$}", handleRootRedirect)

	// Embedded UI.
	mux.Handle("GET /ui/", chain.New(authMW).ThenFunc(s.handleUI))
	mux.HandleFunc("GET /favicon.ico", s.handleFavicon)

	// Prometheus metrics (wrapped by apiChain, matches the legacy endpoint).
	mux.Handle("GET /metrics", apiChain.ThenFunc(s.handleMetrics))

	// Operations endpoints.
	mux.Handle("GET /unload", apiChain.ThenFunc(s.handleUnload))
	mux.Handle("GET /running", apiChain.ThenFunc(s.handleRunning))

	// Upstream passthrough. Meter only the model-dispatched endpoints that can
	// produce token usage/timings.
	upstreamChain := apiChain.Append(
		CreateProfileMiddleware(s),
		CreateUpstreamInflightMiddleware(s.inflight, s.cfg),
		CreateMetricsMiddleware(s.metrics, s.cfg),
	)
	mux.HandleFunc("GET /upstream", handleUpstreamRedirect)
	mux.Handle("/upstream/{upstreamPath...}", upstreamChain.ThenFunc(s.handleUpstream))

	// ComfyUI compatibility passthrough. This uses the fixed comfyui_auto model,
	// whose compatibility settings are applied while loading config. Only the
	// root path may start an unloaded model.
	mux.Handle("/comfyui", apiChain.ThenFunc(handleComfyUIRedirect))
	mux.Handle("/comfyui/{comfyPath...}", apiChain.ThenFunc(s.handleComfyUI))

	// API group (API-key protected) consumed by the UI.
	mux.Handle("GET /api/pool", apiChain.ThenFunc(s.handleAPIPool))
	mux.Handle("POST /api/crashes/clear/{model...}", apiChain.ThenFunc(s.handleAPIClearCrashes))
	mux.Handle("POST /api/models/unload", apiChain.ThenFunc(s.handleAPIUnloadAll))
	mux.Handle("POST /api/models/unload/{model...}", apiChain.ThenFunc(s.handleAPIUnloadModel))
	mux.Handle("POST /api/models/rescan", apiChain.ThenFunc(s.handleAPIRescanModels))
	mux.Handle("POST /api/odysseus/refresh", apiChain.ThenFunc(s.handleAPIOdysseusRefresh))
	mux.Handle("GET /api/odysseus/status", apiChain.ThenFunc(s.handleAPIOdysseusStatus))
	mux.Handle("GET /api/odysseus/model/{model}/profile", apiChain.ThenFunc(s.handleAPIOdysseusModelProfiles))
	mux.Handle("PUT /api/odysseus/model/{model}/profile", apiChain.ThenFunc(s.handleAPIOdysseusModelProfile))
	mux.Handle("PUT /api/models/{model}/mode", apiChain.ThenFunc(s.handleAPIModelMode))
	mux.Handle("GET /api/models/modes", apiChain.ThenFunc(s.handleAPIModelModes))
	mux.Handle("GET /api/profiles", apiChain.ThenFunc(s.handleAPIProfiles))
	mux.Handle("PUT /api/profiles/active", apiChain.ThenFunc(s.handleAPIActiveProfile))
	mux.Handle("GET /api/inflight", apiChain.ThenFunc(s.handleAPIInflight))
	mux.Handle("POST /api/inflight/{id}/cancel", apiChain.ThenFunc(s.handleAPICancelInflight))
	mux.Handle("POST /api/inflight/{id}/next", apiChain.ThenFunc(s.handleAPIInflightNext))
	mux.Handle("POST /api/inflight/{id}/hold", apiChain.ThenFunc(s.handleAPIInflightHold))
	mux.Handle("DELETE /api/inflight/{id}/hold", apiChain.ThenFunc(s.handleAPIInflightHold))
	mux.Handle("GET /api/events", apiChain.ThenFunc(s.handleAPIEvents))
	mux.Handle("GET /api/metrics/activity", apiChain.ThenFunc(s.handleAPIActivity))
	mux.Handle("GET /api/metrics/stats", apiChain.ThenFunc(s.handleAPIActivityStats))
	mux.Handle("GET /api/performance", apiChain.ThenFunc(s.handleAPIPerformance))
	mux.Handle("GET /api/version", apiChain.ThenFunc(s.handleAPIVersion))
	mux.Handle("GET /api/hardware", apiChain.ThenFunc(s.handleAPIHardware))
	mux.Handle("GET /api/tailcat", apiChain.ThenFunc(s.handleAPITailcat))
	mux.Handle("GET /api/captures/{id}", apiChain.ThenFunc(s.handleAPICapture))

	// Stateless MCP server exposing Argos's own documentation as tools,
	// consumed by the Playground's agentic chat and by any external MCP client.
	// Registered without a method so non-POST reaches the handler and gets a
	// 405 with Allow, rather than the mux's bare 404.
	mux.Handle("/api/mcp", apiChain.ThenFunc(s.handleAPIMCP))

	s.mux = mux
	s.handler = chain.New(CreateRequestLogMiddleware(s.proxylog), CreateCORSMiddleware()).Then(mux)
}

func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	s.handler.ServeHTTP(w, r)
}

// ServeTailcatHTTP applies Tailcat's deliberately narrow HTTP capability
// surface before delegating to the normal, API-key-protected handler.
func (s *Server) ServeTailcatHTTP(w http.ResponseWriter, r *http.Request) {
	tc := s.cfg.Tailcat
	if !s.cfg.TailcatEnabled() || tc == nil {
		http.NotFound(w, r)
		return
	}

	inference := isTailcatInferenceRequest(r)
	if !tc.Admin {
		allowed := r.Method == http.MethodGet && (r.URL.Path == "/health" || r.URL.Path == "/v1/models" || r.URL.Path == "/models")
		if r.Method == http.MethodOptions {
			allowed = isTailcatInferencePath(r.URL.Path)
		}
		if inference {
			allowed = true
		}
		if !allowed {
			http.NotFound(w, r)
			return
		}
	}

	if inference && r.Method != http.MethodOptions {
		model, err := swaputil.ExtractModel(r)
		if err != nil || !tailcatModelAllowed(tc.Models, model) {
			http.NotFound(w, r)
			return
		}
	}
	r = r.WithContext(context.WithValue(r.Context(), tailcatRequestContextKey{}, true))
	s.handler.ServeHTTP(w, r)
}

func isTailcatInferenceRequest(r *http.Request) bool {
	if r.Method == http.MethodPost {
		for _, path := range modelPostJSONRoutes {
			if r.URL.Path == path {
				return true
			}
		}
		for _, path := range modelPostFormRoutes {
			if r.URL.Path == path {
				return true
			}
		}
	}
	if r.Method == http.MethodGet {
		for _, path := range modelGetRoutes {
			if r.URL.Path == path {
				return true
			}
		}
	}
	return false
}

func isTailcatInferencePath(path string) bool {
	for _, candidate := range modelPostJSONRoutes {
		if path == candidate {
			return true
		}
	}
	for _, candidate := range modelPostFormRoutes {
		if path == candidate {
			return true
		}
	}
	for _, candidate := range modelGetRoutes {
		if path == candidate {
			return true
		}
	}
	return false
}

func tailcatModelAllowed(models []string, id string) bool {
	for _, exposed := range models {
		if exposed == "*" || exposed == id {
			return true
		}
	}
	return false
}

// CloseStreams cancels long-lived response streams (Server-Sent Events) so a
// graceful httpServer.Shutdown can drain without blocking on them. It does not
// tear down routers; call Shutdown for that. Safe to call repeatedly.
func (s *Server) CloseStreams() {
	s.shutdownFn()
}

// Shutdown stops the router and the tool registry. It is idempotent;
// repeated calls return nil without re-running shutdown.
//
// Callers must drain inflight HTTP requests (httpServer.Shutdown) before
// calling this, otherwise inflight requests 502 when their processes are torn
// down. Call CloseStreams before httpServer.Shutdown so SSE streams do not
// block the drain.
func (s *Server) Shutdown(timeout time.Duration) error {
	if !s.shuttingDown.CompareAndSwap(false, true) {
		return nil
	}
	s.shutdownFn()

	var wg sync.WaitGroup
	var mu sync.Mutex
	var errs []error

	// Server.Shutdown is a closed list: anything holding resources that is not
	// released here leaks. The tool registry is cheap today and expensive once
	// a provider holds upstream connections or subprocesses.
	if err := s.tools.Shutdown(timeout); err != nil {
		errs = append(errs, err)
	}

	for _, rt := range []router.Router{s.local} {
		if rt == nil {
			continue
		}
		wg.Add(1)
		go func(rt router.Router) {
			defer wg.Done()
			if err := rt.Shutdown(timeout); err != nil {
				mu.Lock()
				errs = append(errs, err)
				mu.Unlock()
			}
		}(rt)
	}

	wg.Wait()
	return errors.Join(errs...)
}

// Config returns the configuration this server was built from.
func (s *Server) Config() config.Config {
	return s.cfg
}

// RunningModels reports the state of every local process that is not stopped or
// shut down, keyed by model ID.
func (s *Server) RunningModels() map[string]process.ProcessState {
	if s.local == nil {
		return map[string]process.ProcessState{}
	}
	return s.local.RunningModels()
}

// Placements returns the GPUs the router chose for the named models, so a hot
// reload can give them to the replacement server. Empty for routers that do
// not place.
func (s *Server) Placements(models []string) map[string][]string {
	if c, ok := s.local.(router.PlacementCarrier); ok {
		return c.Placements(models)
	}
	return nil
}

// RestorePlacements records where models carried over a hot reload are running.
func (s *Server) RestorePlacements(placed map[string][]string) {
	if c, ok := s.local.(router.PlacementCarrier); ok && len(placed) > 0 {
		c.RestorePlacements(placed)
	}
}

// DetachRunning hands back the processes for the named running models without
// stopping them, so a replacement server can adopt them across a hot reload.
func (s *Server) DetachRunning(models []string) map[string]process.Process {
	if s.local == nil {
		return map[string]process.Process{}
	}
	return s.local.DetachRunning(models)
}

// Adopt registers processes that are already running so this server serves them
// instead of starting its own. Call it before the server is published.
func (s *Server) Adopt(models map[string]process.Process) {
	if s.local == nil || len(models) == 0 {
		return
	}
	s.local.Adopt(models)
}
