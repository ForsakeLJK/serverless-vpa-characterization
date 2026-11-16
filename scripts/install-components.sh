#!/usr/bin/env bash
# Installs what both testbeds share: metrics-server at a 15s resolution and VPA 1.7.1 with
# in-place updates enabled. The recommender's and updater's flags are set per run by `vpaexp run`.
#
# usage: scripts/install-components.sh <kube-context> [--skip-metrics-server]
set -euo pipefail

CTX="${1:?usage: install-components.sh <kube-context> [--skip-metrics-server]}"
VPA_VERSION="1.7.1"
K="kubectl --context $CTX"

# set_arg <deployment> <--flag> <value>: set or replace one argument of a kube-system deployment.
set_arg() {
  $K -n kube-system get deployment "$1" -o json | python3 -c '
import json, sys
flag, value = sys.argv[1], sys.argv[2]
c = json.load(sys.stdin)["spec"]["template"]["spec"]["containers"][0]
args = [a for a in c.get("args", []) if not a.startswith(flag + "=") and a != flag]
args.append(f"{flag}={value}" if value else flag)
print(json.dumps([{"op": "add", "path": "/spec/template/spec/containers/0/args", "value": args}]))
' "$2" "$3" | xargs -0 $K -n kube-system patch deployment "$1" --type=json -p
}

if [[ "${2:-}" != "--skip-metrics-server" ]]; then
  $K apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
fi
set_arg metrics-server --metric-resolution 15s
set_arg metrics-server --kubelet-insecure-tls ""
$K -n kube-system rollout status deployment/metrics-server --timeout=180s

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
git clone --depth 1 --branch "vertical-pod-autoscaler-$VPA_VERSION" \
  https://github.com/kubernetes/autoscaler.git "$WORK/autoscaler"
(
  cd "$WORK/autoscaler/vertical-pod-autoscaler"
  kubectl config use-context "$CTX"
  ./hack/vpa-down.sh >/dev/null 2>&1 || true   # replace any other VPA installation
  ./hack/vpa-up.sh
)
set_arg vpa-admission-controller --feature-gates InPlace=true
set_arg vpa-updater --feature-gates InPlace=true
for d in vpa-recommender vpa-updater vpa-admission-controller; do
  $K -n kube-system rollout status "deployment/$d" --timeout=180s
done

$K create namespace vpaexp --dry-run=client -o yaml | $K apply -f -
echo "components installed:"
$K -n kube-system get deployments -o custom-columns=NAME:.metadata.name,IMAGE:.spec.template.spec.containers[0].image \
  | grep -E 'NAME|vpa-|metrics-server'
