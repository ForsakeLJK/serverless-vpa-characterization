#!/usr/bin/env bash
# Prototype testbed (spec section 2): minikube profile vpa-serverless on the Mac.
# Starts the cluster with API server auditing, builds the function image inside it,
# and installs metrics-server and VPA.
set -euo pipefail
cd "$(dirname "$0")/.."

PROFILE="vpa-serverless"

# minikube copies ~/.minikube/files/<path> into the VM; /etc/ssl/certs is mounted into the API server.
mkdir -p "$HOME/.minikube/files/etc/ssl/certs"
cp scripts/audit-policy.yaml "$HOME/.minikube/files/etc/ssl/certs/audit-policy.yaml"

# The audit log goes to the API server's stdout; cluster/prototype.toml reads it with kubectl logs.
minikube start -p "$PROFILE" --driver=vfkit --container-runtime=containerd \
  --kubernetes-version=v1.37.0 --cpus=4 --memory=8g \
  --extra-config=apiserver.audit-policy-file=/etc/ssl/certs/audit-policy.yaml \
  --extra-config=apiserver.audit-log-path=-

minikube -p "$PROFILE" image build -t vpaexp-function:1 function/
minikube -p "$PROFILE" addons enable metrics-server
scripts/install-components.sh "$PROFILE" --skip-metrics-server "$@"

echo
echo "Function URL for cluster/prototype.toml: http://$(minikube -p "$PROFILE" ip):30080/"
leftovers="$(kubectl --context "$PROFILE" get vpa,pods -A --no-headers 2>/dev/null \
  | grep -E 'primes-vpa|base64-vpa|vpaexp-sampler' || true)"
if [[ -n "$leftovers" ]]; then
  echo "WARNING: objects from earlier experiments are still on the cluster. Runs must be"
  echo "single-tenant (spec section 2); delete these before running:"
  echo "$leftovers"
fi
