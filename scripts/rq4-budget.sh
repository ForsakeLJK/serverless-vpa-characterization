#!/usr/bin/env bash
# Run on the kubeadm ARM node, from the repo root, between runs.
set -euo pipefail
action=${1:-plan}
conf=/var/lib/kubelet/config.yaml
backup=/var/lib/kubelet/config.yaml.before-rq4
plan() { uv run vpaexp --profile full rq4-budget; }

case "$action" in
  plan) plan; exit ;;
  apply|restore) ;;
  *) echo "usage: $0 plan|apply|restore" >&2; exit 2 ;;
esac

# Serialize reservation changes. Keep the exact original until restoration succeeds.
exec 9>"/tmp/vpaexp-rq4-budget-$(id -u).lock"
flock -n 9 || { echo "Another budget change is running" >&2; exit 1; }
restore() {
  sudo cp --preserve=all "$backup" "$conf.rq4-tmp"
  sudo mv "$conf.rq4-tmp" "$conf"
  sudo systemctl restart kubelet
}
if [[ "$action" == restore ]]; then
  sudo test -f "$backup" || { echo "No RQ4 backup to restore" >&2; exit 1; }
  restore
  for attempt in {1..30}; do
    if sudo systemctl is-active --quiet kubelet && plan | python3 -c 'import json,sys; sys.exit(json.load(sys.stdin)["system_reserved_bytes"] != int(sys.argv[1]))' "$(sudo cat "$backup.reserved")"; then
      sudo rm -- "$backup" "$backup.reserved"
      echo "Verified the restored kubelet memory reservation."
      exit 0
    fi
    sleep 3
  done
  echo "Configuration restored; live verification failed. Backup retained." >&2
  exit 1
fi

state=$(plan)
printf '%s\n' "$state"
read -r desired current actual expected machine < <(
  python3 -c 'import json,sys; d=json.load(sys.stdin); print(*(d[k] for k in ("desired_system_reserved_bytes","system_reserved_bytes","effective_budget","target_budget","machine_id")))' <<<"$state"
)
[[ "$machine" == "$(cat /etc/machine-id)" ]] || { echo "Run on the configured Kubernetes node" >&2; exit 1; }
if [[ "$actual" == "$expected" ]]; then exit; fi
sudo test ! -e "$backup" || { echo "An RQ4 backup exists. Restore before reconfiguring." >&2; exit 1; }
sudo /usr/bin/python3 -c 'import yaml'
printf '%s\n' "$current" | sudo tee "$backup.reserved" >/dev/null
sudo cp --preserve=all "$conf" "$backup"
rollback() {
  echo "Restoring original kubelet configuration" >&2
  restore
}
trap rollback ERR
trap 'rollback; exit 130' INT TERM
sudo /usr/bin/python3 - "$conf" "$desired" <<'PY'
import os
import shutil
import sys
from pathlib import Path
import yaml

path = Path(sys.argv[1])
cfg = yaml.safe_load(path.read_text())
cfg.setdefault("systemReserved", {})["memory"] = sys.argv[2]
# Bound pod admission while preserving host RAM for the co-located control plane.
cfg["enforceNodeAllocatable"] = ["none"]
temp = path.with_suffix(".rq4-tmp")
shutil.copy2(path, temp)
temp.write_text(yaml.safe_dump(cfg, sort_keys=False))
os.replace(temp, path)
PY
sudo systemctl restart kubelet
for attempt in {1..30}; do
  if plan | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(d["effective_budget"] != d["target_budget"] or d["system_reserved_bytes"] != int(sys.argv[1]))' "$desired"; then
    trap - ERR INT TERM
    echo "Verified 6656 MiB for the functions. Original configuration saved at $backup."
    exit 0
  fi
  sleep 3
done
echo "Kubelet did not report the requested budget" >&2
false
