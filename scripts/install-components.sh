#!/usr/bin/env bash
# Installs what both testbeds share: metrics-server at a 15s resolution and VPA 1.7.1 with
# in-place updates enabled. The recommender's and updater's flags are set per run by `vpaexp run`.
#
# usage: scripts/install-components.sh <kube-context> [--skip-metrics-server] [--replace-vpa]
#
# --replace-vpa removes an existing VPA installation of another version first. That deletes
# the VPA CRDs and with them EVERY VerticalPodAutoscaler object on the cluster.
set -euo pipefail

CTX="${1:?usage: install-components.sh <kube-context> [--skip-metrics-server] [--replace-vpa]}"
shift
SKIP_METRICS_SERVER=0
REPLACE_VPA=0
for arg in "$@"; do
  case "$arg" in
    --skip-metrics-server) SKIP_METRICS_SERVER=1 ;;
    --replace-vpa) REPLACE_VPA=1 ;;
    *) echo "unknown option $arg"; exit 2 ;;
  esac
done
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

if [[ "$SKIP_METRICS_SERVER" == 0 ]]; then
  $K apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
fi
set_arg metrics-server --metric-resolution 15s
set_arg metrics-server --kubelet-insecure-tls ""
$K -n kube-system rollout status deployment/metrics-server --timeout=180s

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
# The VPA scripts use the current context; give them a private kubeconfig instead of switching the user's.
kubectl config view --raw --minify --context "$CTX" > "$WORK/kubeconfig"
installed="$($K -n kube-system get deployment vpa-recommender \
  -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)"
if [[ "$installed" == *":$VPA_VERSION" ]]; then
  echo "VPA $VPA_VERSION is already installed ($installed)"
else
  git clone --depth 1 --branch "vertical-pod-autoscaler-$VPA_VERSION" \
    https://github.com/kubernetes/autoscaler.git "$WORK/autoscaler"
  cd "$WORK/autoscaler/vertical-pod-autoscaler"
  if [[ -n "$installed" ]]; then
    if [[ "$REPLACE_VPA" == 0 ]]; then
      echo "Another VPA is installed ($installed). Replacing it deletes every VPA object on the cluster:"
      $K get verticalpodautoscalers.autoscaling.k8s.io -A || true
      echo "Re-run with --replace-vpa to proceed."
      exit 1
    fi
    KUBECONFIG="$WORK/kubeconfig" ./hack/vpa-down.sh || true
  fi
  KUBECONFIG="$WORK/kubeconfig" ./hack/vpa-up.sh
  cd - >/dev/null
fi
set_arg vpa-admission-controller --feature-gates InPlace=true
set_arg vpa-updater --feature-gates InPlace=true
for d in vpa-recommender vpa-updater vpa-admission-controller; do
  $K -n kube-system rollout status "deployment/$d" --timeout=180s
done

$K create namespace vpaexp --dry-run=client -o yaml | $K apply -f -
echo "components installed:"
$K -n kube-system get deployments -o custom-columns=NAME:.metadata.name,IMAGE:.spec.template.spec.containers[0].image \
  | grep -E 'NAME|vpa-|metrics-server'
