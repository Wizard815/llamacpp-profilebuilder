package router

import (
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"testing"
	"time"

	"github.com/Wizard815/Argos/internal/config"
	"github.com/Wizard815/Argos/internal/gpumem"
	"github.com/Wizard815/Argos/internal/logmon"
	"github.com/Wizard815/Argos/internal/process"
	"github.com/Wizard815/Argos/internal/router/scheduler"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// newTestPool builds a poolSwapper with a fixed budget, fixed sizes and a
// controllable clock.
func newTestPool(budget float64, sizes map[string]float64) (*poolSwapper, *time.Time) {
	now := time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC)
	p := &poolSwapper{
		logger:      logmon.NewWriter(io.Discard),
		budget:      budget,
		protect:     10 * time.Minute,
		persistent:  map[string]bool{},
		sizes:       sizes,
		pins:        map[string][]string{},
		spread:      map[string]bool{},
		even:        map[string]bool{},
		placed:      map[string][]string{},
		cpuFallback: map[string]bool{},
		onCPU:       map[string]bool{},
		cpuOnly:     map[string]bool{},
		busy:        map[string]int{},
		lastUsed:    map[string]time.Time{},
		loadTime:    map[string]time.Duration{},
	}
	p.now = func() time.Time { return now }
	return p, &now
}

// The reported problem: a big model fills most of the GPUs and a tiny model
// is requested. Before, the tiny model evicted the big one. It must now load
// alongside.
func TestPool_SmallModelLoadsNextToBigModel(t *testing.T) {
	p, _ := newTestPool(62, map[string]float64{"big": 50, "tiny": 1})
	p.OnServeDone("big")

	assert.Empty(t, p.EvictionFor("tiny", []string{"big"}))
}

func TestPool_EvictsOnlyWhatIsNeeded(t *testing.T) {
	p, now := newTestPool(62, map[string]float64{"a": 20, "b": 20, "c": 20, "new": 20})
	p.lastUsed["a"] = now.Add(-time.Hour)
	p.lastUsed["b"] = now.Add(-2 * time.Hour)
	p.lastUsed["c"] = now.Add(-3 * time.Hour)

	// 60 used + 20 needed > 62: one 20 GiB model must go, the longest idle.
	assert.Equal(t, []string{"c"}, p.EvictionFor("new", []string{"a", "b", "c"}))
}

func TestPool_ProtectedEvictedAfterIdle(t *testing.T) {
	p, now := newTestPool(40, map[string]float64{"recent": 20, "old": 20, "new": 20})
	p.lastUsed["recent"] = now.Add(-time.Minute)
	p.lastUsed["old"] = now.Add(-time.Hour)

	assert.Equal(t, []string{"old"}, p.EvictionFor("new", []string{"old", "recent"}))
}

func TestPool_BusyEvictedLast(t *testing.T) {
	p, now := newTestPool(40, map[string]float64{"busy": 20, "recent": 20, "new": 20})
	p.OnServeStart("busy")
	p.lastUsed["busy"] = now.Add(-time.Hour)
	p.lastUsed["recent"] = now.Add(-time.Minute)

	assert.Equal(t, []string{"recent"}, p.EvictionFor("new", []string{"busy", "recent"}))
}

func TestPool_PersistentNeverEvicted(t *testing.T) {
	p, _ := newTestPool(40, map[string]float64{"whisper": 2, "llm": 30, "new": 30})
	p.persistent["whisper"] = true

	assert.Equal(t, []string{"llm"}, p.EvictionFor("new", []string{"llm", "whisper"}))
}

// Two idle models of the same size: the one that took far longer to load is
// worth keeping.
func TestPool_PrefersCheapReload(t *testing.T) {
	p, now := newTestPool(40, map[string]float64{"slow": 20, "fast": 20, "new": 20})
	p.lastUsed["slow"] = now.Add(-time.Hour)
	p.lastUsed["fast"] = now.Add(-time.Hour)
	p.loadTime["slow"] = 5 * time.Minute
	p.loadTime["fast"] = 5 * time.Second

	assert.Equal(t, []string{"fast"}, p.EvictionFor("new", []string{"fast", "slow"}))
}

