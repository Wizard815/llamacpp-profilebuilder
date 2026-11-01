package router

import (
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/Wizard815/Argos/internal/event"
	"github.com/Wizard815/Argos/internal/swaputil"
)

// A model that crashes on every request used to reload on every request, with
// nothing saying why: the engine starts it, the request crashes it, the next
// request starts it again. This remembers the line it died on and, after a few
// crashes close together, stops starting it until someone loads it by hand.
var reFatal = regexp.MustCompile(`(?i)GGML_ASSERT|ggml_abort|segmentation fault|core dumped|terminate called|` +
	`\b(cuda|hip|rocm|hsa|vulkan) error|out of memory|failed to allocate|illegal memory access|SIGSEGV|SIGABRT|` +
	`error while loading shared libraries|failed to load model|invalid variable name|command not found|` +
	`No such file or directory|Permission denied|is not set|cannot (open|find|connect)|Connection refused|` +
	`^usage:|\bfatal\b|panic:|Traceback`)

// crashTailLines is how many of a model's last log lines a crash keeps, so a
// failure no pattern above knows still says what the process last printed.
const crashTailLines = 6

const (
	// crashLimit crashes within crashWindow stop the engine from starting the
	// model on a request.
	crashLimit  = 3
	crashWindow = 10 * time.Minute
)

// Crash is what the engine knows about a model's last crash.
type Crash struct {
	At time.Time `json:"at"`
	// Line is the first fatal line the model printed before it exited, or a
	// note that it printed none.
	Line string `json:"line"`
	// Recent is how many times it crashed within the last crashWindow.
	Recent int `json:"recent"`
	// Blocked is true once Recent reaches crashLimit: requests for the model
	// are refused instead of starting it again.
	Blocked bool `json:"blocked"`
	// Tail is the last lines the process printed before it exited.
	Tail []string `json:"tail,omitempty"`
}

// CrashWatch follows each model's process: its fatal lines (fed from the same
// log tap as the prefill watch) and its exits the engine did not ask for.
type CrashWatch struct {
	mu     sync.Mutex
	now    func() time.Time
	fatal  map[string]string   // first fatal line since the process started
	tail   map[string][]string // its last lines since it started
	times  map[string][]time.Time
	last   map[string]Crash
	forget func(model string)
}

// Crashes is the engine's watch.
var Crashes = newCrashWatch(time.Now, Prefills.Forget)

var watchExits sync.Once

func init() { Prefills.onLine = Crashes.line }

func newCrashWatch(now func() time.Time, forget func(string)) *CrashWatch {
	return &CrashWatch{now: now, fatal: map[string]string{}, tail: map[string][]string{}, times: map[string][]time.Time{}, last: map[string]Crash{}, forget: forget}
}

// WatchExits subscribes the engine's watch to process state changes, once.
func WatchExits() {
	watchExits.Do(func() {
		event.On(func(e swaputil.ProcessStateChangeEvent) { Crashes.State(e.ProcessName, e.OldState, e.NewState) })
	})
}

// line reads one log line of a model.
func (c *CrashWatch) line(model string, line []byte) {
	s := strings.TrimSpace(string(line))
	if s == "" {
		return
	}
	if len(s) > 300 {
		s = s[:300] + "…"
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	t := append(c.tail[model], s)
	if len(t) > crashTailLines {
		t = t[len(t)-crashTailLines:]
	}
	c.tail[model] = t
	if c.fatal[model] == "" && reFatal.MatchString(s) {
		c.fatal[model] = s
	}
}

// State records a process state change. A process that goes from starting or
// ready straight to stopped was not stopped by the engine (a stop passes
// through stopping): it crashed, or failed to start.
func (c *CrashWatch) State(model, from, to string) {
	if to == "starting" {
		c.mu.Lock()
		delete(c.fatal, model) // a new run: its own lines from here
		delete(c.tail, model)
		c.mu.Unlock()
		return
	}
	if to == "ready" {
		c.mu.Lock()
		delete(c.last, model) // it started: the last failure is no longer the news
		c.mu.Unlock()
		return
	}
	if to != "stopped" {
		return
	}
	if c.forget != nil {
		c.forget(model) // no slot of a stopped process is busy
	}
	if from != "ready" && from != "starting" {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	now := c.now()
	recent := []time.Time{now}
	for _, t := range c.times[model] {
		if now.Sub(t) < crashWindow {
			recent = append(recent, t)
		}
	}
	c.times[model] = recent
	tail := append([]string(nil), c.tail[model]...)
	line := c.fatal[model]
	if line == "" && len(tail) > 0 {
		line = tail[len(tail)-1] // no known error line: the last thing it said
	}
	if line == "" {
		if from == "starting" {
			line = "exited while starting without printing anything (check its log)"
		} else {
			line = "exited without printing anything (check its log)"
		}
	}
	c.last[model] = Crash{At: now, Line: line, Recent: len(recent), Blocked: len(recent) >= crashLimit, Tail: tail}
}

// Get reports a model's last crash, until it starts again or is cleared: a
// failure stays on show however long ago it was. Blocked only holds within
// crashWindow.
func (c *CrashWatch) Get(model string) (Crash, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	cr, ok := c.last[model]
	if !ok {
		return Crash{}, false
	}
	if c.now().Sub(cr.At) >= crashWindow {
		cr.Blocked = false
	}
	return cr, true
}

// Blocked reports whether requests for the model should not start it again.
func (c *CrashWatch) Blocked(model string) (Crash, bool) {
	cr, ok := c.Get(model)
	return cr, ok && cr.Blocked
}

// Clear forgets a model's crashes: it is being loaded by hand, or its command
// was fixed.
func (c *CrashWatch) Clear(model string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	delete(c.times, model)
	delete(c.last, model)
	delete(c.fatal, model)
	delete(c.tail, model)
}
