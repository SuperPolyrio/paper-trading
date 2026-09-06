#!/usr/bin/env bash
set -euo pipefail

project=""
network="default"
db_cidr=""
apply=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) project="$2"; shift 2 ;;
    --network) network="$2"; shift 2 ;;
    --db-cidr) db_cidr="$2"; shift 2 ;;
    --apply) apply=1; shift ;;
    --dry-run) apply=0; shift ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; exit 64 ;;
  esac
done
if [[ -z "$project" || -z "$db_cidr" ]]; then
  printf 'usage: %s --project PROJECT_ID --db-cidr CIDR [--network NAME] [--apply]\n' "$0" >&2
  exit 64
fi

paper_sa="poly-quant-paper-worker@${project}.iam.gserviceaccount.com"
run() {
  printf 'COMMAND'
  printf ' %q' "$@"
  printf '\n'
  if [[ "$apply" == "1" ]]; then
    "$@"
  fi
}

ensure_firewall() {
  local name="$1"
  local priority="$2"
  local action="$3"
  local rules="$4"
  local ranges="$5"
  if [[ "$apply" == "1" ]] && gcloud compute firewall-rules describe "$name" \
      --project "$project" >/dev/null 2>&1; then
    run gcloud compute firewall-rules update "$name" \
      --project "$project" --priority "$priority" --action "$action" \
      --rules "$rules" --destination-ranges "$ranges" \
      --target-service-accounts "$paper_sa"
    return
  fi
  run gcloud compute firewall-rules create "$name" \
    --project "$project" --network "$network" --direction EGRESS \
    --priority "$priority" --action "$action" --rules "$rules" \
    --destination-ranges "$ranges" --target-service-accounts "$paper_sa"
}

ensure_firewall poly-quant-paper-egress-db 900 ALLOW tcp:5432 "$db_cidr"
ensure_firewall poly-quant-paper-egress-dns-metadata 905 ALLOW \
  tcp:53,udp:53,tcp:80 169.254.169.254/32
ensure_firewall poly-quant-paper-egress-https 910 ALLOW tcp:443 0.0.0.0/0
ensure_firewall poly-quant-paper-egress-deny 1000 DENY all 0.0.0.0/0

printf 'NOTE HTTPS is required for public CLOB reads and cannot distinguish HTTP paths at VPC layer.\n'
printf 'NOTE live-order prevention is enforced by no live secrets plus the read-only paper client.\n'