// Greedy order picks a small model first, but only the big one makes room.
// The small one is put back.
func TestPool_PutsBackUnneededEvictions(t *testing.T) {
	p, now := newTestPool(40, map[string]float64{"small": 4, "large": 30, "new": 30})
	p.lastUsed["small"] = now.Add(-2 * time.Hour)
	p.lastUsed["large"] = now.Add(-2 * time.Hour)

	assert.Equal(t, []string{"large"}, p.EvictionFor("new", []string{"large", "small"}))
}

// A model with no known size keeps the old behaviour: it runs alone.
func TestPool_UnknownSizeRunsAlone(t *testing.T) {
	p, _ := newTestPool(62, map[string]float64{"a": 4, "b": 4})

	assert.Equal(t, []string{"a", "b"}, p.EvictionFor("mystery", []string{"a", "b"}))
	assert.Equal(t, []string{"mystery"}, p.EvictionFor("a", []string{"mystery"}))
}

func TestPool_NothingRunning(t *testing.T) {
	p, _ := newTestPool(62, map[string]float64{"a": 100})
	assert.Empty(t, p.EvictionFor("a", nil))
}

func TestPool_ObserverTracksUsage(t *testing.T) {
	p, _ := newTestPool(62, map[string]float64{"a": 4, "b": 4})
	p.OnServeStart("a")
	p.OnServeStart("a")
	assert.Equal(t, 2, p.busy["a"])
	p.OnServeDone("a")
	p.OnServeDone("a")
	assert.NotContains(t, p.busy, "a")

	p.OnSwapDone("a", 12*time.Second, nil)
	assert.Equal(t, 12*time.Second, p.reloadCost("a"))
	assert.Equal(t, 4*time.Second, p.reloadCost("b"), "a never-loaded model costs 1s per GiB")
}

func TestPool_DetectVRAM(t *testing.T) {
	root := t.TempDir()
	for _, card := range []string{"card0", "card1"} {
		dir := filepath.Join(root, card, "device")
		require.NoError(t, os.MkdirAll(dir, 0o755))
		require.NoError(t, os.WriteFile(filepath.Join(dir, "mem_info_vram_total"), []byte("34359738368\n"), 0o644))
	}
	// A connector entry must be ignored.
	require.NoError(t, os.MkdirAll(filepath.Join(root, "card0-DP-1", "device"), 0o755))

	assert.Equal(t, 64.0, detectVRAMGiB(root))
	assert.Equal(t, 0.0, detectVRAMGiB(filepath.Join(root, "missing")))
}

func TestPool_ModelSizeFromFile(t *testing.T) {
	dir := t.TempDir()
	single := filepath.Join(dir, "m.gguf")
	require.NoError(t, os.WriteFile(single, make([]byte, 1000), 0o644))
	for i := 1; i <= 3; i++ {
		name := filepath.Join(dir, "big-0000"+string(rune('0'+i))+"-of-00003.gguf")
		require.NoError(t, os.WriteFile(name, make([]byte, 500), 0o644))
	}

	assert.Equal(t, int64(1000), modelFileBytes([]string{"llama-server", "-m", single}))
	assert.Equal(t, int64(1000), modelFileBytes([]string{"llama-server", "--model=" + single}))
	assert.Equal(t, int64(1500), modelFileBytes([]string{"llama-server", "-m", filepath.Join(dir, "big-00001-of-00003.gguf")}))
	assert.Equal(t, int64(0), modelFileBytes([]string{"whisper-server"}))

	assert.Equal(t, 7.5, modelVRAMGiB(config.ModelConfig{VRAM: 7.5}))
}

