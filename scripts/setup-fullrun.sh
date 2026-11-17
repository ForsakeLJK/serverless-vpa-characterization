#!/usr/bin/env bash
# Full-run testbed (spec sections 2 and 9): single-node kubeadm cluster on the ARM Linux server.
#
# NOT YET RUN ON THE TARGET MACHINE. It assumes Ubuntu/Debian (apt), systemd, cgroup v2,
# two NUMA nodes, and root via sudo. Read it before running it, and run the checks it prints.
#
# Layout: Kubernetes and every pod on NUMA node 0; the load generator and collectors pin
# themselves to NUMA node 1 (client_numa_node in cluster/full.toml).
set -euo pipefail
cd "$(dirname "$0")/.."

K8S_MINOR="v1.37"
K8S_VERSION="v1.37.0"
NODE0_CPUS="$(cat /sys/devices/system/node/node0/cpulist)"
NODE1_CPUS="$(cat /sys/devices/system/node/node1/cpulist)"

echo "== prerequisites"
sudo swapoff -a
sudo modprobe overlay && sudo modprobe br_netfilter
printf 'net.bridge.bridge-nf-call-iptables=1\nnet.ipv4.ip_forward=1\n' | sudo tee /etc/sysctl.d/99-k8s.conf >/dev/null
sudo sysctl --system >/dev/null
[[ "$(stat -fc %T /sys/fs/cgroup)" == "cgroup2fs" ]] || { echo "cgroup v2 is required"; exit 1; }

echo "== containerd, kubeadm, kubelet, kubectl"
sudo apt-get update
sudo apt-get install -y containerd apt-transport-https ca-certificates curl gpg git openssl python3
sudo mkdir -p /etc/containerd /etc/apt/keyrings
containerd config default | sed 's/SystemdCgroup = false/SystemdCgroup = true/' | sudo tee /etc/containerd/config.toml >/dev/null
curl -fsSL "https://pkgs.k8s.io/core:/stable:/$K8S_MINOR/deb/Release.key" | sudo gpg --dearmor --yes -o /etc/apt/keyrings/kubernetes.gpg
echo "deb [signed-by=/etc/apt/keyrings/kubernetes.gpg] https://pkgs.k8s.io/core:/stable:/$K8S_MINOR/deb/ /" \
  | sudo tee /etc/apt/sources.list.d/kubernetes.list >/dev/null
sudo apt-get update
# Pin the packages to K8S_VERSION: the repo also carries later patch releases, and a kubelet
# newer than the control plane fails kubeadm's preflight.
PKG_VERSION="${K8S_VERSION#v}-*"
sudo apt-get install -y --allow-downgrades --allow-change-held-packages \
  "kubelet=$PKG_VERSION" "kubeadm=$PKG_VERSION" "kubectl=$PKG_VERSION"
sudo apt-mark hold kubelet kubeadm kubectl

echo "== NUMA placement: kubelet, containerd and everything they start on node 0 ($NODE0_CPUS)"
for unit in containerd kubelet; do
  sudo mkdir -p "/etc/systemd/system/$unit.service.d"
  printf '[Service]\nCPUAffinity=%s\nNUMAPolicy=bind\nNUMAMask=0\n' "$NODE0_CPUS" \
    | sudo tee "/etc/systemd/system/$unit.service.d/10-numa.conf" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl restart containerd

echo "== kubeadm init (stock kubelet; API server audit log enabled)"
sudo mkdir -p /etc/kubernetes/audit /var/log/kubernetes/audit
sudo cp scripts/audit-policy.yaml /etc/kubernetes/audit/policy.yaml
cat <<YAML | sudo tee /etc/kubernetes/kubeadm-vpaexp.yaml >/dev/null
apiVersion: kubeadm.k8s.io/v1beta4
kind: ClusterConfiguration
kubernetesVersion: $K8S_VERSION
networking:
  podSubnet: 10.244.0.0/16
apiServer:
  extraArgs:
    - {name: audit-policy-file, value: /etc/kubernetes/audit/policy.yaml}
    - {name: audit-log-path, value: /var/log/kubernetes/audit/audit.log}
    - {name: audit-log-maxsize, value: "2000"}
    - {name: audit-log-maxbackup, value: "20"}
  extraVolumes:
    - {name: audit-policy, hostPath: /etc/kubernetes/audit, mountPath: /etc/kubernetes/audit, readOnly: true, pathType: Directory}
    - {name: audit-log, hostPath: /var/log/kubernetes/audit, mountPath: /var/log/kubernetes/audit, readOnly: false, pathType: DirectoryOrCreate}
---
apiVersion: kubelet.config.k8s.io/v1beta1
kind: KubeletConfiguration
cgroupDriver: systemd
# Node 1 belongs to the client, so it is taken out of node allocatable.
reservedSystemCPUs: "$NODE1_CPUS"
YAML
sudo kubeadm init --config /etc/kubernetes/kubeadm-vpaexp.yaml
mkdir -p "$HOME/.kube"
sudo cp /etc/kubernetes/admin.conf "$HOME/.kube/config"
sudo chown "$(id -u):$(id -g)" "$HOME/.kube/config"
CTX="$(kubectl config current-context)"
kubectl taint nodes --all node-role.kubernetes.io/control-plane- || true
kubectl apply -f https://github.com/flannel-io/flannel/releases/latest/download/kube-flannel.yml
kubectl wait --for=condition=Ready node --all --timeout=300s

echo "== function image (built with nerdctl + buildkit into containerd's k8s.io namespace)"
if ! command -v nerdctl >/dev/null; then
  echo "Install nerdctl-full (https://github.com/containerd/nerdctl/releases, arm64), start buildkit, then run:"
  echo "  sudo nerdctl --namespace k8s.io build -t vpaexp-function:1 function/"
  echo "and re-run this script from 'install-components' on: scripts/install-components.sh $CTX"
  exit 1
fi
sudo nerdctl --namespace k8s.io build -t vpaexp-function:1 function/

scripts/install-components.sh "$CTX"

cat <<MSG

Setup finished. Context for cluster/full.toml: $CTX
Check before the smoke tests:
  1. Pods are confined to NUMA node 0. With the function deployed:
       pid=\$(pgrep -f /app/server.py | head -1); taskset -cp \$pid; numastat -p \$pid
     Expect CPU list $NODE0_CPUS and memory on node 0 only.
  2. Node allocatable CPU is 64: kubectl get node -o jsonpath='{.items[0].status.allocatable.cpu}'
  3. The audit log is written: sudo tail -1 /var/log/kubernetes/audit/audit.log
MSG
