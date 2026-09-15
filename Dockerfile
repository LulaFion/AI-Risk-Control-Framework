# riskdet -- portable detection pipeline. Same code runs on Windows locally
# and in this container; there is no subprocess/bq/gcloud dependency anywhere.
#
# Credentials: NEVER baked into a layer (.dockerignore excludes *.json). In
# Cloud Run the job's service account provides ADC; locally mount a key and
# set RISKDET_KEY_FILE.
#
# Deploy target: Cloud Run JOBS (batch, runs to completion), not Services.
# The deploy command below is shipped UNEXECUTED -- current IAM holds only
# Cloud Run Invoker; deploying additionally needs roles/run.developer,
# roles/iam.serviceAccountUser (actAs), roles/artifactregistry.writer and
# a build path (roles/cloudbuild.builds.editor or a local docker push).
#
#   REGION=asia-southeast1
#   gcloud builds submit --tag $REGION-docker.pkg.dev/acp-prod/riskdet/riskdet:v1 .
#   gcloud run jobs deploy riskdet-scan \
#     --image=$REGION-docker.pkg.dev/acp-prod/riskdet/riskdet:v1 \
#     --region=$REGION --project=acp-prod \
#     --service-account=airc-740@acp-prod.iam.gserviceaccount.com \
#     --task-timeout=3600s --max-retries=1 --memory=4Gi --cpu=2 \
#     --set-env-vars=RISKDET_BQ_PROJECT=acp-develop,RISKDET_LOG_PROJECT=acp-prod \
#     --args=run,--as-of,2026-09-01T00:00:00

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

RUN adduser --disabled-password --gecos "" --uid 10001 riskdet

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY riskdet/ ./riskdet/
COPY config/ ./config/
COPY ["OMG Game Info.csv", "./OMG Game Info.csv"]

ENV RISKDET_ROOT=/app \
    RISKDET_OUT_DIR=/data/out \
    RISKDET_ARTIFACT_DIR=/data/output \
    RISKDET_CATALOG_CSV="/app/OMG Game Info.csv"

RUN mkdir -p /data/out /data/output && chown -R riskdet /data
USER riskdet

ENTRYPOINT ["python", "-m", "riskdet"]
CMD ["check"]
