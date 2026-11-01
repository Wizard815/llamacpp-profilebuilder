---
title: VRAM-aware pool router
summary: Put every model in one pool so models load side by side while VRAM fits, and only the cheapest idle model is evicted when it does not.
category: guides
tags: [routing, pool, vram, eviction, swap, gpu, memory, persistent]
config_keys: [routing, models.*.vram, routing.router.settings.pool.protectWaits, routing.router.settings.pool.answerFirst, routing.router.settings.pool.oneAtATime, routing.router.settings.pool.shareGuard, routing.router.settings.pool.cpuFallback]
updated: 2026-10-25
---

# VRAM-aware pool router

With the default `group` router every model is in one swapping group, so
loading any model unloads the others, even a 0.6B model next to a 30B one
with plenty of free VRAM. The `pool` router fixes that: models share the
GPUs while they fit.

```yaml
models:
  qwen-30b:
    cmd: llama-server --port ${PORT} -m /models/qwen3-30b-q4.gguf -c 32768
    vram: 26          # weights + KV cache for -c 32768
  qwen-0.6b:
    cmd: llama-server --port ${PORT} -m /models/qwen3-0.6b.gguf
  whisper:
    cmd: whisper-server --port ${PORT} -m /models/ggml-large-v3.bin
    vram: 4

routing:
  router:
    use: pool
    settings:
      pool:
        cards: [32, 32]   # empty = detect the cards at startup
        reserveGiB: 2
        protectSeconds: 600
        persistent: [whisper]
```

## How it decides

1. If the requested model fits next to everything running, nothing stops.
2. Otherwise running models are ranked: idle first, then models used in the
   last `protectSeconds`, busy models last. Within each, the model that is
   quickest to reload and idle longest goes first. Load times are measured.
3. Models are taken in that order until the new one fits. Any that turn out
   not to be needed are put back.
4. `persistent` models are never evicted for another model.
5. A model used in the last `protectSeconds` stays loaded (`protectWaits`,
   on by default): a request for another model that needs its room waits
   instead of unloading it. Without this, an agent such as Hermes lost its
   model between two prompts: the moment the model went idle, a queued
   request for another model unloaded it, and the next prompt reloaded it
   from scratch, prompt cache and all.

## Waiting requests: Hold, Go next, Cancel

A request that waits says why on Schedule -> Live: held, waiting for room
(the model it needs is busy), or waiting for a protected model. Each running
request has buttons:

- **Hold** keeps a waiting request from unloading anything until
  **Release**. A model already loaded with room still serves it.
- **Go next** sends it now, past a hold, the one-at-a-time line or a
  protected model (which then unloads).
- **Cancel** stops it: the client gets an error and the model stops working
  on it.

With `protectWaits` on, a request for another model can wait up to
`protectSeconds` after the protected model's last request, so keep that
window near how long your clients pause between prompts (a tool call, a
human reading the answer). `protectWaits: false` brings back the old
behaviour, where protection only orders evictions.

## Per-card placement

With per-card sizes (`cards`, or detection when `vramGiB` is 0) the pool also
chooses **which card** each model runs on, and sets `HIP_VISIBLE_DEVICES` when
the model starts:

- A model that fits on one card goes on one card. Of the cards with room it
  picks the one with the fewest models, then the most free VRAM, so the cards
  share the work and each model gets a whole card's compute and bandwidth.
- A model bigger than a card is split over the cards with the most room.
- Fragmentation counts: 22 GiB free in total is no use for a 28 GiB model when
  neither card has 28 free, so the pool evicts until one card does.
- A `HIP_VISIBLE_DEVICES` in a model's `env` limits the cards it may use. A
  pin to more than one card (`HIP_VISIBLE_DEVICES=0,1`) means use all of them.