// newTestPoolRouter builds a Pool router from supplied processes and a
// prepared swapper, bypassing NewPool's call to process.New.
func newTestPoolRouter(t *testing.T, swapper *poolSwapper, processes map[string]process.Process) *Pool {
	t.Helper()
	conf := config.Config{HealthCheckTimeout: 5}
	conf.Routing.Router.Use = "pool"
	base, err := newBaseRouter("pool", conf, processes, logmon.NewWriter(io.Discard), swapper)
	require.NoError(t, err)
	r := &Pool{baseRouter: base}
	go base.run()
	t.Cleanup(func() {
		if !r.shuttingDown.Load() {
			_ = r.Shutdown(time.Second)
		}
	})
	return r
}

// End to end through the scheduler: a running big model is left alone when a
// tiny model fits next to it.
func TestPool_RouterKeepsBigModelLoaded(t *testing.T) {
	big := newFakeProcess("big")
	big.markReady()
	go big.Run(0)
	tiny := newFakeProcess("tiny")
	tiny.autoReady = true

	swapper, _ := newTestPool(62, map[string]float64{"big": 50, "tiny": 1})
	r := newTestPoolRouter(t, swapper, map[string]process.Process{"big": big, "tiny": tiny})

	w := httptest.NewRecorder()
	r.ServeHTTP(w, newRequest("tiny"))

	require.Equal(t, http.StatusOK, w.Code, w.Body.String())
	assert.Equal(t, int32(0), big.stopCalls.Load(), "big must stay loaded")
	assert.Equal(t, int32(1), tiny.runCalls.Load())
}

// End to end: when the new model does not fit, only the idle model that is
// cheapest to lose is stopped.
func TestPool_RouterEvictsWhenFull(t *testing.T) {
	a := newFakeProcess("a")
	a.markReady()
	go a.Run(0)
	b := newFakeProcess("b")
	b.markReady()
	go b.Run(0)
	c := newFakeProcess("c")
	c.autoReady = true

	swapper, now := newTestPool(40, map[string]float64{"a": 20, "b": 20, "c": 20})
	swapper.lastUsed["a"] = now.Add(-time.Minute) // protected
	swapper.lastUsed["b"] = now.Add(-time.Hour)

	r := newTestPoolRouter(t, swapper, map[string]process.Process{"a": a, "b": b, "c": c})

	w := httptest.NewRecorder()
	r.ServeHTTP(w, newRequest("c"))

	require.Equal(t, http.StatusOK, w.Code, w.Body.String())
	assert.Equal(t, int32(0), a.stopCalls.Load(), "recently used a must stay")
	assert.Equal(t, int32(1), b.stopCalls.Load(), "idle b makes room")
	assert.Equal(t, int32(1), c.runCalls.Load())
}

// newTestCardPool is a pool over cards of the given GiB. Each card's cap is its
// size, which keeps the numbers in tests easy to read.
func newTestCardPool(caps []float64, sizes map[string]float64) (*poolSwapper, *time.Time) {
	var total float64
	for _, c := range caps {
		total += c
	}
	p, now := newTestPool(total, sizes)
	for i, c := range caps {
		p.cards = append(p.cards, poolCard{id: strconv.Itoa(i), cap: c})
	}
	return p, now
}

// start commits a swap the way the scheduler does, then marks it running.
func start(p *poolSwapper, target string, running ...string) []string {
	p.OnSwapStart(target, append(slices.Clone(running), target))
	return p.placement(target)
}

func TestPool_PlacesModelsOnSeparateCards(t *testing.T) {
	p, _ := newTestCardPool([]float64{31, 31}, map[string]float64{"a": 26, "s": 6, "b": 20})

	assert.Equal(t, []string{"0"}, start(p, "a"))
	assert.Equal(t, []string{"1"}, start(p, "s", "a"), "the empty card wins over sharing with a")
	assert.Equal(t, []string{"1"}, start(p, "b", "a", "s"), "b only fits next to s")
	assert.Empty(t, p.EvictionFor("b", []string{"a", "s"}))
}

