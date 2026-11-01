package router

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/Wizard815/Argos/internal/config"
	"github.com/Wizard815/Argos/internal/gpumem"
	"github.com/Wizard815/Argos/internal/logmon"
	"github.com/Wizard815/Argos/internal/process"
)

// Placer is implemented by routers that choose which GPUs a model runs on.
// Placement returns the HIP_VISIBLE_DEVICES ids picked for a model about to
// start, or nil when the router has no opinion.
type Placer interface {
	Placement(modelID string) []string
}

// Pool is a router where every model belongs to one shared pool. Models load
// side by side while their VRAM fits; a model is evicted only when a new one
// does not fit, and then the cheapest, least recently used, idle model goes
// first. When per-card sizes are known each model is also placed on the card
// (or cards) with room, so the cards work independently.
type Pool struct {
	*baseRouter
	swapper *poolSwapper
}

func NewPool(conf config.Config, proxylog, upstreamlog *logmon.Monitor) (*Pool, error) {
	swapper := newPoolSwapper(conf, proxylog, gpumem.Detect)

	processes := make(map[string]process.Process, len(conf.Models))
	base, err := newBaseRouter("pool", conf, processes, proxylog, swapper)
	if err != nil {
		return nil, fmt.Errorf("creating base router: %w", err)
	}

	for mid, modelCfg := range conf.Models {
		procLog := logmon.NewWriter(Prefills.Tap(mid, upstreamlog))
		p, err := process.New(base.procCtx, mid, modelCfg, procLog, proxylog)
		if err != nil {
			base.shutdownFn()
			base.procCancel()
			return nil, fmt.Errorf("creating process for %q: %w", mid, err)
		}
		processes[mid] = p
	}

	r := &Pool{baseRouter: base, swapper: swapper}
	go base.run()
	go r.watchOutside(base.shutdownCtx)
	return r, nil
}

// Placement implements Placer.
func (r *Pool) Placement(modelID string) []string {
	return r.swapper.placement(modelID)
}

// OnCPU reports whether the pool started the model on the CPU because it did
// not fit on the GPUs (routing.router.settings.pool.cpuFallback).
func (r *Pool) OnCPU(modelID string) bool {
	return r.swapper.runsOnCPU(modelID)
}

// PlacementCarrier is implemented by routers whose placement has to survive a
// hot reload. A reload builds a new router and carries the running children
// across; without this the new pool knows nothing about where they are and
// plans as if each one were spread over every card.
type PlacementCarrier interface {
	Placements(ids []string) map[string][]string
	RestorePlacements(placed map[string][]string)
}

// Placements implements PlacementCarrier.
func (r *Pool) Placements(ids []string) map[string][]string {
	out := make(map[string][]string, len(ids))
	for _, id := range ids {
		if cards := r.swapper.placement(id); len(cards) > 0 {
			out[id] = cards
		}
	}
	return out
}

// RestorePlacements implements PlacementCarrier. Cards this pool does not have
// are dropped: the card list can change in the same reload.
func (r *Pool) RestorePlacements(placed map[string][]string) {
	r.swapper.restore(placed)
}

func (p *poolSwapper) restore(placed map[string][]string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if len(p.cards) == 0 {
		return
	}
	have := make(map[string]bool, len(p.cards))
	for _, c := range p.cards {
		have[c.id] = true
	}
	for id, cards := range placed {
		var keep []string
		for _, c := range cards {
			if have[c] {
				keep = append(keep, c)
			}
		}
		if len(keep) > 0 {
			p.placed[id] = keep
		}
	}
}

// poolCard is one GPU in the pool.
type poolCard struct {
	id  string
	cap float64 // GiB a model may use: the card's VRAM minus its share of the reserve
}