- A `cmd` with `--split-mode tensor` or `row` (`-sm`) is spread the same way over
  every card it may use, since those modes exist to share each layer across
  GPUs. `layer` (llama-server's default) and `none` still fit on one card.
- How much room that needs depends on the mode, because llama.cpp picks the cut
  points differently: with no `--tensor-split`, `tensor` divides the model
  evenly, so every card it uses has to hold its exact share, while `layer` and
  `row` split in proportion to each card's free memory, so what has to cover the
  model is the cards' free space together.
- A model too big for one card is placed on the cards with the most room, and
  again together they have to cover it - not half each, which turns away a model
  they can hold as soon as one card is a little tighter than the other.
- Card ids come from `rocm-smi`, the numbering `HIP_VISIBLE_DEVICES` uses. If
  `rocm-smi` is missing the pool falls back to `/sys/class/drm`, numbers the
  cards that have VRAM 0, 1, ... in that order (HIP's usual order, whatever
  the DRM `cardN` numbers are) and warns. Set `cards` to be sure.
- Set `placement: off` to plan by total VRAM only.

Do not hard-code a card split such as `-ts 1,1` or `--device` in a model's
`cmd` when the pool may put it on one card; the pool decides the cards.

## Other apps on the GPUs

Every 5 seconds the pool reads how much VRAM each card really has in use
(the same reading as the Performance page: LACT, `rocm-smi` or `/sys/class/drm`) and takes away what its own running models
account for. The rest belongs to something Argos did not start: a game,
ComfyUI, a llama-server from another container. The pool plans around it, so a
model is not placed on a card that only looked empty. The first `reserveGiB`
(split over the cards) covers the driver and a desktop; only usage beyond it
takes room from models.

- The smallest of the last four readings counts, so a model still unloading is
  not taken for another app. New usage by another app counts within about 20
  seconds.
- `GET /api/pool` shows it per card as `other_gib`, with `live_used_gib` the
  card's own reading, and `free_gib` is what is left for Argos. Schedule -> Live
  draws it as a hatched **other apps** segment on the card.
- If an Argos model holds more than its `vram` says, the extra shows as other
  apps too. Raise that model's `vram` so the plan matches.

## Model sizes

Set `models.*.vram` in GiB, including the KV cache for your context size.
Or put the sizes in the pool block, which lets a separate file in
`-config-dir` supply them without redefining any model:

```yaml
routing:
  router:
    use: pool
    settings:
      pool:
        cards: [32, 32]
        sizes: {qwen3-30b: 22.5, whisper: 4}
```

`models.*.vram` wins over `pool.sizes`, and either wins over the estimate.
Without it the size is estimated from the `-m` file (weights + 20% + 1 GiB;
split GGUFs are summed). A model with no `vram` and no `-m` file has an
unknown size and runs alone, like the group default. The log warns about each
one at startup.

## Watching it: GET /api/pool

`GET /api/pool` reports what the pool is doing right now: each card's planned
use and the models on it, every model's state, size and card, the requests
queued (with how long each has waited) and the models loading. With any other
router it answers `{"enabled": false}`. Model sizes there are the pool's plan;
each card's `live_used_gib` and `other_gib` are read live (see Other apps on the
GPUs).

The builder's **Schedule -> Live** shows this next to a **Requests** feed: the
engine's newest requests (chat, embeddings, benchmarks, audio), who sent them,
the context in and out, speeds and status, and how many each model is working
on now. **Usage -> GPU** has the same feed under its charts with **Recent
requests** ticked. Only requests through the engine appear; a client talking to
a llama-server port directly bypasses it.

Each model also reports what its llama-server slots are doing
(`models[].slots`), read from its log: a slot reading a prompt (prefill, with
tokens done of total and speed) or generating the answer (tokens out, speed
now). A model run with `-np 2` serves two chats at once, so it can show one
slot generating while the other reads a new prompt. Live lists every busy slot
under **Working now** and gives each running request its slot's status; a
request with no free slot shows **waiting**, and a busy slot no request through
Argos accounts for shows **not through Argos**.

A model that exits without being asked to (a crash, or a failed start) is
reported in `models[].crash` with the first fatal line it printed
(`GGML_ASSERT`, a segfault, out of memory, a GPU error). After three crashes in
ten minutes, requests for it get a 503 naming that line instead of starting it
again; **Load** from Schedule -> Live (`POST /api/crashes/clear/<model>` on the
engine) lets it start again. `GET /api/inflight` lists every
request still running, and the same state is on MCP as `schedule__pool_status`,
`schedule__requests_in_flight` and `schedule__recent_requests`.

## Small helpers on the CPU: cpuFallback

A model listed in `cpuFallback` that does not fit on the GPUs beside what is
loaded is started on the CPU instead of unloading anything: with every GPU
hidden (`HIP_VISIBLE_DEVICES=-1`, `CUDA_VISIBLE_DEVICES=-1`), so llama.cpp finds
none and runs it on the CPU whatever its `-ngl`. It holds no VRAM there and
never causes an eviction; Live marks it **on CPU**. Its next load goes back to
the GPUs if they have room then.

