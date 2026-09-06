#!/usr/bin/env bash
set -euo pipefail

project=""
apply=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) project="$2"; shift 2 ;;
    --apply) apply=1; shift ;;
    --dry-run) apply=0; shift ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; exit 64 ;;
  esac
done
if [[ -z "$project" ]]; then
  printf 'usage: %s --project PROJECT_ID [--dry-run|--apply]\n' "$0" >&2
  exit 64
fi

paper_sa="poly-quant-paper-worker@${project}.iam.gserviceaccount.com"
live_sa="poly-quant-live-calibration@${project}.iam.gserviceaccount.com"

run() {
  printf 'COMMAND'
  printf ' %q' "$@"
  printf '\n'
  if [[ "$apply" == "1" ]]; then
    "$@"
  fi
}

ensure_service_account() {
  local account_id="$1"
  local display_name="$2"
  local email="${account_id}@${project}.iam.gserviceaccount.com"
  if [[ "$apply" == "1" ]] && gcloud iam service-accounts describe "$email" \
      --project "$project" >/dev/null 2>&1; then
    printf 'EXISTS service-account %s\n' "$email"
    return
  fi
  run gcloud iam service-accounts create "$account_id" \
    --project "$project" --display-name "$display_name"
}

ensure_secret() {
  local secret="$1"
  if [[ "$apply" == "1" ]] && gcloud secrets describe "$secret" \
      --project "$project" >/dev/null 2>&1; then
    printf 'EXISTS secret %s\n' "$secret"
    return
  fi
  run gcloud secrets create "$secret" \
    --project "$project" --replication-policy automatic
}

ensure_service_account poly-quant-paper-worker "Poly Quant paper worker"
ensure_service_account poly-quant-live-calibration "Poly Quant live calibration"

ensure_secret poly-quant-paper-db-password
ensure_secret poly-quant-live-private-key
ensure_secret poly-quant-live-api-key
ensure_secret poly-quant-live-api-secret
ensure_secret poly-quant-live-api-passphrase
ensure_secret poly-quant-live-db-password

run gcloud secrets add-iam-policy-binding poly-quant-paper-db-password \
  --project "$project" \
  --member "serviceAccount:${paper_sa}" \
  --role roles/secretmanager.secretAccessor
run gcloud secrets add-iam-policy-binding poly-quant-live-private-key \
  --project "$project" \
  --member "serviceAccount:${live_sa}" \
  --role roles/secretmanager.secretAccessor
run gcloud secrets add-iam-policy-binding poly-quant-live-api-key \
  --project "$project" \
  --member "serviceAccount:${live_sa}" \
  --role roles/secretmanager.secretAccessor
run gcloud secrets add-iam-policy-binding poly-quant-live-api-secret \
  --project "$project" \
  --member "serviceAccount:${live_sa}" \
  --role roles/secretmanager.secretAccessor
run gcloud secrets add-iam-policy-binding poly-quant-live-api-passphrase \
  --project "$project" \
  --member "serviceAccount:${live_sa}" \
  --role roles/secretmanager.secretAccessor
run gcloud secrets add-iam-policy-binding poly-quant-live-db-password \
  --project "$project" \
  --member "serviceAccount:${live_sa}" \
  --role roles/secretmanager.secretAccessor

run gcloud projects add-iam-policy-binding "$project" \
  --member "serviceAccount:${paper_sa}" --role roles/logging.logWriter
run gcloud projects add-iam-policy-binding "$project" \
  --member "serviceAccount:${live_sa}" --role roles/logging.logWriter

printf 'VERIFY paper service account has no project-wide Secret Manager accessor and no live secret binding.\n'
printf 'VERIFY paper and live services run on separate VM identities; one VM service account is shared by every process on that VM.\n'