// poolSwapper is the pool's eviction and placement policy. It implements
// scheduler.UsageObserver so it knows which models are busy, when each was
// last used and how long each took to load.
//
// The scheduler drives it from the run-loop goroutine, but process launch and
// the status API read it from other goroutines, so mu guards busy, lastUsed,
// loadTime and placed. Methods named ...Locked, and the helpers they call,
// expect mu to be held; none of them take it again, because a nested RLock
// can deadlock behind a waiting writer.
type poolSwapper struct {
	logger     *logmon.Monitor
	budget     float64 // usable GiB across the pool: VRAM minus the reserve
	protect    time.Duration
	persistent map[string]bool
	sizes      map[string]float64 // GiB; 0 means unknown
	pins       map[string][]string
	spread     map[string]bool // placed on every allowed card, not on the one with room
	even       map[string]bool // ...and divided into equal shares, as --split-mode tensor does

	// cards is empty when per-card sizes are unknown; the pool then plans by
	// budget alone and never places.
	cards []poolCard

	mu     sync.RWMutex
	placed map[string][]string // model -> card ids chosen at its last swap

	// cpuFallback models run on the CPU when they do not fit beside what is
	// loaded, instead of evicting it; onCPU is the ones running that way now.
	cpuFallback map[string]bool
	onCPU       map[string]bool
	cpuOnly     map[string]bool // set to run on the CPU by its own command (CPU only, -ngl 0)

	// outside is the VRAM per card held by something other than Argos's
	// running models (another app), read live; see outside.go.
	outside      map[string]float64
	outsideHist  map[string][]float64
	reserveShare float64 // each card's part of reserveGiB, which covers the first of it

	busy     map[string]int
	lastUsed map[string]time.Time
	// protectWaits: a model used within protect is not unloaded for another
	// model's request; that request waits (scheduler.StaysLoaded)
	protectWaits bool
	loadTime     map[string]time.Duration

	now func() time.Time
}

// newPoolSwapper builds the swapper from the pool config. detect lists the
// installed cards; it is consulted only when placement is on and the config
// gives no card sizes.
func newPoolSwapper(conf config.Config, logger *logmon.Monitor, detect func() ([]gpumem.Card, string)) *poolSwapper {
	pc := conf.Routing.Router.Settings.Pool

	var cards []gpumem.Card
	switch {
	case len(pc.Cards) > 0:
		for i, g := range pc.Cards {
			cards = append(cards, gpumem.Card{ID: strconv.Itoa(i), TotalGiB: g})
		}
	case pc.Placement != "off" && pc.VRAMGiB == 0:
		var source string
		cards, source = detect()
		if source == "sysfs" {
			logger.Warnf("pool: rocm-smi is unavailable, so cards are numbered 0..n-1 in /sys/class/drm order, which is HIP's usual order. Set routing.router.settings.pool.cards to be sure")
		}
	}

	total := pc.VRAMGiB
	if len(cards) > 0 {
		total = 0
		for _, c := range cards {
			total += c.TotalGiB
		}
	}
	if total == 0 {
		total = detectVRAMGiB(drmSysfsRoot)
	}
	if total > 0 {
		logger.Infof("pool: %.1f GiB of VRAM across %d card(s), %.1f GiB reserved", total, len(cards), pc.ReserveGiB)
	} else {
		logger.Warnf("pool: could not detect VRAM; set routing.router.settings.pool.vramGiB. Until then every model runs alone")
	}

	p := &poolSwapper{
		logger:      logger,
		budget:      total - pc.ReserveGiB,
		protect:     time.Duration(pc.ProtectSeconds) * time.Second,
		persistent:  make(map[string]bool, len(pc.Persistent)),
		sizes:       make(map[string]float64, len(conf.Models)),
		pins:        make(map[string][]string),
		spread:      make(map[string]bool),
		even:        make(map[string]bool),
		placed:      make(map[string][]string),
		cpuFallback: make(map[string]bool, len(pc.CPUFallback)),
		onCPU:       make(map[string]bool),
		cpuOnly:     make(map[string]bool),
		outside:     make(map[string]float64),
		outsideHist: make(map[string][]float64),
		busy:        make(map[string]int),
		lastUsed:    make(map[string]time.Time),
		loadTime:    make(map[string]time.Duration),
		now:         time.Now,
	}
	p.protectWaits = pc.ProtectWaits == nil || *pc.ProtectWaits
	if pc.Placement != "off" {
		for _, c := range cards {
			p.cards = append(p.cards, poolCard{id: c.ID, cap: max(c.TotalGiB-pc.ReserveGiB/float64(len(cards)), 0)})
		}
		if len(cards) > 0 {
			p.reserveShare = pc.ReserveGiB / float64(len(cards))
		}
		lastOutsideMu.Lock()
		for k, v := range lastOutside {
			p.outside[k] = v
		}
		lastOutsideMu.Unlock()
	}
	for _, id := range pc.Persistent {
		p.persistent[id] = true
	}
	for _, id := range pc.CPUFallback {
		p.cpuFallback[id] = true
	}
	for id, mc := range conf.Models {
		p.sizes[id] = modelVRAMGiB(mc)
		if mc.VRAM <= 0 && pc.Sizes[id] > 0 {
			p.sizes[id] = pc.Sizes[id]
		}
		if p.sizes[id] == 0 {
			logger.Warnf("pool: unknown VRAM size for %s; set its vram to let it share the GPUs", id)
		}
		if cpuOnly(mc.Cmd, mc.Env) {
			p.cpuOnly[id] = true
			continue // no VRAM, no cards
		}
		pin := hipPin(mc.Env)
		if len(pin) > 0 {
			p.pins[id] = pin
		}
		// Pinned to several cards, or told to split a tensor or row across GPUs: the
		// model is meant to use more than one card, so one with room is not enough.
		if len(pin) > 1 || splitsAcrossCards(mc.Cmd) {
			p.spread[id] = true
			if splitsEvenly(mc.Cmd) {
				p.even[id] = true
			}
		}
	}
	return p
}