```yaml
        cpuFallback: ["qwen3-0.6b", "nomic-embed-text-v2-q8"]
```

The builder fills it with the assistant's chat and embedding models
(Settings -> Assistant -> "Run the chat and embedding models on the CPU when the
GPUs are full", on by default). What goes wrong: a llama.cpp build with
GPU-only features in the command (a KV cache type with no CPU kernels, `-sm
tensor`) can fail on the CPU; give such a model a plain command.

## Two chats on one model: answer first

A model run with `-np 2` (or more) serves several chats at once, and
llama.cpp builds each GPU batch from all of them: one token for a chat that is
answering, up to a whole batch of a chat reading its prompt. While one chat
reads a long prompt, the other's answer drops from tens of tokens a second to
one or two, until the prompt is read. Neither chat is held; they share the GPU,
and the answer gets the small share.

Argos cannot split one batch differently or pause a slot inside llama-server,
but it can choose when a request goes in. With **answer first** on for a model
(Schedule -> Plan, or Live -> ⋯), a long new prompt waits while another chat on
that model is answering, then goes as soon as the answer ends (an agent's turns
leave a gap at every tool call). Live shows it as **held**.

```yaml
routing:
  router:
    use: pool
    settings:
      pool:
        answerFirst:
          models: ["qwen-coder--2chat"]
          minPromptTokens: 4000   # shorter new prompts go straight in
          maxWaitSeconds: 300     # the longest a prompt waits
```

Two answers at once can be slow too: on some builds two chats generating
together run at a quarter of the speed each. **One at a time** (Plan, or Live
-> ⋯) lets one request into the model and queues the rest in Argos, in order,
so each runs at the speed it has alone. With `-np 2` that still pays: each
conversation keeps its own slot's cache, so switching chats does not re-read
the prompt. It replaces answer first for that model.

```yaml
        oneAtATime: ["qwen-coder--2chat"]
```

A waiting request shows **held** on Live with its place in line; **Go next**
sends it first (to the front of the line, or past an answer-first hold).

## Two models on one GPU: share guard

Two different models are not held for each other by default: each gets its
own requests. On a card they share, the GPU takes turns between the two
processes, so one model's work slows the other's answer there, and a model
split across both cards is as slow as its slowest card.

How much depends on the pair, so Argos learns it from the logs: each model's
answer speed (its slots' `tg_3s`) when nothing else on its cards is busy, and
its speed while a given other model is busy. Live -> **Sharing a GPU** shows
it ("40 t/s alone, 8 t/s beside the 10 GB model: −80%"). With the **share
guard** on (Plan), a new request for a model waits while another model on the
same GPU is answering, if that one was seen to slow down more than
`maxSlowdownPercent` beside it; it goes when the answer ends, after
`maxWaitSeconds`, or on **Go next**.

```yaml
        shareGuard:
          enabled: true
          maxSlowdownPercent: 50
          maxWaitSeconds: 120
```

It learns by watching, so the first time two models overlap nothing is held;
after three readings of a pair it acts on them. A model answering two chats of
its own is not sampled (it slows itself), and models on different cards never
hold each other.

The size is the prompt's *new* part, estimated at four bytes a token: the next
turn of a conversation the model already has cached counts only its new
messages, so it is not held. What goes wrong:

- **A chat waits a long time:** the other chat writes a long answer. Lower
  `maxWaitSeconds`, or turn answer first off for that model.
- **A short turn is held anyway:** it came from a conversation Argos has not
  seen in its last 16 requests to the model, so all of it counted as new.
- **It never holds:** it reads which chat is answering from the model's log;
  a llama-server that does not log its slots (`print_timing` lines) is never
  seen answering.

## What goes wrong

- **Out of memory on load:** a `vram` value is too low, or the estimate missed
  a large context. Raise `vram` or `reserveGiB`.
- **Models still swap out:** check the startup log for "unknown VRAM size",
  and for "could not detect VRAM" (set `cards` or `vramGiB`).
- **A model lands on the wrong card:** the log shows "gpu placement: binding
  ..." for every start. Check the ids against `rocm-smi`.
- **A model unloads after a few minutes:** that is its `ttl`, not the pool.
  See `guides/model-runtime/ttl-and-unloading`.