func TestPool_SpreadsModelBiggerThanACard(t *testing.T) {
	p, _ := newTestCardPool([]float64{31, 31}, map[string]float64{"huge": 50, "tiny": 1})

	assert.Equal(t, []string{"0", "1"}, start(p, "huge"))
	assert.Empty(t, p.EvictionFor("tiny", []string{"huge"}), "25 GiB per card leaves room")
	assert.Equal(t, []string{"0"}, start(p, "tiny", "huge") /* tie: card 0 */)
}

// The pool has room in total but no card has room for the model: the fit test
// must fail and evict until one card does.
func TestPool_FragmentationEvictsForOneCard(t *testing.T) {
	p, now := newTestCardPool([]float64{31, 31}, map[string]float64{"a": 20, "b": 20, "c": 28})
	start(p, "a")
	start(p, "b", "a")
	p.lastUsed["a"] = now.Add(-2 * time.Hour)
	p.lastUsed["b"] = now.Add(-time.Hour)

	// 22 GiB is free overall, but only 11 per card.
	assert.Equal(t, []string{"a"}, p.EvictionFor("c", []string{"a", "b"}))
}

func TestPool_PlacementFollowsTheEvictionPlan(t *testing.T) {
	p, now := newTestCardPool([]float64{31, 31}, map[string]float64{"a": 20, "b": 20, "c": 28})
	start(p, "a")
	start(p, "b", "a")
	p.lastUsed["a"] = now.Add(-2 * time.Hour)
	p.lastUsed["b"] = now.Add(-time.Hour)

	assert.Equal(t, []string{"0"}, start(p, "c", "a", "b"), "c takes the card a freed")
	assert.NotContains(t, p.placed, "a", "an evicted model's placement is forgotten")
}

func TestPool_HonoursHIPPin(t *testing.T) {
	p, _ := newTestCardPool([]float64{31, 31}, map[string]float64{"a": 10, "pinned": 10})
	p.pins["pinned"] = []string{"1"}
	start(p, "a")

	// Card 1 is the emptier card anyway; pin the model to the busier one.
	p.pins["pinned"] = []string{"0"}
	assert.Equal(t, []string{"0"}, start(p, "pinned", "a"))
}

func TestPool_PinnedCardFullEvicts(t *testing.T) {
	p, now := newTestCardPool([]float64{31, 31}, map[string]float64{"a": 26, "pinned": 20})
	start(p, "a")
	p.lastUsed["a"] = now.Add(-time.Hour)
	p.pins["pinned"] = []string{"0"}

	assert.Equal(t, []string{"a"}, p.EvictionFor("pinned", []string{"a"}), "card 1 is empty but the pin says card 0")
}

func TestPool_UnknownSizeTakesAllCards(t *testing.T) {
	p, _ := newTestCardPool([]float64{31, 31}, map[string]float64{"a": 4, "mystery": 0})
	start(p, "a")

	assert.Equal(t, []string{"a"}, p.EvictionFor("mystery", []string{"a"}))
	assert.Equal(t, []string{"0", "1"}, start(p, "mystery"))
	assert.Equal(t, []string{"mystery"}, p.EvictionFor("a", []string{"mystery"}))
}

func TestPool_NoCardsNoPlacement(t *testing.T) {
	p, _ := newTestPool(62, map[string]float64{"a": 4})
	p.OnSwapStart("a", []string{"a"})

	assert.Nil(t, p.placement("a"))
}

func TestPool_NewSwapperFromCardConfig(t *testing.T) {
	conf := config.Config{Models: map[string]config.ModelConfig{
		"a": {VRAM: 10, Env: []string{"HIP_VISIBLE_DEVICES=1"}},
	}}
	conf.Routing.Router.Settings.Pool = config.PoolConfig{Cards: []float64{32, 32}, ReserveGiB: 2, ProtectSeconds: 60, Placement: "auto"}
	detect := func() ([]gpumem.Card, string) {
		t.Fatal("detect must not run when cards are configured")
		return nil, ""
	}

	p := newPoolSwapper(conf, logmon.NewWriter(io.Discard), detect)

	require.Len(t, p.cards, 2)
	assert.Equal(t, 31.0, p.cards[0].cap, "each card gives up half the 2 GiB reserve")
	assert.Equal(t, 62.0, p.budget)
	assert.Equal(t, []string{"1"}, p.pins["a"])
}