// splitAcross matches -sm / --split-mode tensor or row: the modes that share
// each layer across GPUs. layer (llama-server's default) and none do not ask
// for more than one card, so the pool may still fit those on one.
var splitAcross = regexp.MustCompile(`(?:^|\s)(?:-sm|--split-mode)(?:\s+|=)(?:tensor|row)(?:\s|$)`)

func splitsAcrossCards(cmd string) bool {
	return splitAcross.MatchString(cmd)
}

// splitEven matches --split-mode tensor, the one mode that divides the model
// into equal shares whatever memory each card has. llama.cpp's docs: with no
// --tensor-split, "layer/row use automatic splitting proportional to memory,
// while tensor splits tensor segments evenly". So tensor needs an equal share
// free on every card; layer and row need the cards' free space together to
// cover the model, and llama.cpp weighs them when it picks the cut points.
var splitEven = regexp.MustCompile(`(?:^|\s)(?:-sm|--split-mode)(?:\s+|=)tensor(?:\s|$)`)

func splitsEvenly(cmd string) bool {
	return splitEven.MatchString(cmd)
}

// totalFree is the free space the cards have together.
func totalFree(cards []poolCard, free func(poolCard) float64) float64 {
	total := 0.0
	for _, c := range cards {
		total += free(c)
	}
	return total
}

// noGPULayers matches -ngl 0 / --n-gpu-layers 0: every layer stays on the CPU.
var noGPULayers = regexp.MustCompile(`(?:^|\s)(?:-ngl|--n-gpu-layers|--gpu-layers)(?:\s+|=)0(?:\s|$)`)

// cpuOnly reports whether a model is set to run on the CPU: no GPU visible to
// it (HIP_ or CUDA_VISIBLE_DEVICES=-1, the Builder's "CPU only") or no layer
// on the GPU (-ngl 0). Such a model holds no VRAM and is never placed.
func cpuOnly(cmd string, env []string) bool {
	if noGPULayers.MatchString(cmd) {
		return true
	}
	for _, e := range env {
		for _, k := range []string{"HIP_VISIBLE_DEVICES=", "CUDA_VISIBLE_DEVICES="} {
			if v, ok := strings.CutPrefix(e, k); ok && strings.TrimSpace(v) == "-1" {
				return true
			}
		}
	}
	return false
}

