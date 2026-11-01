package server

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"hash/fnv"
	"io"
	"net/http"
	"sync"
	"time"

	"github.com/Wizard815/Argos/internal/chain"
	"github.com/Wizard815/Argos/internal/router"
	"github.com/Wizard815/Argos/internal/router/scheduler"
	"github.com/Wizard815/Argos/internal/swaputil"
	"github.com/tidwall/gjson"
)

// Answer first (routing.router.settings.pool.answerFirst).
//
// llama.cpp serves a model's chats in slots and builds each GPU batch from all
// of them: one token for a chat that is answering, up to a whole n_batch of a
// chat reading its prompt. While one chat reads a long prompt the other's
// answer drops from tens of tokens a second to one or two, because every step
// now takes as long as the prompt chunk. Argos cannot share a step out
// differently or pause a slot once both are inside llama-server; what it can
// do is choose when a request goes in. A long new prompt for a model where
// another chat is still answering waits for that answer to finish (an agent's
// turns leave a gap at each tool call), then goes. Short ones go straight
// through: a few seconds of a slower answer is cheaper than the wait.

const (
	answerFirstPoll     = 250 * time.Millisecond
	answerFirstRemember = 16 // recent requests per model, to find a conversation's new part
	bytesPerToken       = 4
)

type answerFirstState struct {
	mu     sync.Mutex
	recent map[string][][]uint64 // model -> message hashes of its recent requests, newest last
	slots  func(model string) []router.Slot
	poll   time.Duration
}

func newAnswerFirst() *answerFirstState {
	return &answerFirstState{recent: map[string][][]uint64{}, slots: router.Prefills.Slots, poll: answerFirstPoll}
}

// newTokens estimates how much of a request's prompt the model has not read
// before: the messages after the longest run it shares with a recent request
// to the same model (a conversation's earlier turns sit in the slot's cache),
// or all of it, tool definitions included, for a conversation it has not
// seen. It remembers the request for the next one.
func (a *answerFirstState) newTokens(model string, body []byte) int {
	msgs := gjson.GetBytes(body, "messages")
	if !msgs.IsArray() {
		return len(gjson.GetBytes(body, "prompt").Raw) / bytesPerToken
	}
	var hashes []uint64
	var sizes []int
	msgs.ForEach(func(_, m gjson.Result) bool {
		h := fnv.New64a()
		h.Write([]byte(m.Raw))
		hashes = append(hashes, h.Sum64())
		sizes = append(sizes, len(m.Raw))
		return true
	})

	a.mu.Lock()
	shared := 0
	for _, prev := range a.recent[model] {
		n := 0
		for n < len(prev) && n < len(hashes) && prev[n] == hashes[n] {
			n++
		}
		shared = max(shared, n)
	}
	list := append(a.recent[model], hashes)
	if len(list) > answerFirstRemember {
		list = list[len(list)-answerFirstRemember:]
	}
	a.recent[model] = list
	a.mu.Unlock()

	newBytes := 0
	for _, s := range sizes[shared:] {
		newBytes += s
	}
	if shared == 0 {
		newBytes += len(gjson.GetBytes(body, "tools").Raw)
	}
	return newBytes / bytesPerToken
}

// answering reports whether a chat on the model is generating right now.
func (a *answerFirstState) answering(model string) bool {
	for _, s := range a.slots(model) {
		if s.Phase == router.PhaseGenerating {
			return true
		}
	}
	return false
}

// slotGate is the waiting room in front of each model: one at a time lets a
// single request into the model and queues the rest in order; answer first
// holds a long prompt while another chat answers. "Go next" (Schedule -> Live)
// moves a queued request to the front, or ends an answer-first hold.
type slotGate struct {
	mu      sync.Mutex
	busy    map[string]bool
	queue   map[string][]*gateTicket
	skip    map[string]chan struct{} // in-flight id -> ends its answer-first hold
	setHeld func(id, why string)
}

type gateTicket struct {
	id    string
	ready chan struct{}
}

func newSlotGate(setHeld func(id, why string)) *slotGate {
	return &slotGate{busy: map[string]bool{}, queue: map[string][]*gateTicket{}, skip: map[string]chan struct{}{}, setHeld: setHeld}
}

// acquire waits for the model to be free, in order. False when the client gave
// up first.
func (g *slotGate) acquire(ctx context.Context, model, id string) bool {
	g.mu.Lock()
	if !g.busy[model] && len(g.queue[model]) == 0 {
		g.busy[model] = true
		g.mu.Unlock()
		return true
	}
	t := &gateTicket{id: id, ready: make(chan struct{})}
	g.queue[model] = append(g.queue[model], t)
	g.noteLocked(model)
	g.mu.Unlock()

	select {
	case <-t.ready:
		g.setHeld(id, "")
		return true
	case <-ctx.Done():
		g.mu.Lock()
		q, queued := g.queue[model], false
		for i, x := range q {
			if x == t {
				g.queue[model] = append(q[:i:i], q[i+1:]...)
				queued = true
				break
			}
		}
		g.noteLocked(model)
		g.mu.Unlock()
		if !queued { // it was let in as the client left: pass the turn on
			g.release(model)
		}
		return false
	}
}

