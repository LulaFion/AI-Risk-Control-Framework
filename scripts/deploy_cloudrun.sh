#!/usr/bin/env bash
# Deploy the riskdet pipeline as a Cloud Run JOB (batch, runs to completion).
#
# Status (2026-09-01): VERIFIED end-to-end. The job builds, pushes, deploys and
# runs as the dedicated own-project runtime SA `riskdet-runtime@acp-develop`,
# reaching BOTH BigQuery (acp-develop) and Cloud Logging (acp-prod). `check`
# passes (execution Completed / exit 0).
set -euo pipefail

PROJECT=acp-develop
REGION=us-central1
ACCOUNT=fion@lulainno.com
JOB=riskdet-check                       # test job: runs `python -m riskdet check`
RUNTIME_SA=riskdet-runtime@acp-develop.iam.gserviceaccount.com   # least-privilege runtime identity

gcloud config set account "$ACCOUNT"

# --- ONE-TIME IAM (done; recorded here for reproducibility; needs an IAM admin) -
# 1) Cloud Build (compute) SA must push images:
#    PN=$(gcloud projects describe $PROJECT --format='value(projectNumber)')
#    gcloud projects add-iam-policy-binding $PROJECT \
#      --member="serviceAccount:${PN}-compute@developer.gserviceaccount.com" \
#      --role="roles/artifactregistry.writer"
# 2) Runtime SA roles (acp-develop): bigquery.jobUser(user), bigquery.dataViewer,
#    bigquery.dataEditor(OMG_riskdet), logging.logWriter, storage.objectAdmin(bucket).
# 3) Runtime SA log read (acp-prod): roles/logging.viewer   <-- NOT viewAccessor;
#    listing entries needs logging.logEntries.list. (+privateLogViewer if the
#    spin-server logs are data-access/_Required.)
#    gcloud projects add-iam-policy-binding acp-prod \
#      --member="serviceAccount:${RUNTIME_SA}" --role="roles/logging.viewer"
# fion already holds Service Account User (project) -> can actAs the runtime SA.

# --- 1) build from source + deploy the job as the runtime SA ------------------
# `--args check` overrides the image CMD to run the readiness check.
gcloud run jobs deploy "$JOB" \
  --source . --region "$REGION" --project "$PROJECT" \
  --service-account "$RUNTIME_SA" \
  --set-env-vars RISKDET_BQ_PROJECT=acp-develop,RISKDET_LOG_PROJECT=acp-prod \
  --max-retries 0 --task-timeout 600s --memory 2Gi \
  --args check

# --- 2) execute to completion and stream the result --------------------------
gcloud run jobs execute "$JOB" --region "$REGION" --project "$PROJECT" --wait

# --- 3) cleanup the test job (optional) --------------------------------------
# gcloud run jobs delete "$JOB" --region "$REGION" --project "$PROJECT" -q

# =============================================================================
# $0 DRY-RUN scan (validates scan SQL + BQ access, bills no bytes):
#   gcloud run jobs update "$JOB" --region "$REGION" --project "$PROJECT" \
#     --set-env-vars RISKDET_BQ_PROJECT=acp-develop,RISKDET_LOG_PROJECT=acp-prod,RISKDET_DRY_RUN_ONLY=1 \
#     --args run,--as-of,2026-09-01T00:00:00
#   gcloud run jobs execute "$JOB" --region "$REGION" --project "$PROJECT" --wait
#
# REAL SCAN (after approval; first calibration/extract ~ $0.66). Mount the SAME
# shared bucket so candidates.jsonl lands where the L4 job will read it:
#   gcloud run jobs deploy riskdet-scan \
#     --source . --region "$REGION" --project "$PROJECT" \
#     --service-account "$RUNTIME_SA" \
#     --set-env-vars RISKDET_BQ_PROJECT=acp-develop,RISKDET_LOG_PROJECT=acp-prod,RISKDET_OUT_DIR=/data/out,RISKDET_ARTIFACT_DIR=/data/output \
#     --add-volume=name=data,type=cloud-storage,bucket=$SHARED_BUCKET \
#     --add-volume-mount=volume=data,mount-path=/data --execution-environment=gen2 \
#     --task-timeout 3600s --max-retries 1 --memory 4Gi --cpu 2 \
#     --args run,--as-of,2026-09-01T00:00:00
#
# SCHEDULE (daily): wrap the execute in a Cloud Scheduler -> Cloud Run Jobs trigger.
# =============================================================================


