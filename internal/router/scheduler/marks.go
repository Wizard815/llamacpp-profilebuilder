package scheduler

import (
	"context"
	"sync"
)

// What a person did to a request from Schedule -> Live, and what the queue
// tells them about one. A request is known here by the id the engine's
// in-flight list gives it, carried on its context.
//
// Held: it waits in the queue and never makes a running model unload; it is
// still served at once by a model that is loaded and has room. Sent on ("go
// next"): it may unload a model that is only staying loaded because it was
// used a moment ago.

type requestIDKey struct{}

// WithRequestID tags a request's context with its in-flight id.
func WithRequestID(ctx context.Context, id string) context.Context {
	return context.WithValue(ctx, requestIDKey{}, id)
}

// RequestID is the in-flight id a request's context carries, or "".
func RequestID(ctx context.Context) string {
	if ctx == nil {
		return ""
	}
	id, _ := ctx.Value(requestIDKey{}).(string)
	return id
}

type marks struct {
	mu     sync.Mutex
	held   map[string]bool
	forced map[string]bool
	note   func(id, why string)
}

// Marks is the engine's: one per process, shared by every router so a hold
// survives a config reload.
var Marks = &marks{held: map[string]bool{}, forced: map[string]bool{}}

// SetNote gives the queue a way to say why a request waits (the in-flight
// list's "held" text); "" clears it.
func (m *marks) SetNote(f func(id, why string)) {
	m.mu.Lock()
	m.note = f
	m.mu.Unlock()
}

func (m *marks) say(id, why string) {
	m.mu.Lock()
	f := m.note
	m.mu.Unlock()
	if f != nil && id != "" {
		f(id, why)
	}
}

// Hold keeps a request from making any model unload until Release.
func (m *marks) Hold(id string) {
	m.mu.Lock()
	m.held[id] = true
	delete(m.forced, id)
	m.mu.Unlock()
}

// Release lets a held request go on as it would have.
func (m *marks) Release(id string) {
	m.mu.Lock()
	delete(m.held, id)
	m.mu.Unlock()
}

// Force lets a request unload a model that is only staying loaded because it
// was used a moment ago (and releases a hold).
func (m *marks) Force(id string) {
	m.mu.Lock()
	delete(m.held, id)
	m.forced[id] = true
	m.mu.Unlock()
}

func (m *marks) Held(id string) bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.held[id]
}

func (m *marks) Forced(id string) bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.forced[id]
}

// Forget drops a finished request's marks.
func (m *marks) Forget(id string) {
	m.mu.Lock()
	delete(m.held, id)
	delete(m.forced, id)
	m.mu.Unlock()
}

// StaysLoaded is what a planner reports when a model it would unload was used
// so recently it should stay: the next prompt of the same chat or agent is
// likely seconds away, and unloading between them costs a full reload.
type StaysLoaded interface {
	// StaysLoaded reports whether model should not be unloaded for another
	// model's request yet, and for how many more seconds.
	StaysLoaded(model string) (bool, float64)
}