// release lets the next request in, or frees the model.
func (g *slotGate) release(model string) {
	g.mu.Lock()
	defer g.mu.Unlock()
	if q := g.queue[model]; len(q) > 0 {
		g.queue[model] = q[1:]
		close(q[0].ready) // the model stays busy: it is that request's turn
		g.noteLocked(model)
		return
	}
	g.busy[model] = false
}

// noteLocked tells each queued request where it stands.
func (g *slotGate) noteLocked(model string) {
	for i, t := range g.queue[model] {
		g.setHeld(t.id, fmt.Sprintf("one at a time: waits for %s to finish the request before it (%d ahead)", model, i+1))
	}
}

// next moves a waiting request to the front of its queue, or ends its
// answer-first hold. False when that request is not waiting.
func (g *slotGate) next(id string) bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	if ch, ok := g.skip[id]; ok {
		close(ch)
		delete(g.skip, id)
		return true
	}
	for model, q := range g.queue {
		for i, t := range q {
			if t.id == id {
				g.queue[model] = append([]*gateTicket{t}, append(q[:i:i], q[i+1:]...)...)
				g.noteLocked(model)
				return true
			}
		}
	}
	return false
}

// holdUntil registers an answer-first hold that "go next" can end.
func (g *slotGate) holdUntil(id string) (<-chan struct{}, func()) {
	ch := make(chan struct{})
	g.mu.Lock()
	g.skip[id] = ch
	g.mu.Unlock()
	return ch, func() {
		g.mu.Lock()
		if g.skip[id] == ch {
			delete(g.skip, id)
		}
		g.mu.Unlock()
	}
}

// shareViews is what the share watch needs of the running models: their
// cards (from the pool's placement) and their busy slots.
func (s *Server) shareViews() []router.ShareView {
	placer, _ := s.local.(router.Placer)
	var out []router.ShareView
	for id := range s.local.RunningModels() {
		v := router.ShareView{ID: id, Slots: router.Prefills.Slots(id), CPU: s.onCPU(id)}
		if placer != nil {
			v.Cards = placer.Placement(id)
		}
		out = append(out, v)
	}
	return out
}

// onCPU reports whether model runs on the CPU (CPU only, or fallen back): it
// takes no GPU time, so no share guard applies to it either way.
func (s *Server) onCPU(model string) bool {
	cpu, ok := s.local.(interface{ OnCPU(string) bool })
	return ok && cpu.OnCPU(model)
}

// watchSharing samples the running models once a second, so the share watch
// learns how much each slows beside the others.
func (s *Server) watchSharing() {
	tick := time.NewTicker(time.Second)
	defer tick.Stop()
	for {
		select {
		case <-s.shutdownCtx.Done():
			return
		case now := <-tick.C:
			router.Sharing.Sample(s.shareViews(), now)
		}
	}
}

// shareHolder names a model on the same GPU as model that is answering now
// and was seen to drop under keep of its speed alone while model is busy.
func (s *Server) shareHolder(model string, keep float64) (string, router.Slowdown, bool) {
	views := s.shareViews()
	var mine router.ShareView
	for _, v := range views {
		if v.ID == model {
			mine = v
		}
	}
	if mine.ID == "" {
		mine.ID = model // not loaded: it may be placed anywhere, so it shares every card
		mine.CPU = s.onCPU(model)
	}
	if mine.CPU {
		return "", router.Slowdown{}, false // on the CPU: it slows no GPU model and waits for none
	}
	for _, a := range views {
		if a.ID == model || a.CPU {
			continue
		}
		answering := false
		for _, sl := range a.Slots {
			answering = answering || sl.Phase == router.PhaseGenerating
		}
		if !answering || !router.SharesCards(a.Cards, mine.Cards) {
			continue
		}
		if sd, hurts := router.Sharing.Hurts(a.ID, model, keep); hurts {
			return a.ID, sd, true
		}
	}
	return "", router.Slowdown{}, false
}