# =============================================================================
# LAYER 4 on Cloud Run -- the LLM agent chain (investigator -> skeptic ->
# report-composer). SEPARATE image (Dockerfile.l4) + SEPARATE job (riskdet-l4).
# Runs AFTER a scan: reads the scan's out/candidates.jsonl, gates it, investigates
# the human_review queue, writes output/cases/*.md + the daily digest.
#
# BACKEND (this config): Claude via the ANTHROPIC API KEY (billed to your Anthropic
# account), injected from Secret Manager -- NO Vertex, NO Model Garden for Claude.
# The digest composer runs on Gemini via Vertex ADC (no key, no Model Garden step).
#
# The two jobs form the pipeline:   riskdet-scan (deterministic) -> riskdet-l4 (LLM)
# =============================================================================
L4_JOB=riskdet-l4
L4_REGION=us-central1
L4_IMAGE="${L4_REGION}-docker.pkg.dev/${PROJECT}/riskdet/${L4_JOB}:v1"

# --- Claude via the Anthropic API KEY (Secret Manager) -----------------------
L4_BACKEND="anthropic"                        # anthropic (API key) | vertex
ANTHROPIC_SECRET="anthropic-api-key"          # Secret Manager secret (already exists)
# Public Anthropic model ids (NOT the Vertex @version form):
L4_MODEL="claude-sonnet-5"                     # primary (ANTHROPIC_MODEL)
L4_SMALL_MODEL="claude-haiku-4-5-20251001"     # background model

# --- Digest composer: Gemini via Vertex ADC (no key, no Model Garden) --------
L4_COMPOSER="gemini"                            # gemini | claude
L4_GEMINI_MODEL="gemini-3.7-flash"             # served from the `global` endpoint
L4_GEMINI_LOCATION="global"

# SHARED STORAGE (REQUIRED). Cloud Run jobs have NO shared filesystem, so the
# scan job's out/candidates.jsonl is invisible to the L4 job unless BOTH jobs
# mount the SAME GCS bucket via gcsfuse. Mounted at /data, which is exactly where
# both images already point RISKDET_OUT_DIR (/data/out) and RISKDET_ARTIFACT_DIR
# (/data/output) -- so scan writes candidates.jsonl to gs://$SHARED_BUCKET/out/
# and L4 reads it from the same place. With the mount, per-file GCS sync is
# redundant (artifacts already live in the bucket).
SHARED_BUCKET="riskdet-artifacts-acp-develop"                      
if [ -z "$SHARED_BUCKET" ]; then
  echo "ERROR: set SHARED_BUCKET to a GCS bucket both jobs will mount" >&2; exit 1
fi
VOL_ARGS=(
  --add-volume="name=data,type=cloud-storage,bucket=${SHARED_BUCKET}"
  --add-volume-mount="volume=data,mount-path=/data"
  --execution-environment=gen2          # cloud-storage volumes require gen2
)

# --- IAM for Layer 4 (API-key path) -- VERIFIED IN PLACE 2026-09-07 ----------
# 1) Claude key: runtime SA has roles/secretmanager.secretAccessor ON the secret
#    `anthropic-api-key` (confirmed). The job reads the key at runtime; no human
#    needs read access. NO Model Garden step on this path (API key, not Vertex).
# 2) Gemini composer (Vertex ADC): runtime SA has roles/aiplatform.user
#    (confirmed). Gemini is first-party -> no Model Garden step.
# 3) Shared-bucket access for the runtime SA (gcsfuse read+write) -- grant if the
#    bucket is new:
#      gcloud storage buckets create gs://${SHARED_BUCKET} --location=$L4_REGION --project=$PROJECT
#      gcloud storage buckets add-iam-policy-binding gs://${SHARED_BUCKET} \
#        --member="serviceAccount:${RUNTIME_SA}" --role="roles/storage.objectAdmin"
# Runtime SA already has BigQuery read + acp-prod logging.viewer (investigator pull-logs).
# NOTE: confirm the secret's :latest version is a FRESH key (not one exposed in chat).
#       Rotate: printf '%s' "sk-ant-..." | gcloud secrets versions add anthropic-api-key --data-file=-