// hipPin returns the card ids a model's env pins with HIP_VISIBLE_DEVICES.
func hipPin(env []string) []string {
	for _, e := range env {
		if v, ok := strings.CutPrefix(e, "HIP_VISIBLE_DEVICES="); ok {
			var ids []string
			for _, id := range strings.Split(v, ",") {
				if id = strings.TrimSpace(id); id != "" {
					ids = append(ids, id)
				}
			}
			return ids
		}
	}
	return nil
}

// EvictionFor returns the running models to stop so target fits.
//
//  1. If target fits next to everything running, nothing is evicted.
//  2. Otherwise candidates (running, not persistent) are ranked: idle and
//     unprotected first, then protected (used within protectSeconds), busy
//     last. Within a tier the model that is cheapest to bring back and idle
//     longest goes first.
//  3. Candidates are taken in that order until target fits, then any taken
//     model that is no longer needed to make room is put back.
//
// "Fits" means the pool budget has room and, when per-card sizes are known,
// some card (or set of cards) has room too. A model of unknown size is treated
// as needing the whole pool, which keeps the old "one model at a time"
// behaviour for models the pool cannot measure.
func (p *poolSwapper) EvictionFor(target string, running []string) []string {
	p.mu.RLock()
	defer p.mu.RUnlock()
	return p.evictionForLocked(target, running)
}

func (p *poolSwapper) evictionForLocked(target string, running []string) []string {
	var others []string
	for _, id := range running {
		if id != target {
			others = append(others, id)
		}
	}
	if len(others) == 0 || p.cpuOnly[target] {
		return nil // a CPU-only model takes no room on the GPUs
	}
	if ok, _ := p.fits(target, others); ok {
		return nil
	}
	if p.cpuFallback[target] {
		return nil // it runs on the CPU rather than push anything off the GPUs
	}

	var candidates []string
	for _, id := range others {
		// a model on the CPU holds no VRAM: unloading it frees nothing on the GPUs
		if !p.persistent[id] && !p.onCPU[id] && !p.cpuOnly[id] {
			candidates = append(candidates, id)
		}
	}
	p.rank(candidates)

	var evict []string
	remaining := slices.Clone(others)
	for _, id := range candidates {
		if ok, _ := p.fits(target, remaining); ok {
			break
		}
		evict = append(evict, id)
		remaining = slices.DeleteFunc(remaining, func(r string) bool { return r == id })
	}
	if ok, _ := p.fits(target, remaining); !ok {
		// Even evicting every candidate is not enough. Free all we can and
		// let the load try.
		return candidates
	}

	// Put back anything taken early that the later, larger picks made
	// unnecessary. Walk from the most valuable pick down.
	var kept []string
	for i := len(evict) - 1; i >= 0; i-- {
		trial := append(slices.Clone(remaining), evict[i])
		if ok, _ := p.fits(target, trial); ok {
			remaining = trial
			continue
		}
		kept = append(kept, evict[i])
	}
	sort.Strings(kept)
	return kept
}

