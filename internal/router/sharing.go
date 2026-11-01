package router

import (
	"sort"
	"sync"
	"time"
)

// Two models on one GPU take turns on it: while one reads a prompt or writes
// an answer, the other's answer slows down, sometimes to a fraction of its
// speed. How much depends on the pair, so it is learned from the logs: each
// model's answer speed (its slots' tg_3s) when nothing else on its cards is
// busy, and its speed while a given other model is busy. The pool's share
// guard holds one model's new request while a model it slows a lot is
// answering. It is shared by every router, like the slot watch, so a reload
// does not forget what was learned.

const (
	shareAlpha       = 0.3 // weight of a new sample in the running averages
	shareMinSamples  = 3   // samples beside a model before its slowdown is trusted
	shareFreshWithin = 6 * time.Second
)

// Slowdown is how fast a model answers beside another, as a share of its
// speed alone.
type Slowdown struct {
	Beside  string    `json:"beside"`
	Ratio   float64   `json:"ratio"` // 0.2: it answers at a fifth of its speed alone
	Samples int       `json:"samples"`
	Updated time.Time `json:"updated"`
}

// ShareView is one running model as the watch needs it: its cards (none known
// means it is taken to share every card) and its busy slots.
type ShareView struct {
	ID    string
	Cards []string
	Slots []Slot
	// CPU: the model runs on the CPU, so it shares no GPU with anyone
	CPU bool
}

type ShareWatch struct {
	mu       sync.Mutex
	solo     map[string]float64              // model -> answer speed alone (t/s)
	pairs    map[string]map[string]*Slowdown // model -> other model -> slowdown beside it
	lastSeen map[string]time.Time            // model -> the slot update last sampled
}

// Sharing is the engine's watch.
var Sharing = NewShareWatch()

func NewShareWatch() *ShareWatch {
	return &ShareWatch{solo: map[string]float64{}, pairs: map[string]map[string]*Slowdown{}, lastSeen: map[string]time.Time{}}
}

// SharesCards reports whether two models can be on the same GPU: no cards
// known for one of them means it may be on any.
func SharesCards(a, b []string) bool {
	if len(a) == 0 || len(b) == 0 {
		return true
	}
	for _, x := range a {
		for _, y := range b {
			if x == y {
				return true
			}
		}
	}
	return false
}

// Sample reads one moment of the running models.
func (w *ShareWatch) Sample(models []ShareView, now time.Time) {
	w.mu.Lock()
	defer w.mu.Unlock()
	for _, a := range models {
		if a.CPU {
			continue // not on a GPU: nothing to learn about sharing one
		}
		// one chat at a time: two of its own slots slow it down by themselves
		if len(a.Slots) != 1 || a.Slots[0].Phase != PhaseGenerating || a.Slots[0].GenTokensPerSecond <= 0 {
			continue
		}
		sl := a.Slots[0]
		if now.Sub(sl.Updated) > shareFreshWithin || !sl.Updated.After(w.lastSeen[a.ID]) {
			continue // no new speed reading since the last sample
		}
		w.lastSeen[a.ID] = sl.Updated
		var beside []string
		for _, b := range models {
			if b.ID != a.ID && !b.CPU && len(b.Slots) > 0 && SharesCards(a.Cards, b.Cards) {
				beside = append(beside, b.ID)
			}
		}
		tps := sl.GenTokensPerSecond
		if len(beside) == 0 {
			if old, ok := w.solo[a.ID]; ok {
				w.solo[a.ID] = old + shareAlpha*(tps-old)
			} else {
				w.solo[a.ID] = tps
			}
			continue
		}
		solo, ok := w.solo[a.ID]
		if !ok || solo <= 0 {
			continue // its speed alone is not known yet
		}
		ratio := min(tps/solo, 1.5)
		if w.pairs[a.ID] == nil {
			w.pairs[a.ID] = map[string]*Slowdown{}
		}
		for _, b := range beside {
			p := w.pairs[a.ID][b]
			if p == nil {
				p = &Slowdown{Beside: b, Ratio: ratio}
				w.pairs[a.ID][b] = p
			} else {
				p.Ratio += shareAlpha * (ratio - p.Ratio)
			}
			p.Samples++
			p.Updated = now
		}
	}
}

// Solo is the model's answer speed alone, when known.
func (w *ShareWatch) Solo(model string) (float64, bool) {
	w.mu.Lock()
	defer w.mu.Unlock()
	v, ok := w.solo[model]
	return v, ok
}

// Slowdowns lists how much the model slows beside each other model, worst
// first; only pairs seen often enough to trust.
func (w *ShareWatch) Slowdowns(model string) []Slowdown {
	w.mu.Lock()
	defer w.mu.Unlock()
	var out []Slowdown
	for _, p := range w.pairs[model] {
		if p.Samples >= shareMinSamples {
			out = append(out, *p)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Ratio < out[j].Ratio })
	return out
}

// Hurts reports whether model a answers at under keep of its speed alone
// while b is busy.
func (w *ShareWatch) Hurts(a, b string, keep float64) (Slowdown, bool) {
	w.mu.Lock()
	defer w.mu.Unlock()
	p := w.pairs[a][b]
	if p == nil || p.Samples < shareMinSamples || p.Ratio >= keep {
		return Slowdown{}, false
	}
	return *p, true
}