func TestPool_NewSwapperDetectsCards(t *testing.T) {
	conf := config.Config{Models: map[string]config.ModelConfig{"a": {VRAM: 10}}}
	conf.Routing.Router.Settings.Pool = config.PoolConfig{ReserveGiB: 2, ProtectSeconds: 60, Placement: "auto"}
	detect := func() ([]gpumem.Card, string) {
		return []gpumem.Card{{ID: "0", TotalGiB: 31.98}, {ID: "1", TotalGiB: 31.98}}, "rocm-smi"
	}

	p := newPoolSwapper(conf, logmon.NewWriter(io.Discard), detect)

	require.Len(t, p.cards, 2)
	assert.InDelta(t, 61.96, p.budget, 0.01)
}

func TestPool_PlacementOffAndFixedVRAMDoNotPlace(t *testing.T) {
	for name, pc := range map[string]config.PoolConfig{
		"off":        {Placement: "off", ReserveGiB: 2, ProtectSeconds: 60},
		"fixed vram": {Placement: "auto", VRAMGiB: 64, ReserveGiB: 2, ProtectSeconds: 60},
	} {
		t.Run(name, func(t *testing.T) {
			conf := config.Config{Models: map[string]config.ModelConfig{"a": {VRAM: 10}}}
			conf.Routing.Router.Settings.Pool = pc
			detect := func() ([]gpumem.Card, string) { t.Fatal("detect must not run"); return nil, "" }

			p := newPoolSwapper(conf, logmon.NewWriter(io.Discard), detect)
			assert.Empty(t, p.cards)
		})
	}
}

func TestPool_RouterExposesPlacement(t *testing.T) {
	a := newFakeProcess("a")
	a.autoReady = true
	swapper, _ := newTestCardPool([]float64{31, 31}, map[string]float64{"a": 10})
	r := newTestPoolRouter(t, swapper, map[string]process.Process{"a": a})
	pool := &Pool{baseRouter: r.baseRouter, swapper: swapper}

	w := httptest.NewRecorder()
	pool.ServeHTTP(w, newRequest("a"))

	require.Equal(t, http.StatusOK, w.Code, w.Body.String())
	var placer Placer = pool
	assert.Equal(t, []string{"0"}, placer.Placement("a"))
	assert.Nil(t, placer.Placement("unknown"))
}

func TestPool_StatusReportsCardsModelsAndQueue(t *testing.T) {
	p, now := newTestCardPool([]float64{31, 31}, map[string]float64{"a": 26, "s": 6, "off": 4, "mystery": 0})
	p.persistent["s"] = true
	start(p, "a")
	start(p, "s", "a")
	p.OnServeStart("a")
	p.OnSwapDone("s", 8*time.Second, nil)
	p.lastUsed["a"] = now.Add(-time.Hour)

	running := map[string]process.ProcessState{"a": process.StateReady, "s": process.StateStarting}
	sched := scheduler.Status{
		Queued:   []scheduler.QueuedStatus{{Model: "off", Position: 1, Waited: 5 * time.Second}},
		Swaps:    []scheduler.SwapStatus{{Model: "s", Waiters: 2, Elapsed: time.Second}},
		InFlight: map[string]int{"a": 1},
	}
	st := p.status(running, sched)

	assert.True(t, st.Placement)
	require.Len(t, st.Cards, 2)
	assert.Equal(t, 26.0, st.Cards[0].UsedGiB)
	assert.Equal(t, []string{"a"}, st.Cards[0].Models)
	assert.Equal(t, 25.0, st.Cards[1].FreeGiB, "s holds 6 of card 1's 31")

	byID := map[string]PoolModelStatus{}
	for _, m := range st.Models {
		byID[m.ID] = m
	}
	require.Len(t, byID, 4, "every configured model is listed, running or not")
	assert.Equal(t, "ready", byID["a"].State)
	assert.Equal(t, []string{"0"}, byID["a"].Cards)
	assert.Equal(t, 1, byID["a"].InFlight)
	assert.False(t, byID["a"].Protected, "a was last used an hour ago")
	assert.True(t, byID["s"].Persistent)
	assert.Equal(t, 8.0, byID["s"].LoadSeconds)
	assert.Equal(t, "stopped", byID["off"].State)
	assert.Empty(t, byID["off"].Cards)
	assert.False(t, byID["mystery"].SizeKnown)

	require.Len(t, st.Queue, 1)
	assert.Equal(t, "off", st.Queue[0].Model)
	assert.Equal(t, 5.0, st.Queue[0].WaitedSeconds)
	require.Len(t, st.Swaps, 1)
	assert.Empty(t, st.Swaps[0].Evict, "evict is [] not null in the JSON")
}

