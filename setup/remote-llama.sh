#!/usr/bin/env bash
# remote-llama: make "start llama-server" mean "start it in ANOTHER container over SSH".
#
#   remote-llama <backend> <llama-server args...>
#   e.g. remote-llama rocm --host 0.0.0.0 --port 5801 -ngl 99 -m /app/models/x.gguf
#   remote-llama <backend> --bench <llama-bench args...>
#   runs llama-bench there instead (the Playground's llama-bench tab): no port, nothing forwarded
#   remote-llama <backend> --ps
#   lists the llama.cpp processes there with their memory and CPU time (Argos's CPU view)
#
# Argos runs this as the model's command. It:
#   * ssh's into the llama.cpp container for that backend and runs llama-server there
#   * forwards localhost:<port> here -> <port> there, so Argos's default proxy
#     (http://localhost:${PORT}) just works, for scanned and Odysseus models alike
#   * passes GPU-pinning env vars (HIP_VISIBLE_DEVICES, ...) across, since ssh drops them
#   * uses -tt so the remote llama-server gets a hangup when this process is stopped
#     (that is how Argos "unloads" it -- verify the VRAM is actually freed)
#
# Per backend NAME (rocm, cuda, custom) it reads, upper-cased:
#   <NAME>_SSH_HOST (required)  <NAME>_SSH_PORT (22)  <NAME>_SSH_USER (root)
#   <NAME>_SSH_BIN  (llama-server, path of the binary inside that container)
# Shared: REMOTE_SSH_KEY (/run/secrets/argos_ssh_key), REMOTE_ENV_PASSTHRU,
#         REMOTE_KNOWN_HOSTS (/data/ssh_known_hosts)
#
# REQUIREMENT: the models folder is mounted at the SAME path in both containers
# (/app/models), so ${MODEL_PATH} means the same file on both sides.
set -euo pipefail

[[ $# -ge 1 ]] || { echo "usage: remote-llama <backend> <llama-server args...>" >&2; exit 2; }
name="$1"; shift
NAME="${name^^}"

get() { local v="${NAME}_$1"; printf '%s' "${!v:-$2}"; }
host="$(get SSH_HOST "")"
[[ -n "$host" ]] || { echo "remote-llama: ${NAME}_SSH_HOST is not set" >&2; exit 2; }
user="$(get SSH_USER root)"; sport="$(get SSH_PORT 22)"; bin="$(get SSH_BIN llama-server)"
key="${REMOTE_SSH_KEY:-/run/secrets/argos_ssh_key}"
# Without the key ssh only says "Permission denied (publickey)", which reads like the far side's fault.
[[ -r "$key" ]] || { echo "remote-llama: no SSH key at $key. Set REMOTE_SSH_KEY_FILE in .env to the private key on the host and mount it there (docker-compose.override.yaml: \${REMOTE_SSH_KEY_FILE}:/run/secrets/argos_ssh_key:ro), then recreate the container." >&2; exit 2; }
known="${REMOTE_KNOWN_HOSTS:-/data/ssh_known_hosts}"
passthru="${REMOTE_ENV_PASSTHRU:-HIP_VISIBLE_DEVICES CUDA_VISIBLE_DEVICES ROCR_VISIBLE_DEVICES HSA_OVERRIDE_GFX_VERSION LLAMA_ENABLE_MTP_OPT}"

# --ps: what the llama.cpp processes there use, for Argos's CPU view (no tty, nothing forwarded):
# one line each, "<pid> <rss KiB> <cpu seconds so far> <command line>"
if [[ "${1:-}" == "--ps" ]]; then
  exec ssh -i "$key" -p "$sport" \
    -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$known" -o ConnectTimeout=5 \
    "${user}@${host}" "ps -eo pid=,rss=,times=,args= 2>/dev/null | grep -E '[l]lama-(server|bench)' || true"
fi

# llama-bench: the binary beside llama-server there (or <NAME>_SSH_BENCH_BIN), no port to forward
bench=0
if [[ "${1:-}" == "--bench" ]]; then
  bench=1; shift
  bbin="$(get SSH_BENCH_BIN "")"
  if [[ -z "$bbin" ]]; then
    if [[ "$bin" == */* ]]; then bbin="${bin%/*}/llama-bench"; else bbin="llama-bench"; fi
  fi
  bin="$bbin"
fi

# find the port llama-server will listen on
lport=""; args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  case "${args[i]}" in
    --port|-p) lport="${args[i+1]:-}" ;;
    --port=*)  lport="${args[i]#--port=}" ;;
  esac
done
[[ $bench == 1 || "$lport" =~ ^[0-9]+$ ]] || { echo "remote-llama: no --port <n> in the llama-server args" >&2; exit 2; }

# remote command: exec env K=V... <bin> <args...>, every word shell-quoted
rcmd="exec env"
for v in $passthru; do
  [[ -n "${!v:-}" ]] && rcmd+=" $(printf '%q' "$v=${!v}")"
done
rcmd+=" $(printf '%q' "$bin")"
for a in "${args[@]}"; do rcmd+=" $(printf '%q' "$a")"; done

if [[ $bench == 1 ]]; then
  exec ssh -tt -i "$key" -p "$sport" \
    -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$known" \
    -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
    "${user}@${host}" "$rcmd"
fi

exec ssh -tt -i "$key" -p "$sport" \
  -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$known" \
  -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes \
  -L "${lport}:127.0.0.1:${lport}" \
  "${user}@${host}" "$rcmd"