// fits reports whether target can run next to the others, and on which cards.
// The card list is nil when the pool has no per-card sizes.
func (p *poolSwapper) fits(target string, others []string) (bool, []string) {
	if len(p.cards) == 0 {
		used := 0.0
		for _, id := range others {
			used += p.size(id)
		}
		return used+p.size(target) <= p.budget, nil
	}

	allowed := p.allowedCards(target)
	if p.sizes[target] == 0 {
		// Unknown size: it takes every card, so it needs the pool to itself.
		return len(others) == 0, cardIDs(allowed)
	}

	used := make(map[string]float64, len(p.cards))
	models := make(map[string]int, len(p.cards))
	for _, id := range others {
		for _, c := range p.cards {
			if u := p.usage(id, c); u > 0 {
				used[c.id] += u
				models[c.id]++
			}
		}
	}
	free := func(c poolCard) float64 { return c.cap - used[c.id] - p.outsideOn(c) }
	need := p.sizes[target]

	// Meant for several cards: all the cards it may use.
	if p.spread[target] && len(allowed) > 1 {
		if p.even[target] {
			// --split-mode tensor divides the model evenly, so every card has
			// to hold its exact share. Nothing to weigh: it is a hard number.
			share := need / float64(len(allowed))
			for _, c := range allowed {
				if free(c) < share {
					return false, nil
				}
			}
		} else if totalFree(allowed, free) < need {
			// layer and row split in proportion to each card's free memory, so
			// what has to cover the model is the cards together - not an equal
			// share each, which refuses a model the cards have room for as soon
			// as one of them is a little tighter than the other.
			return false, nil
		}
		ids := cardIDs(allowed)
		sortIDs(ids)
		return true, ids
	}

	// One card: the one with the fewest models already on it, so the cards
	// share the load, then the most free room.
	var best *poolCard
	for i := range allowed {
		c := allowed[i]
		if free(c) < need {
			continue
		}
		if best == nil || models[c.id] < models[best.id] ||
			(models[c.id] == models[best.id] && free(c) > free(*best)) {
			best = &allowed[i]
		}
	}
	if best != nil {
		return true, []string{best.id}
	}

	// Too big for one card: fill the cards with the most room until their
	// combined free space covers the model.
	//
	// This asked for an equal share on each card instead, and that refused
	// models the cards had room for whenever one of them held less than half.
	// A 44.61 GiB model over cards with 21.94 and 31 GiB free needs 22.31 on
	// the first by that rule - a third of a GiB short - and was turned away
	// with 52.94 GiB free between them. Worse, the pool then evicted a running
	// model to try to make room it did not need.
	//
	// Across cards the total is what matters: llama.cpp decides the layer split
	// when it starts, and it weighs the free memory on each card when it does.
	byFree := slices.Clone(allowed)
	sort.SliceStable(byFree, func(i, j int) bool { return free(byFree[i]) > free(byFree[j]) })
	if len(byFree) > 1 {
		total := free(byFree[0])
		for k := 2; k <= len(byFree); k++ {
			total += free(byFree[k-1])
			if total >= need {
				ids := make([]string, k)
				for i := range ids {
					ids[i] = byFree[i].id
				}
				sortIDs(ids)
				return true, ids
			}
		}
	}
	return false, nil
}

// allowedCards is the cards target may use: the ones its HIP_VISIBLE_DEVICES
// pin names, or every card.
func (p *poolSwapper) allowedCards(target string) []poolCard {
	pin := p.pins[target]
	if len(pin) == 0 {
		return p.cards
	}
	var out []poolCard
	for _, c := range p.cards {
		if slices.Contains(pin, c.id) {
			out = append(out, c)
		}
	}
	if len(out) == 0 {
		return p.cards
	}
	return out
}

// usage is the GiB model id holds on card c. A model of unknown size is
// assumed to fill every card; a model with no recorded placement (started
// before the pool knew about it) is assumed spread over all cards.
func (p *poolSwapper) usage(id string, c poolCard) float64 {
	if p.onCPU[id] || p.cpuOnly[id] {
		return 0
	}
	if p.sizes[id] == 0 {
		return c.cap
	}
	cards := p.placed[id]
	if len(cards) == 0 {
		return p.sizes[id] / float64(len(p.cards))
	}
	if !slices.Contains(cards, c.id) {
		return 0
	}
	return p.sizes[id] / float64(len(cards))
}

// runsOnCPU reports whether id was started on the CPU (cpu fallback).
func (p *poolSwapper) runsOnCPU(id string) bool {
	p.mu.RLock()
	defer p.mu.RUnlock()
	return p.onCPU[id] || p.cpuOnly[id]
}

// placement returns the cards chosen for id at its last swap, or nil.
func (p *poolSwapper) placement(id string) []string {
	p.mu.RLock()
	defer p.mu.RUnlock()
	return slices.Clone(p.placed[id])
}

func cardIDs(cards []poolCard) []string {
	ids := make([]string, len(cards))
	for i, c := range cards {
		ids[i] = c.id
	}
	return ids
}