func TestPool_SizesMapSuppliesVRAM(t *testing.T) {
	conf := config.Config{Models: map[string]config.ModelConfig{
		"explicit": {VRAM: 5},
		"mapped":   {},
	}}
	conf.Routing.Router.Settings.Pool = config.PoolConfig{
		Placement: "off", VRAMGiB: 64, ReserveGiB: 2, ProtectSeconds: 60,
		Sizes: map[string]float64{"explicit": 99, "mapped": 12},
	}

	p := newPoolSwapper(conf, logmon.NewWriter(io.Discard), func() ([]gpumem.Card, string) { return nil, "" })

	assert.Equal(t, 5.0, p.sizes["explicit"], "models.*.vram beats pool.sizes")
	assert.Equal(t, 12.0, p.sizes["mapped"])
}

func TestPool_RouterReportsStatusThroughTheRunLoop(t *testing.T) {
	a := newFakeProcess("a")
	a.autoReady = true
	swapper, _ := newTestCardPool([]float64{31, 31}, map[string]float64{"a": 10})
	r := newTestPoolRouter(t, swapper, map[string]process.Process{"a": a})
	pool := &Pool{baseRouter: r.baseRouter, swapper: swapper}

	w := httptest.NewRecorder()
	pool.ServeHTTP(w, newRequest("a"))
	require.Equal(t, http.StatusOK, w.Code, w.Body.String())

	var reporter PoolReporter = pool
	st := reporter.PoolStatus()
	require.Len(t, st.Models, 1)
	assert.Equal(t, "a", st.Models[0].ID)
	assert.Equal(t, []string{"0"}, st.Models[0].Cards)
	assert.Empty(t, st.Queue)
}

// With placement off the cards still size the pool, but models are not placed.
func TestPool_PlacementOffKeepsCardTotals(t *testing.T) {
	conf := config.Config{Models: map[string]config.ModelConfig{"a": {VRAM: 10}}}
	conf.Routing.Router.Settings.Pool = config.PoolConfig{Cards: []float64{32, 32}, Placement: "off", ReserveGiB: 2, ProtectSeconds: 60}

	p := newPoolSwapper(conf, logmon.NewWriter(io.Discard), func() ([]gpumem.Card, string) { return nil, "" })

	assert.Empty(t, p.cards, "no per-card placement")
	assert.Equal(t, 62.0, p.budget, "the cards still set the budget")
	p.OnSwapStart("a", []string{"a"})
	assert.Nil(t, p.placement("a"))
}

// A reload builds a new pool and carries the running models across. Their
// cards must come with them: forgotten, the pool plans each as if spread over
// every card and shows (and fills) the wrong ones.
func TestPool_PlacementSurvivesReload(t *testing.T) {
	old, _ := newTestPool(62, map[string]float64{"big": 24})
	old.cards = []poolCard{{id: "0", cap: 31}, {id: "1", cap: 31}}
	old.OnSwapStart("big", nil)
	require.Equal(t, []string{"0"}, old.placement("big"))

	next, _ := newTestPool(62, map[string]float64{"big": 24})
	next.cards = []poolCard{{id: "0", cap: 31}, {id: "1", cap: 31}}
	next.restore(map[string][]string{"big": old.placement("big"), "gone": {"7"}})

	assert.Equal(t, []string{"0"}, next.placement("big"))
	assert.Empty(t, next.placement("gone"), "a card the new pool does not have is dropped")
	// the second card is still free for another 24 GiB model
	ok, cards := next.fits("other", []string{"big"})
	assert.False(t, ok && len(cards) == 0)
}

