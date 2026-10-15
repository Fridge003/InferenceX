#!/usr/bin/env bash
set -eo pipefail

# The RAPL energy counters are root-only by default (CVE-2020-8694). The CPU
# power exporter runs on the bare host as the job user, so open them for reading.
# The mode resets on reboot; nothing else about the host changes.
zones=(/sys/class/powercap/intel-rapl/intel-rapl:[0-9]*/energy_uj)
if [[ ! -e "${zones[0]}" ]]; then
    echo "[$(hostname -s)] no RAPL package zones under /sys/class/powercap" >&2
    exit 1
fi
sudo -n chmod o+r "${zones[@]}"
for zone in "${zones[@]}"; do
    echo "[$(hostname -s)] $(cat "$(dirname "$zone")/name") energy_uj mode $(stat -c %a "$zone")"
done