func sortIDs(ids []string) {
	sort.Slice(ids, func(i, j int) bool {
		a, _ := strconv.Atoi(ids[i])
		b, _ := strconv.Atoi(ids[j])
		return a < b
	})
}

// size returns a model's VRAM in GiB. Unknown sizes count as the whole pool.
func (p *poolSwapper) size(id string) float64 {
	if p.onCPU[id] || p.cpuOnly[id] {
		return 0
	}
	if s := p.sizes[id]; s > 0 {
		return s
	}
	return max(p.budget, 0) + 1
}

// rank sorts candidates so the first is the best one to evict.
func (p *poolSwapper) rank(ids []string) {
	now := p.now()
	tier := func(id string) int {
		switch {
		case p.busy[id] > 0:
			return 2
		case now.Sub(p.lastUsed[id]) < p.protect:
			return 1
		default:
			return 0
		}
	}
	// keep is what we lose by evicting: reload time, discounted by how long
	// the model has sat idle. Lower is cheaper to evict.
	keep := func(id string) float64 {
		idleMin := now.Sub(p.lastUsed[id]).Minutes()
		return p.reloadCost(id).Seconds() / (1 + idleMin)
	}
	sort.SliceStable(ids, func(i, j int) bool {
		ti, tj := tier(ids[i]), tier(ids[j])
		if ti != tj {
			return ti < tj
		}
		ki, kj := keep(ids[i]), keep(ids[j])
		if ki != kj {
			return ki < kj
		}
		return ids[i] < ids[j]
	})
}

// reloadCost is the measured load time, or an estimate of one second per GiB
// until the model has been loaded once.
func (p *poolSwapper) reloadCost(id string) time.Duration {
	if d, ok := p.loadTime[id]; ok {
		return d
	}
	return time.Duration(p.size(id) * float64(time.Second))
}

// OnSwapStart commits the decision: it records which cards target will run on,
// so process launch can read it, and logs the plan.
func (p *poolSwapper) OnSwapStart(target string, running []string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.cpuOnly[target] {
		delete(p.placed, target)
		p.logger.Infof("pool: loading %s on the CPU (its command keeps it off the GPUs) next to %v", target, running)
		return
	}
	delete(p.onCPU, target) // a new run: GPU if it fits
	evict := p.evictionForLocked(target, running)
	var staying []string
	for _, id := range running {
		if id != target && !slices.Contains(evict, id) {
			staying = append(staying, id)
		}
	}
	for id := range p.onCPU {
		if !slices.Contains(running, id) {
			delete(p.onCPU, id) // stopped since
		}
	}
	if p.cpuFallback[target] {
		if ok, _ := p.fits(target, staying); !ok {
			p.onCPU[target] = true
			delete(p.placed, target)
			p.logger.Infof("pool: loading %s on the CPU: its %.1f GiB do not fit beside %v, and it is set to fall back to the CPU rather than unload them",
				target, p.sizes[target], staying)
			return
		}
	}

	used := 0.0
	for _, id := range staying {
		used += p.size(id)
	}

	var cardNote string
	if len(p.cards) > 0 {
		ok, cards := p.fits(target, staying)
		if !ok {
			cards = cardIDs(p.allowedCards(target))
		}
		p.placed[target] = cards
		// Forget models that are gone or about to be stopped for this swap.
		for id := range p.placed {
			if id != target && (!slices.Contains(running, id) || slices.Contains(evict, id)) {
				delete(p.placed, id)
			}
		}
		cardNote = " on GPU " + strings.Join(cards, ",")
	}

	if len(evict) == 0 {
		p.logger.Infof("pool: loading %s (%.1f GiB)%s next to %v, %.1f/%.1f GiB in use",
			target, p.size(target), cardNote, running, used, p.budget)
		return
	}
	p.logger.Infof("pool: loading %s (%.1f GiB)%s needs room, evicting %v, %.1f/%.1f GiB in use",
		target, p.size(target), cardNote, evict, used, p.budget)
}

// OnServeStart implements scheduler.UsageObserver.
func (p *poolSwapper) OnServeStart(id string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.busy[id]++
	p.lastUsed[id] = p.now()
}