// The live failure, with the live numbers: two 31 GiB cards, 9.06 GiB already
// on card 0, and a 44.61 GiB model. It fits in neither card alone but 52.94 GiB
// is free across the two, so it must be placed on both and start.
func TestPool_AModelTooBigForOneCardFitsAcrossTwo(t *testing.T) {
	p, _ := newTestCardPool([]float64{31, 31}, map[string]float64{"small": 9.06, "hermes": 44.61})
	assert.Equal(t, []string{"0"}, start(p, "small"))

	ok, cards := p.fits("hermes", []string{"small"})
	assert.True(t, ok, "44.61 GiB fits in the 52.94 GiB free across both cards")
	assert.Equal(t, []string{"0", "1"}, cards)
}

// The same rule seen from the eviction side: the pool must not report that it
// needs to free a card to fit a model that already fits across both.
func TestPool_NoEvictionForAModelThatFitsAcrossTwo(t *testing.T) {
	p, _ := newTestCardPool([]float64{31, 31}, map[string]float64{"small": 9.06, "hermes": 44.61})
	start(p, "small")

	assert.Empty(t, p.EvictionFor("hermes", []string{"small"}))
}

func TestPool_TensorSplitUsesEveryCard(t *testing.T) {
	p, _ := newTestCardPool([]float64{31, 31}, map[string]float64{"small": 7, "other": 4})
	p.spread["small"] = true

	assert.Equal(t, []string{"0", "1"}, start(p, "small"), "7 GiB fits one card, but a tensor split wants both")
	assert.Equal(t, []string{"0"}, start(p, "other", "small"), "a plain model still takes one card")
}

// --split-mode tensor divides the model evenly, so every card it uses has to
// hold its exact share - whatever the others have free.
func TestPool_TensorSpreadEvictsUntilEveryCardHasItsShare(t *testing.T) {
	p, now := newTestCardPool([]float64{31, 31}, map[string]float64{"a": 28, "wide": 20})
	start(p, "a")
	p.lastUsed["a"] = now.Add(-time.Hour)
	p.spread["wide"] = true
	p.even["wide"] = true

	assert.Equal(t, []string{"a"}, p.EvictionFor("wide", []string{"a"}), "card 1 has room for all of it, but tensor takes half on card 0")
}

// layer and row split in proportion to each card's free memory, so the same
// model needs the two cards together to hold it - and card 1 can carry most of
// it while card 0 keeps its few free GiB. Refusing this evicted a running model
// for room that was not needed.
func TestPool_LayerSpreadFitsOnBothIfTogetherTheyHoldIt(t *testing.T) {
	p, now := newTestCardPool([]float64{31, 31}, map[string]float64{"a": 28, "wide": 20})
	start(p, "a")
	p.lastUsed["a"] = now.Add(-time.Hour)
	p.spread["wide"] = true // not even: layer or row

	assert.Empty(t, p.EvictionFor("wide", []string{"a"}),
		"3 GiB free on card 0 and 31 on card 1 holds a 20 GiB model between them")
}

