package router

import (
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func gen(id int, tps float64, at time.Time) []Slot {
	return []Slot{{ID: id, Phase: PhaseGenerating, Prefill: Prefill{Updated: at}, GenTokensPerSecond: tps}}
}

// The 20 GB model on cards 0 and 1 answers at 40 t/s alone and at 8 t/s while
// the 10 GB model on card 1 works: that is learned from the slots' speeds.
func TestSharing_LearnsHowMuchAModelSlowsBesideAnother(t *testing.T) {
	w := NewShareWatch()
	now := time.Unix(1000, 0)
	big, small := []string{"0", "1"}, []string{"1"}
	for i := 0; i < 3; i++ { // alone
		now = now.Add(3 * time.Second)
		w.Sample([]ShareView{{ID: "big", Cards: big, Slots: gen(0, 40, now)}, {ID: "small", Cards: small}}, now)
	}
	solo, ok := w.Solo("big")
	require.True(t, ok)
	assert.InDelta(t, 40, solo, 0.01)
	_, hurts := w.Hurts("big", "small", 0.5)
	assert.False(t, hurts, "nothing learned beside it yet")

	for i := 0; i < 4; i++ { // beside the small one, which is reading a prompt
		now = now.Add(3 * time.Second)
		w.Sample([]ShareView{
			{ID: "big", Cards: big, Slots: gen(0, 8, now)},
			{ID: "small", Cards: small, Slots: []Slot{{ID: 0, Phase: PhasePrefill, Prefill: Prefill{Updated: now}}}},
		}, now)
		w.Sample([]ShareView{{ID: "big", Cards: big, Slots: gen(0, 8, now)}}, now.Add(time.Second)) // the same reading again: not a new sample
	}
	sd, hurts := w.Hurts("big", "small", 0.5)
	require.True(t, hurts)
	assert.Equal(t, 4, sd.Samples, "a reading counts once, however often it is sampled")
	assert.InDelta(t, 0.2, sd.Ratio, 0.01)
	assert.Equal(t, "small", w.Slowdowns("big")[0].Beside)
	_, hurts = w.Hurts("big", "small", 0.1)
	assert.False(t, hurts, "a fifth of its speed is above a tenth")
}

func TestSharing_ModelsOnOtherCardsAndTwoOwnChatsAreNotCounted(t *testing.T) {
	w := NewShareWatch()
	now := time.Unix(1000, 0)
	for i := 0; i < 5; i++ {
		now = now.Add(3 * time.Second)
		w.Sample([]ShareView{
			{ID: "a", Cards: []string{"0"}, Slots: gen(0, 40, now)},
			{ID: "b", Cards: []string{"1"}, Slots: gen(0, 30, now)},
		}, now)
	}
	solo, _ := w.Solo("a")
	assert.InDelta(t, 40, solo, 0.01, "b is on another card: a is alone on its own")
	assert.Empty(t, w.Slowdowns("a"))

	two := append(gen(0, 10, now), gen(1, 10, now)...)
	w.Sample([]ShareView{{ID: "c", Cards: []string{"0"}, Slots: two}}, now)
	_, ok := w.Solo("c")
	assert.False(t, ok, "two chats of its own slow it by themselves: not its speed alone")
}

// A model on the CPU shares no GPU: a GPU model working beside it is still
// "alone" on its cards, and the CPU model learns no slowdown at all.
func TestSharing_ACPUModelSharesNoGPU(t *testing.T) {
	w := NewShareWatch()
	now := time.Unix(1000, 0)
	for i := 0; i < 4; i++ {
		now = now.Add(3 * time.Second)
		w.Sample([]ShareView{
			{ID: "coder", Cards: []string{"0", "1"}, Slots: gen(0, 40, now)},
			{ID: "chat-cpu", CPU: true, Slots: gen(1, 10, now)},
		}, now)
	}
	solo, ok := w.Solo("coder")
	require.True(t, ok)
	assert.InDelta(t, 40, solo, 0.01, "the CPU model working beside it does not count as sharing")
	assert.Empty(t, w.Slowdowns("coder"))
	_, ok = w.Solo("chat-cpu")
	assert.False(t, ok, "nothing learned for a model off the GPUs")
}
