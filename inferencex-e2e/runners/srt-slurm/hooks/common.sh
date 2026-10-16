#!/usr/bin/env bash

# Shared host checks. Sourcing this file only defines functions.

# Poll VRAM usage every 10s for up to 15 minutes. A stricter threshold is useful
# when the engine sizes its KV cache from device-wide free memory.
wait_for_amd_gpu_clean() {
    local threshold="${1:-10}"
    local gpu_clean=false vram_max i
    for i in $(seq 1 90); do
        vram_max=$(rocm-smi --showmemuse 2>/dev/null \
            | grep -oE "GPU Memory Allocated \(VRAM%\): [0-9]+" \
            | awk '{if ($NF > m) m = $NF} END {print m+0}')
        if [ "${vram_max:-0}" -le "$threshold" ]; then
            echo "GPUs clean (vram%max=$vram_max <= $threshold after $((i * 10))s)"
            gpu_clean=true
            break
        fi
        echo "waiting for prior-job GPU memory reclaim: vram%max=$vram_max (target <= $threshold)"
        sleep 10
    done
    if [ "$gpu_clean" != "true" ]; then
        echo "Error: GPUs still draining prior job's memory after 15min" >&2
        return 1
    fi
}

# The RAPL energy counters are root-only by default (CVE-2020-8694), and the CPU
# power exporter runs on the bare host as the job user. The mode resets on reboot.
# CPU power is best-effort, so a host without the counters or sudo only warns.
open_rapl_counters() {
    local zones zone
    zones=(/sys/class/powercap/intel-rapl/intel-rapl:[0-9]*/energy_uj)
    if [[ ! -e "${zones[0]}" ]]; then
        echo "[$(hostname -s)] no RAPL package zones under /sys/class/powercap"
        return 0
    fi
    if ! sudo -n chmod o+r "${zones[@]}"; then
        echo "[$(hostname -s)] could not open the RAPL energy counters; no CPU power" >&2
        return 0
    fi
    for zone in "${zones[@]}"; do
        echo "[$(hostname -s)] $(cat "$(dirname "$zone")/name") energy_uj mode $(stat -c %a "$zone")"
    done
}