# --- Artifact Registry repo (idempotent) -------------------------------------
gcloud artifacts repositories create riskdet \
  --repository-format=docker --location="$L4_REGION" --project="$PROJECT" \
  --description="riskdet images" 2>/dev/null || true

# --- 1) build + push the L4 image from Dockerfile.l4 -------------------------
gcloud builds submit --config cloudbuild.l4.yaml --project "$PROJECT" \
  --substitutions=_IMAGE="$L4_IMAGE"

# --- 2) deploy the L4 job as the runtime SA ----------------------------------
# Claude uses the API key (from --set-secrets); Gemini composer uses Vertex ADC.
L4_ENV="RISKDET_BQ_PROJECT=${PROJECT},RISKDET_LOG_PROJECT=acp-prod"
L4_ENV="${L4_ENV},RISKDET_L4_BACKEND=${L4_BACKEND}"
L4_ENV="${L4_ENV},RISKDET_OUT_DIR=/data/out,RISKDET_ARTIFACT_DIR=/data/output"
[ -n "$L4_MODEL" ]        && L4_ENV="${L4_ENV},RISKDET_L4_MODEL=${L4_MODEL}"
[ -n "$L4_SMALL_MODEL" ]  && L4_ENV="${L4_ENV},RISKDET_L4_SMALL_MODEL=${L4_SMALL_MODEL}"
[ -n "$L4_COMPOSER" ]     && L4_ENV="${L4_ENV},RISKDET_L4_COMPOSER=${L4_COMPOSER}"
[ -n "$L4_GEMINI_MODEL" ] && L4_ENV="${L4_ENV},RISKDET_L4_GEMINI_MODEL=${L4_GEMINI_MODEL}"
[ -n "$L4_GEMINI_LOCATION" ] && L4_ENV="${L4_ENV},RISKDET_L4_GEMINI_LOCATION=${L4_GEMINI_LOCATION}"

gcloud run jobs deploy "$L4_JOB" \
  --image "$L4_IMAGE" --region "$L4_REGION" --project "$PROJECT" \
  --service-account "$RUNTIME_SA" \
  --set-env-vars "$L4_ENV" \
  --set-secrets "ANTHROPIC_API_KEY=${ANTHROPIC_SECRET}:latest" \
  "${VOL_ARGS[@]}" \
  --task-timeout 3600s --max-retries 0 --memory 4Gi --cpu 2 \
  --args layer4,--as-of,2026-09-01T00:00:00,--dry-run     # dry-run first: gate only, $0

# --- 3) execute: dry-run (gate only, no LLM) to prove wiring -----------------
gcloud run jobs execute "$L4_JOB" --region "$L4_REGION" --project "$PROJECT" --wait

# REAL L4 RUN (after the dry-run is green):
#   gcloud run jobs update "$L4_JOB" --region "$L4_REGION" --project "$PROJECT" \
#     --args layer4,--as-of,2026-09-01T00:00:00           # drop --dry-run -> spends tokens
#   gcloud run jobs execute "$L4_JOB" --region "$L4_REGION" --project "$PROJECT" --wait
#
# ORDER: run riskdet-scan FIRST (writes gs://$SHARED_BUCKET/out/candidates.jsonl),
# then riskdet-l4 reads it from the same mount. A daily Cloud Scheduler should
# chain them (scan -> on success -> l4). Per-case cost is bounded by
# RISKDET_L4_BUDGET_USD (default $1) and RISKDET_L4_MAX_TURNS (default 40); cap
# volume with --max-cases N. Claude tokens bill to your Anthropic account; the
# Gemini composer bills to GCP (tiny).
# =============================================================================