func TestPool_SpreadFromTheModelConfig(t *testing.T) {
	conf := config.Config{Models: map[string]config.ModelConfig{
		"tensor": {VRAM: 7, Cmd: "llama-server -m /m/a.gguf --split-mode tensor -ngl 99"},
		"row":    {VRAM: 7, Cmd: "llama-server -m /m/a.gguf -sm row"},
		"layer":  {VRAM: 7, Cmd: "llama-server -m /m/a.gguf --split-mode layer"},
		"pinned": {VRAM: 7, Cmd: "llama-server -m /m/a.gguf", Env: []string{"HIP_VISIBLE_DEVICES=0,1"}},
		"one":    {VRAM: 7, Cmd: "llama-server -m /m/a.gguf", Env: []string{"HIP_VISIBLE_DEVICES=1"}},
	}}
	conf.Routing.Router.Settings.Pool = config.PoolConfig{Cards: []float64{32, 32}, Placement: "auto"}
	p := newPoolSwapper(conf, logmon.NewWriter(io.Discard), func() ([]gpumem.Card, string) { return nil, "" })

	assert.True(t, p.spread["tensor"])
	assert.True(t, p.spread["row"])
	assert.False(t, p.spread["layer"], "layer is llama-server's default; one card is still fine")
	assert.True(t, p.spread["pinned"], "a pin to two cards means both")
	assert.False(t, p.spread["one"])
}

// The assistant's small model falls back to the CPU: with a big model filling
// the GPUs it starts on the CPU instead of evicting it, holds no VRAM there,
// and goes back to the GPU on a later load that fits.
func TestPool_CPUFallbackRunsOnTheCPUInsteadOfEvicting(t *testing.T) {
	p, _ := newTestPool(40, map[string]float64{"big": 36, "assistant": 6, "other": 30})
	p.cpuFallback["assistant"] = true

	assert.Empty(t, p.EvictionFor("assistant", []string{"big"}), "it never pushes the big model off")
	p.OnSwapStart("assistant", []string{"big"})
	assert.True(t, p.runsOnCPU("assistant"))
	assert.Zero(t, p.size("assistant"), "on the CPU it holds no VRAM")
	assert.Equal(t, []string{"big"}, p.EvictionFor("other", []string{"big", "assistant"}),
		"another model makes room by the big one, not the CPU one that frees nothing")

	p.OnSwapStart("assistant", nil) // later, alone: it fits
	assert.False(t, p.runsOnCPU("assistant"))
	assert.Equal(t, 6.0, p.size("assistant"))
}

// The Builder's "CPU only" (GPU variable -1) or -ngl 0: the model holds no
// VRAM, so it never evicts anything and never counts against the cards.
func TestPool_CPUOnlyModelTakesNoRoom(t *testing.T) {
	assert.True(t, cpuOnly("llama-server -m x.gguf -ngl 0", nil))
	assert.True(t, cpuOnly("llama-server -m x.gguf --n-gpu-layers=0", nil))
	assert.True(t, cpuOnly("llama-server -m x.gguf -ngl 99", []string{"HIP_VISIBLE_DEVICES=-1"}))
	assert.False(t, cpuOnly("llama-server -m x.gguf -ngl 99 --port 10", []string{"HIP_VISIBLE_DEVICES=0"}))

	p, _ := newTestPool(40, map[string]float64{"big": 38, "cpu": 20})
	p.cpuOnly["cpu"] = true
	assert.Empty(t, p.EvictionFor("cpu", []string{"big"}))
	p.OnSwapStart("cpu", []string{"big"})
	assert.True(t, p.runsOnCPU("cpu"))
	assert.Zero(t, p.size("cpu"))
}

// A model on the CPU holds no VRAM, so it is never unloaded to make room on
// the GPUs: not even when nothing frees enough and the pool frees all it can.
func TestPool_CPUModelsAreNeverEvictedForAGPUModel(t *testing.T) {
	p, _ := newTestPool(62, map[string]float64{"coder": 62.7, "embed": 0.7, "chat": 0.8, "old": 10})
	p.cpuOnly["embed"] = true
	p.onCPU["chat"] = true
	evict := p.EvictionFor("coder", []string{"embed", "chat", "old"})
	assert.Equal(t, []string{"old"}, evict, "too big to fit even alone: everything on the GPUs goes, the CPU models stay")
	assert.Empty(t, p.EvictionFor("coder", []string{"embed", "chat"}))
}