// StaysLoaded implements scheduler.StaysLoaded: a model used within the
// protect window stays for its next prompt instead of being unloaded for
// another model, when protectWaits is on. Persistent models never get here
// (they are not evicted at all).
func (p *poolSwapper) StaysLoaded(id string) (bool, float64) {
	p.mu.RLock()
	defer p.mu.RUnlock()
	if !p.protectWaits || p.protect <= 0 {
		return false, 0
	}
	t, ok := p.lastUsed[id]
	if !ok {
		return false, 0
	}
	left := p.protect - p.now().Sub(t)
	if left <= 0 {
		return false, 0
	}
	return true, left.Seconds()
}

// OnServeDone implements scheduler.UsageObserver.
func (p *poolSwapper) OnServeDone(id string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.busy[id]--; p.busy[id] <= 0 {
		delete(p.busy, id)
	}
	p.lastUsed[id] = p.now()
}

// OnSwapDone implements scheduler.UsageObserver.
func (p *poolSwapper) OnSwapDone(id string, took time.Duration, err error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if err != nil {
		return
	}
	p.loadTime[id] = took
	p.lastUsed[id] = p.now()
}

// modelVRAMGiB is the configured vram, or an estimate from the model file:
// weights plus 20% and 1 GiB for KV cache and buffers. 0 means unknown.
func modelVRAMGiB(mc config.ModelConfig) float64 {
	if mc.VRAM > 0 {
		return mc.VRAM
	}
	argv, err := mc.SanitizedCommand()
	if err != nil {
		return 0
	}
	bytes := modelFileBytes(argv)
	if bytes == 0 {
		return 0
	}
	return float64(bytes)/(1<<30)*1.2 + 1
}

// shardPattern matches the first-shard naming of a split GGUF,
// e.g. model-00001-of-00003.gguf.
var shardPattern = regexp.MustCompile(`^(.*)-(\d{5})-of-(\d{5})\.gguf$`)

// modelFileBytes returns the size of the -m / --model file in argv, summing
// every shard of a split GGUF. 0 means no model file was found.
func modelFileBytes(argv []string) int64 {
	path := ""
	for i, t := range argv {
		if (t == "-m" || t == "--model") && i+1 < len(argv) {
			path = argv[i+1]
			break
		}
		if v, ok := strings.CutPrefix(t, "--model="); ok {
			path = v
			break
		}
	}
	if path == "" {
		return 0
	}

	m := shardPattern.FindStringSubmatch(filepath.Base(path))
	if m == nil {
		fi, err := os.Stat(path)
		if err != nil {
			return 0
		}
		return fi.Size()
	}
	count, _ := strconv.Atoi(m[3])
	var total int64
	for i := 1; i <= count; i++ {
		shard := filepath.Join(filepath.Dir(path), fmt.Sprintf("%s-%05d-of-%s.gguf", m[1], i, m[3]))
		fi, err := os.Stat(shard)
		if err != nil {
			return 0
		}
		total += fi.Size()
	}
	return total
}

// drmSysfsRoot is where detectVRAMGiB looks for GPUs.
const drmSysfsRoot = "/sys/class/drm"

var drmCardPattern = regexp.MustCompile(`^card\d+$`)

// detectVRAMGiB sums mem_info_vram_total over every DRM card that reports it
// (AMD cards do, including MI50 / gfx906). 0 means nothing was found.
func detectVRAMGiB(root string) float64 {
	entries, err := os.ReadDir(root)
	if err != nil {
		return 0
	}
	var total int64
	for _, e := range entries {
		if !drmCardPattern.MatchString(e.Name()) {
			continue
		}
		data, err := os.ReadFile(filepath.Join(root, e.Name(), "device", "mem_info_vram_total"))
		if err != nil {
			continue
		}
		v, err := strconv.ParseInt(strings.TrimSpace(string(data)), 10, 64)
		if err != nil || v <= 0 {
			continue
		}
		total += v
	}
	return float64(total) / (1 << 30)
}