// CreateAnswerFirstMiddleware is the waiting room in front of each model: one
// at a time for the models it is on, else answer first for those. It runs
// after the in-flight tracker, so a waiting request is listed as running and
// marked held.
func CreateAnswerFirstMiddleware(s *Server) chain.Middleware {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			pool := s.cfg.Routing.Router.Settings.Pool
			af := pool.AnswerFirst
			data, ok := swaputil.ReadContext(r.Context())
			if !ok || s.answerFirst == nil || s.gate == nil || r.Method != http.MethodPost {
				next.ServeHTTP(w, r)
				return
			}
			iw, _ := w.(*inflightResponseWriter)
			id := ""
			if iw != nil {
				id = iw.id
			}

			// Share guard: another model on the same GPU is answering and was
			// seen to slow down a lot beside this one; wait for its answer.
			if sg := pool.ShareGuard; sg.Enabled {
				keep := 1 - float64(sg.MaxSlowdownPercent)/100
				if other, sd, hold := s.shareHolder(data.ModelID, keep); hold {
					if iw != nil {
						iw.tracker.SetHeld(id, fmt.Sprintf("share guard: waits while %s answers (it drops to %.0f%% of its speed beside %s)",
							other, sd.Ratio*100, data.ModelID))
					}
					skip, done := s.gate.holdUntil(id)
					start := time.Now()
					deadline := start.Add(time.Duration(sg.MaxWaitSeconds) * time.Second)
					tick := time.NewTicker(s.answerFirst.poll)
				guard:
					for {
						if _, _, still := s.shareHolder(data.ModelID, keep); !still {
							break
						}
						select {
						case <-r.Context().Done():
							tick.Stop()
							done()
							return
						case <-skip:
							break guard
						case <-tick.C:
							if time.Now().After(deadline) {
								break guard
							}
						}
					}
					tick.Stop()
					done()
					if iw != nil {
						iw.tracker.SetHeld(id, "")
					}
					s.proxylog.Infof("<%s> share guard: held a request for %s while %s answered",
						data.ModelID, time.Since(start).Round(100*time.Millisecond), other)
				}
			}

			// One at a time: the model gets this request only once the one
			// before it is done, so each runs at the speed it has alone.
			if pool.OneAtATimeFor(data.ModelID) {
				start := time.Now()
				if !s.gate.acquire(r.Context(), data.ModelID, id) {
					return // the client gave up while it waited
				}
				defer s.gate.release(data.ModelID)
				if waited := time.Since(start); waited > time.Second {
					s.proxylog.Infof("<%s> one at a time: a request waited %s for the one before it", data.ModelID, waited.Round(100*time.Millisecond))
				}
				next.ServeHTTP(w, r)
				return
			}

			if len(af.Models) == 0 || !af.Applies(data.ModelID) {
				next.ServeHTTP(w, r)
				return
			}
			body, err := io.ReadAll(r.Body)
			r.Body = io.NopCloser(bytes.NewReader(body))
			if err != nil {
				next.ServeHTTP(w, r)
				return
			}
			tokens := s.answerFirst.newTokens(data.ModelID, body)
			if tokens < af.MinPromptTokens || !s.answerFirst.answering(data.ModelID) {
				next.ServeHTTP(w, r)
				return
			}

			if iw != nil {
				iw.tracker.SetHeld(id, fmt.Sprintf("answer first: a ~%d-token prompt waits while another chat on this model is answering", tokens))
			}
			skip, done := s.gate.holdUntil(id)
			start := time.Now()
			deadline := start.Add(time.Duration(af.MaxWaitSeconds) * time.Second)
			tick := time.NewTicker(s.answerFirst.poll)
		wait:
			for s.answerFirst.answering(data.ModelID) {
				select {
				case <-r.Context().Done():
					tick.Stop()
					done()
					return // the client gave up while it waited
				case <-skip:
					break wait // sent on by hand (Go next)
				case <-tick.C:
					if time.Now().After(deadline) {
						break wait
					}
				}
			}
			tick.Stop()
			done()
			if iw != nil {
				iw.tracker.SetHeld(id, "")
			}
			s.proxylog.Infof("<%s> answer first: held a ~%d-token prompt for %s while another chat answered",
				data.ModelID, tokens, time.Since(start).Round(100*time.Millisecond))
			next.ServeHTTP(w, r)
		})
	}
}

// handleAPIInflightHold holds a request (POST .../hold) so it never makes a
// model unload, or lets it go on as before (DELETE .../hold).
func (s *Server) handleAPIInflightHold(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !s.inflightHas(id) {
		swaputil.SendResponse(w, r, http.StatusNotFound, "no such request running")
		return
	}
	if r.Method == http.MethodDelete {
		scheduler.Marks.Release(id)
		s.inflight.SetHeld(id, "")
	} else {
		scheduler.Marks.Hold(id)
		s.inflight.SetHeld(id, "held: it will not make any model unload; Release or Go next lets it")
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{"msg": "ok"})
}

func (s *Server) inflightHas(id string) bool {
	for _, e := range s.inflight.Current().Requests {
		if e.ID == id {
			return true
		}
	}
	return false
}

// handleAPIInflightNext sends a waiting request on first (Schedule -> Live):
// to the front of its model's one-at-a-time queue, or out of its
// answer-first hold.
func (s *Server) handleAPIInflightNext(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	gated := s.gate != nil && s.gate.next(id)
	// in the scheduler's queue: past a hold, and past a model that is only protected
	queued := s.inflightHas(id)
	if queued {
		scheduler.Marks.Force(id)
	}
	if !gated && !queued {
		swaputil.SendResponse(w, r, http.StatusNotFound, "that request is not waiting")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{"msg": "ok"})
}
