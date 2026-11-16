#!/usr/bin/env bash
# Build locally, ship images into k3s containerd on 192.168.1.2, apply manifests.
# Needs the ult-server-ops root wrapper (root-run.sh) and ssh key access as runner.
# Secrets are generated on the server on first deploy and never leave it.
set -euo pipefail

cd "$(dirname "$0")/../.."
RR="${RR:-$HOME/.claude/skills/ult-server-ops/scripts/root-run.sh}"
SSH_TARGET="${ULT_HOST:-runner@192.168.1.2}"
SSH_PORT="${ULT_PORT:-42222}"
GO2RTC_IMAGE="${GO2RTC_IMAGE:-alexxit/go2rtc:1.9.14}"
SHA="$(git rev-parse --short HEAD)$(git diff --quiet HEAD -- . ':!plans' || echo -dirty)"
APP_IMAGE="camviewer/app:${SHA}"
BUNDLE="/tmp/camviewer-images.tar.gz"
MANIFEST="/tmp/camviewer-k3s.yaml"

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

echo "== import + apply (as root)"
REMOTE="$(mktemp)"
cat > "${REMOTE}" <<EOS
set -e
gunzip -c ${BUNDLE} | k3s ctr images import -
kubectl create namespace camviewer --dry-run=client -o yaml | kubectl apply -f - >/dev/null
if ! kubectl -n camviewer get secret camviewer-secrets >/dev/null 2>&1; then
  FERNET="\$(python3 -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"
  SESSION="\$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')"
  kubectl -n camviewer create secret generic camviewer-secrets \
    --from-literal=CAMVIEWER_SECRET_KEY="\${FERNET}" --from-literal=SESSION_SECRET="\${SESSION}" >/dev/null
  echo "secret created"
fi
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
