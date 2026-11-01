package router

import (
	"io"
	"testing"
	"time"

	"github.com/Wizard815/Argos/internal/config"
	"github.com/Wizard815/Argos/internal/logmon"
	"github.com/Wizard815/Argos/internal/process"
	"github.com/Wizard815/Argos/internal/router/scheduler"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func outsideSwapper(t *testing.T) *poolSwapper {
	t.Helper()
	lastOutsideMu.Lock()
	lastOutside = map[string]float64{}
	lastOutsideMu.Unlock()
	t.Cleanup(func() {
		lastOutsideMu.Lock()
		lastOutside = map[string]float64{}
		lastOutsideMu.Unlock()
	})
	conf := config.Config{Models: map[string]config.ModelConfig{"a": {VRAM: 10}, "big": {VRAM: 20}}}
	conf.Routing.Router.Settings.Pool = config.PoolConfig{Cards: []float64{32, 32}, ReserveGiB: 2, ProtectSeconds: 60, Placement: "auto"}
	return newPoolSwapper(conf, logmon.NewWriter(io.Discard), nil)
}

func TestPool_OutsideVRAMIsPlannedAround(t *testing.T) {
	p := outsideSwapper(t)
	ok, cards := p.fits("big", nil)
	require.True(t, ok)
	assert.Len(t, cards, 1, "with the cards empty, one card holds it")

	// another app holds 19 GiB on each card: 13 left on each (31 less 18, the
	// first GiB being the reserve), so the 20 GiB model needs both
	p.sampleOutside(map[string]float64{"0": 19, "1": 19}, nil)
	ok, cards = p.fits("big", nil)
	require.True(t, ok)
	assert.Equal(t, []string{"0", "1"}, cards)

	p.sampleOutside(map[string]float64{"0": 29, "1": 29}, nil)
	p.sampleOutside(map[string]float64{"0": 29, "1": 29}, nil)
	p.sampleOutside(map[string]float64{"0": 29, "1": 29}, nil)
	p.sampleOutside(map[string]float64{"0": 29, "1": 29}, nil)
	ok, _ = p.fits("big", nil)
	assert.False(t, ok, "4 GiB left across the cards")
}

func TestPool_OutsideVRAMLeavesArgosModelsOutAndShowsOnTheCards(t *testing.T) {
	p := outsideSwapper(t)
	p.placed["a"] = []string{"0"}
	running := map[string]process.ProcessState{"a": process.StateReady}
	p.sampleOutside(map[string]float64{"0": 15, "1": 0.5}, running)

	st := p.status(running, scheduler.Status{})
	require.Len(t, st.Cards, 2)
	c0 := st.Cards[0]
	require.NotNil(t, c0.OtherGiB)
	assert.Equal(t, 5.0, *c0.OtherGiB, "15 in use, 10 of it model a's")
	assert.Equal(t, 15.0, *c0.LiveUsedGiB)
	assert.Equal(t, 10.0, c0.UsedGiB)
	assert.Equal(t, 17.0, c0.FreeGiB, "31 less a's 10 and the 4 the reserve does not cover")
	assert.Equal(t, 31.0, st.Cards[1].FreeGiB, "half a GiB is within the reserve")
}

func TestPool_OutsideVRAMIgnoresAModelStillUnloading(t *testing.T) {
	p := outsideSwapper(t)
	// a model was just stopped but its memory is not freed yet: one reading
	p.sampleOutside(map[string]float64{"0": 0, "1": 0}, nil)
	p.sampleOutside(map[string]float64{"0": 10, "1": 0}, nil)
	assert.Equal(t, 0.0, p.outside["0"], "the smallest recent reading counts")
	for range outsideSamples {
		p.sampleOutside(map[string]float64{"0": 6, "1": 0}, nil)
	}
	assert.Equal(t, 6.0, p.outside["0"], "held for longer, it counts")

	// a new pool after a reload starts from the last reading
	conf := config.Config{}
	conf.Routing.Router.Settings.Pool = config.PoolConfig{Cards: []float64{32, 32}, ReserveGiB: 2, ProtectSeconds: 60, Placement: "auto"}
	q := newPoolSwapper(conf, logmon.NewWriter(io.Discard), nil)
	assert.Equal(t, 6.0, q.outside["0"])
}

func TestPool_OutsideVRAMReadsTheEnginesOwnGPUReadingFirst(t *testing.T) {
	SetCardUsedSource(func() map[string]float64 { return map[string]float64{"0": 19.8, "1": 16.6} })
	t.Cleanup(func() { SetCardUsedSource(nil) })
	assert.Equal(t, map[string]float64{"0": 19.8, "1": 16.6}, cardUsed())

	p := outsideSwapper(t)
	p.sampleOutside(cardUsed(), nil)
	st := p.status(nil, scheduler.Status{})
	require.NotNil(t, st.Cards[0].OtherGiB)
	assert.InDelta(t, 19.8, *st.Cards[0].OtherGiB, 0.001)
	assert.InDelta(t, 12.2, st.Cards[0].FreeGiB, 0.001, "31 less the 18.8 the reserve does not cover")
}

// Protect holds by default: a model used within protectSeconds stays for its
// next prompt; protectWaits: false only orders evictions, as before.
func TestPool_AProtectedModelStaysLoaded(t *testing.T) {
	conf := config.Config{Models: map[string]config.ModelConfig{"a": {VRAM: 10}}}
	conf.Routing.Router.Settings.Pool = config.PoolConfig{VRAMGiB: 64, ProtectSeconds: 600, Placement: "off"}
	p := newPoolSwapper(conf, logmon.NewWriter(io.Discard), nil)
	now := time.Unix(10000, 0)
	p.now = func() time.Time { return now }

	stay, _ := p.StaysLoaded("a")
	assert.False(t, stay, "never used")
	p.OnServeStart("a")
	now = now.Add(5 * time.Minute)
	p.OnServeDone("a")
	now = now.Add(time.Minute)
	stay, left := p.StaysLoaded("a")
	assert.True(t, stay)
	assert.InDelta(t, 540, left, 0.1, "counted from the end of its last request")
	now = now.Add(10 * time.Minute)
	stay, _ = p.StaysLoaded("a")
	assert.False(t, stay)

	off := false
	conf.Routing.Router.Settings.Pool.ProtectWaits = &off
	q := newPoolSwapper(conf, logmon.NewWriter(io.Discard), nil)
	q.OnServeDone("a")
	stay, _ = q.StaysLoaded("a")
	assert.False(t, stay)
}
