#!/usr/bin/env bash
# Build locally, ship images into k3s containerd on 192.168.1.2, apply manifests.
# Needs the ult-server-ops root wrapper (root-run.sh) and ssh key access as runner.
# Secrets: CAMVIEWER_SECRETS_FILE points at a local env file OUTSIDE the repo with
#   SESSION_SECRET, APP_USERNAME, APP_PASSWORD_HASH, HIK_CONNECT_USER, HIK_CONNECT_PASSWORD
# It is copied to the server, turned into the k8s Secret and deleted. Values are never printed.
set -euo pipefail

cd "$(dirname "$0")/../.."
RR="${RR:-$HOME/.claude/skills/ult-server-ops/scripts/root-run.sh}"
SSH_TARGET="${ULT_HOST:-runner@192.168.1.2}"
SSH_PORT="${ULT_PORT:-42222}"
GO2RTC_IMAGE="${GO2RTC_IMAGE:-alexxit/go2rtc:1.9.14}"
SHA="$(git rev-parse --short HEAD)$(git diff --quiet HEAD -- . ':!plans' || echo -dirty)"
APP_IMAGE="camviewer/app:${SHA}"
SECRETS_FILE="${CAMVIEWER_SECRETS_FILE:-}"
REQUIRED_KEYS="SESSION_SECRET APP_USERNAME APP_PASSWORD_HASH HIK_CONNECT_USER HIK_CONNECT_PASSWORD"
BUNDLE="/tmp/camviewer-images.tar.gz"
REMOTE_ENV="/tmp/camviewer-secrets.env"
MANIFEST="/tmp/camviewer-k3s.yaml"

if [ -n "${SECRETS_FILE}" ]; then
  for key in ${REQUIRED_KEYS}; do
    grep -q "^${key}=." "${SECRETS_FILE}" || { echo "missing ${key} in secrets file" >&2; exit 2; }
  done
fi

echo "== build ${APP_IMAGE}"
docker build -t "${APP_IMAGE}" .
docker pull "${GO2RTC_IMAGE}"

echo "== ship images"
docker save "${APP_IMAGE}" "${GO2RTC_IMAGE}" | gzip \
  | ssh -p "${SSH_PORT}" "${SSH_TARGET}" "cat > ${BUNDLE}"

echo "== ship manifests"
{
  for f in namespace configmap-go2rtc pvc deployment service; do
    sed -e "s#__APP_IMAGE__#${APP_IMAGE}#" -e "s#__GO2RTC_IMAGE__#${GO2RTC_IMAGE}#" "deploy/k3s/${f}.yaml"
    echo "---"
  done
} | ssh -p "${SSH_PORT}" "${SSH_TARGET}" "cat > ${MANIFEST}"

if [ -n "${SECRETS_FILE}" ]; then
  echo "== ship secrets"
  ssh -p "${SSH_PORT}" "${SSH_TARGET}" "umask 077; cat > ${REMOTE_ENV}" < "${SECRETS_FILE}"
fi

echo "== import + apply (as root)"
REMOTE="$(mktemp)"
cat > "${REMOTE}" <<EOS
set -e
gunzip -c ${BUNDLE} | k3s ctr images import -
kubectl create namespace camviewer --dry-run=client -o yaml | kubectl apply -f - >/dev/null
if [ -f ${REMOTE_ENV} ]; then
  kubectl -n camviewer create secret generic camviewer-secrets --from-env-file=${REMOTE_ENV} \\
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  rm -f ${REMOTE_ENV}
  echo "secret applied"
fi
kubectl -n camviewer get secret camviewer-secrets >/dev/null || { echo "secret camviewer-secrets missing"; exit 1; }
kubectl apply -f ${MANIFEST}
kubectl -n camviewer rollout restart deployment/camviewer
kubectl -n camviewer rollout status deployment/camviewer --timeout=180s
rm -f ${BUNDLE} ${MANIFEST}
kubectl -n camviewer get pods,svc
EOS
bash "${RR}" -f "${REMOTE}"
rm -f "${REMOTE}"

echo "== check"
curl -fsS "http://192.168.1.2:30092/healthz" && echo
