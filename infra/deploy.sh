#!/usr/bin/env bash
set -Eeuo pipefail

# Override these at invocation time to choose a subscription/region/name set.
LOCATION="${LOCATION:-eastus2}"
SWA_LOCATION="${SWA_LOCATION:-eastus2}"
RESOURCE_SUFFIX="${RESOURCE_SUFFIX:-$(date +%s | tail -c 5)}"
RESOURCE_GROUP="${RESOURCE_GROUP:-rg-overseer-${RESOURCE_SUFFIX}}"
LOG_WORKSPACE="${LOG_WORKSPACE:-law-overseer-${RESOURCE_SUFFIX}}"
APP_INSIGHTS="${APP_INSIGHTS:-appi-overseer-${RESOURCE_SUFFIX}}"
EVENTHUB_NAMESPACE_NAME="${EVENTHUB_NAMESPACE_NAME:-eh-overseer-${RESOURCE_SUFFIX}}"
EVENTHUB_NAME="${EVENTHUB_NAME:-match-events}"
COSMOS_ACCOUNT="${COSMOS_ACCOUNT:-cosmos-overseer-${RESOURCE_SUFFIX}}"
COSMOS_DATABASE="${COSMOS_DATABASE:-overseer}"
COSMOS_MATCH_CONTAINER="${COSMOS_MATCH_CONTAINER:-match-state}"
COSMOS_CACHE_CONTAINER="${COSMOS_CACHE_CONTAINER:-narration-cache}"
ACR_NAME="${ACR_NAME:-overseer${RESOURCE_SUFFIX}}"
ACA_ENVIRONMENT="${ACA_ENVIRONMENT:-acae-overseer-${RESOURCE_SUFFIX}}"
CONTAINER_APP="${CONTAINER_APP:-overseer-api-${RESOURCE_SUFFIX}}"
SIMULATOR_JOB="${SIMULATOR_JOB:-job-overseer-sim-${RESOURCE_SUFFIX}}"
PULL_IDENTITY="${PULL_IDENTITY:-id-overseer-acr-${RESOURCE_SUFFIX}}"
IMAGE_NAME="${IMAGE_NAME:-overseer-api}"
IMAGE_TAG="${IMAGE_TAG:-phase4}"
STATIC_WEB_APP="${STATIC_WEB_APP:-swa-overseer-${RESOURCE_SUFFIX}}"
CONSUMER_GROUP="${CONSUMER_GROUP:-\$Default}"
FOUNDRY_ROLE="${FOUNDRY_ROLE:-Foundry User}"
SIMULATOR_SPEED="${SIMULATOR_SPEED:-20}"

if [[ -n "${AZURE_SUBSCRIPTION_ID:-}" ]]; then
  az account set --subscription "$AZURE_SUBSCRIPTION_ID"
fi

if ! az account show --only-show-errors >/dev/null 2>&1; then
  echo "Run 'az login' and select an Azure subscription before deployment." >&2
  exit 1
fi
for command_name in az npx python3 curl; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Required command is missing: $command_name" >&2
    exit 1
  fi
done
if [[ -z "${FOUNDRY_PROJECT_ENDPOINT:-}" || -z "${FOUNDRY_MODEL:-}" ]]; then
  echo "Set FOUNDRY_PROJECT_ENDPOINT and FOUNDRY_MODEL before deployment." >&2
  exit 1
fi
if [[ -z "${FOUNDRY_RESOURCE_ID:-}" ]]; then
  echo "Set FOUNDRY_RESOURCE_ID to the Azure resource ID of the Foundry resource." >&2
  exit 1
fi

if [[ -n "${AZURE_SUBSCRIPTION_ID:-}" ]]; then
  SUBSCRIPTION_ID="$AZURE_SUBSCRIPTION_ID"
else
  SUBSCRIPTION_ID="$(az account show --query id -o tsv)"
fi
ACR_ID="/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${RESOURCE_GROUP}/providers/Microsoft.ContainerRegistry/registries/${ACR_NAME}"
COSMOS_DATA_CONTRIBUTOR_ROLE_ID="00000000-0000-0000-0000-000000000002"

assign_azure_role() {
  local principal_id="$1"
  local role_name="$2"
  local scope="$3"
  local existing
  existing="$(az role assignment list \
    --assignee-object-id "$principal_id" \
    --scope "$scope" \
    --query "[?roleDefinitionName=='${role_name}'].id | [0]" \
    -o tsv)"
  if [[ -z "$existing" || "$existing" == "None" ]]; then
    az role assignment create \
      --assignee-object-id "$principal_id" \
      --assignee-principal-type ServicePrincipal \
      --role "$role_name" \
      --scope "$scope" \
      --only-show-errors \
      -o none
  fi
}

echo "Creating resource group ${RESOURCE_GROUP} in ${LOCATION}..."
az group create \
  --name "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --only-show-errors \
  -o none

echo "Creating Log Analytics and Application Insights..."
az monitor log-analytics workspace create \
  --resource-group "$RESOURCE_GROUP" \
  --workspace-name "$LOG_WORKSPACE" \
  --location "$LOCATION" \
  --sku PerGB2018 \
  --retention-time 30 \
  --only-show-errors \
  -o none
WORKSPACE_ID="$(az monitor log-analytics workspace show \
  --resource-group "$RESOURCE_GROUP" \
  --workspace-name "$LOG_WORKSPACE" \
  --query customerId -o tsv)"
WORKSPACE_KEY="$(az monitor log-analytics workspace get-shared-keys \
  --resource-group "$RESOURCE_GROUP" \
  --workspace-name "$LOG_WORKSPACE" \
  --query primarySharedKey -o tsv)"
WORKSPACE_RESOURCE_ID="$(az monitor log-analytics workspace show \
  --resource-group "$RESOURCE_GROUP" \
  --workspace-name "$LOG_WORKSPACE" \
  --query id -o tsv)"
az monitor app-insights component create \
  --resource-group "$RESOURCE_GROUP" \
  --app "$APP_INSIGHTS" \
  --location "$LOCATION" \
  --kind web \
  --application-type web \
  --workspace "$WORKSPACE_RESOURCE_ID" \
  --only-show-errors \
  -o none
APP_INSIGHTS_CONNECTION_STRING="$(az monitor app-insights component show \
  --resource-group "$RESOURCE_GROUP" \
  --app "$APP_INSIGHTS" \
  --query connectionString -o tsv)"

echo "Creating Basic Event Hubs namespace and hub..."
az eventhubs namespace create \
  --resource-group "$RESOURCE_GROUP" \
  --name "$EVENTHUB_NAMESPACE_NAME" \
  --location "$LOCATION" \
  --sku Basic \
  --capacity 1 \
  --minimum-tls-version 1.2 \
  --only-show-errors \
  -o none
az eventhubs eventhub create \
  --resource-group "$RESOURCE_GROUP" \
  --namespace-name "$EVENTHUB_NAMESPACE_NAME" \
  --name "$EVENTHUB_NAME" \
  --partition-count 2 \
  --message-retention 1 \
  --only-show-errors \
  -o none
EVENTHUB_SCOPE="$(az eventhubs eventhub show \
  --resource-group "$RESOURCE_GROUP" \
  --namespace-name "$EVENTHUB_NAMESPACE_NAME" \
  --name "$EVENTHUB_NAME" \
  --query id -o tsv)"
EVENTHUB_FQDN="${EVENTHUB_NAMESPACE_NAME}.servicebus.windows.net"

echo "Creating serverless Cosmos DB and containers..."
az cosmosdb create \
  --resource-group "$RESOURCE_GROUP" \
  --name "$COSMOS_ACCOUNT" \
  --location "$LOCATION" \
  --kind GlobalDocumentDB \
  --capabilities EnableServerless \
  --default-consistency-level Session \
  --only-show-errors \
  -o none
az cosmosdb sql database create \
  --resource-group "$RESOURCE_GROUP" \
  --account-name "$COSMOS_ACCOUNT" \
  --name "$COSMOS_DATABASE" \
  --only-show-errors \
  -o none
az cosmosdb sql container create \
  --resource-group "$RESOURCE_GROUP" \
  --account-name "$COSMOS_ACCOUNT" \
  --database-name "$COSMOS_DATABASE" \
  --name "$COSMOS_MATCH_CONTAINER" \
  --partition-key-path /match_id \
  --only-show-errors \
  -o none
az cosmosdb sql container create \
  --resource-group "$RESOURCE_GROUP" \
  --account-name "$COSMOS_ACCOUNT" \
  --database-name "$COSMOS_DATABASE" \
  --name "$COSMOS_CACHE_CONTAINER" \
  --partition-key-path /cache_key \
  --only-show-errors \
  -o none
COSMOS_ENDPOINT="$(az cosmosdb show \
  --resource-group "$RESOURCE_GROUP" \
  --name "$COSMOS_ACCOUNT" \
  --query documentEndpoint -o tsv)"

echo "Creating registry and managed identity for image pulls..."
az acr create \
  --resource-group "$RESOURCE_GROUP" \
  --name "$ACR_NAME" \
  --location "$LOCATION" \
  --sku Basic \
  --admin-enabled false \
  --only-show-errors \
  -o none
ACR_LOGIN_SERVER="$(az acr show \
  --resource-group "$RESOURCE_GROUP" \
  --name "$ACR_NAME" \
  --query loginServer -o tsv)"
ACR_RESOURCE_ID="$(az acr show \
  --resource-group "$RESOURCE_GROUP" \
  --name "$ACR_NAME" \
  --query id -o tsv)"
az identity create \
  --resource-group "$RESOURCE_GROUP" \
  --name "$PULL_IDENTITY" \
  --location "$LOCATION" \
  --only-show-errors \
  -o none
PULL_IDENTITY_ID="$(az identity show \
  --resource-group "$RESOURCE_GROUP" \
  --name "$PULL_IDENTITY" \
  --query id -o tsv)"
PULL_PRINCIPAL_ID="$(az identity show \
  --resource-group "$RESOURCE_GROUP" \
  --name "$PULL_IDENTITY" \
  --query principalId -o tsv)"
assign_azure_role "$PULL_PRINCIPAL_ID" "AcrPull" "$ACR_RESOURCE_ID"
sleep 10
az acr build \
  --registry "$ACR_NAME" \
  --image "${IMAGE_NAME}:${IMAGE_TAG}" \
  . \
  --only-show-errors

echo "Creating Static Web App and Container Apps environment..."
az staticwebapp create \
  --resource-group "$RESOURCE_GROUP" \
  --name "$STATIC_WEB_APP" \
  --location "$SWA_LOCATION" \
  --sku Free \
  --only-show-errors \
  -o none
SWA_HOST="$(az staticwebapp show \
  --resource-group "$RESOURCE_GROUP" \
  --name "$STATIC_WEB_APP" \
  --query defaultHostname -o tsv)"
SWA_ORIGIN="https://${SWA_HOST}"

az containerapp env create \
  --resource-group "$RESOURCE_GROUP" \
  --name "$ACA_ENVIRONMENT" \
  --location "$LOCATION" \
  --logs-workspace-id "$WORKSPACE_ID" \
  --logs-workspace-key "$WORKSPACE_KEY" \
  --only-show-errors \
  -o none

echo "Creating the single-replica processor/API app..."
env_vars=(
  "EVENTHUB_NAMESPACE=${EVENTHUB_FQDN}"
  "EVENTHUB_NAME=${EVENTHUB_NAME}"
  "EVENTHUB_CONSUMER_GROUP=${CONSUMER_GROUP}"
  "COSMOS_ENDPOINT=${COSMOS_ENDPOINT}"
  "COSMOS_DATABASE=${COSMOS_DATABASE}"
  "COSMOS_MATCH_STATE_CONTAINER=${COSMOS_MATCH_CONTAINER}"
  "COSMOS_NARRATION_CACHE_CONTAINER=${COSMOS_CACHE_CONTAINER}"
  "MATCH_SNAPSHOT_INTERVAL_SECONDS=${MATCH_SNAPSHOT_INTERVAL_SECONDS:-30}"
  "FOUNDRY_PROJECT_ENDPOINT=${FOUNDRY_PROJECT_ENDPOINT}"
  "FOUNDRY_MODEL=${FOUNDRY_MODEL}"
  "CORS_ALLOWED_ORIGINS=${SWA_ORIGIN}"
  "PORT=8000"
)
if [[ -n "${AZURE_TRANSLATOR_ENABLED:-}" ]]; then
  env_vars+=("AZURE_TRANSLATOR_ENABLED=${AZURE_TRANSLATOR_ENABLED}")
fi
if [[ -n "${AZURE_TEXT_TRANSLATION_ENDPOINT:-}" ]]; then
  env_vars+=("AZURE_TEXT_TRANSLATION_ENDPOINT=${AZURE_TEXT_TRANSLATION_ENDPOINT}")
fi
if [[ -n "${AZURE_TEXT_TRANSLATION_REGION:-}" ]]; then
  env_vars+=("AZURE_TEXT_TRANSLATION_REGION=${AZURE_TEXT_TRANSLATION_REGION}")
fi
if [[ -n "${AZURE_CONTENT_SAFETY_ENABLED:-}" ]]; then
  env_vars+=("AZURE_CONTENT_SAFETY_ENABLED=${AZURE_CONTENT_SAFETY_ENABLED}")
fi
if [[ -n "${AZURE_CONTENT_SAFETY_ENDPOINT:-}" ]]; then
  env_vars+=("AZURE_CONTENT_SAFETY_ENDPOINT=${AZURE_CONTENT_SAFETY_ENDPOINT}")
fi
if [[ -n "${WEB_PUBSUB_HUB:-}" ]]; then
  env_vars+=("WEB_PUBSUB_HUB=${WEB_PUBSUB_HUB}")
fi
az containerapp create \
  --resource-group "$RESOURCE_GROUP" \
  --name "$CONTAINER_APP" \
  --environment "$ACA_ENVIRONMENT" \
  --image "${ACR_LOGIN_SERVER}/${IMAGE_NAME}:${IMAGE_TAG}" \
  --registry-server "$ACR_LOGIN_SERVER" \
  --registry-identity "$PULL_IDENTITY_ID" \
  --system-assigned \
  --user-assigned "$PULL_IDENTITY_ID" \
  --ingress external \
  --target-port 8000 \
  --transport auto \
  --min-replicas 0 \
  --max-replicas 1 \
  --cpu 1.0 \
  --memory 2Gi \
  --env-vars "${env_vars[@]}" \
  --only-show-errors \
  -o none
APP_PRINCIPAL_ID="$(az containerapp show \
  --resource-group "$RESOURCE_GROUP" \
  --name "$CONTAINER_APP" \
  --query identity.principalId -o tsv)"
ACA_FQDN="$(az containerapp show \
  --resource-group "$RESOURCE_GROUP" \
  --name "$CONTAINER_APP" \
  --query properties.configuration.ingress.fqdn -o tsv)"
API_ORIGIN="https://${ACA_FQDN}"

echo "Assigning managed identity data-plane roles..."
assign_azure_role "$APP_PRINCIPAL_ID" "Azure Event Hubs Data Receiver" "$EVENTHUB_SCOPE"
assign_azure_role "$APP_PRINCIPAL_ID" "Azure Event Hubs Data Sender" "$EVENTHUB_SCOPE"
COSMOS_ASSIGNMENT="$(az cosmosdb sql role assignment list \
  --resource-group "$RESOURCE_GROUP" \
  --account-name "$COSMOS_ACCOUNT" \
  --query "[?principalId=='${APP_PRINCIPAL_ID}' && roleDefinitionId=='${COSMOS_DATA_CONTRIBUTOR_ROLE_ID}'].id | [0]" \
  -o tsv)"
if [[ -z "$COSMOS_ASSIGNMENT" || "$COSMOS_ASSIGNMENT" == "None" ]]; then
  az cosmosdb sql role assignment create \
    --resource-group "$RESOURCE_GROUP" \
    --account-name "$COSMOS_ACCOUNT" \
    --scope "/" \
    --principal-id "$APP_PRINCIPAL_ID" \
    --role-definition-id "$COSMOS_DATA_CONTRIBUTOR_ROLE_ID" \
    --only-show-errors \
    -o none
fi
# Microsoft Foundry docs call this role "Foundry User" (formerly "Azure AI User").
# UNVERIFIED: tenant policy may require a different project/resource assignment scope.
assign_azure_role "$APP_PRINCIPAL_ID" "$FOUNDRY_ROLE" "$FOUNDRY_RESOURCE_ID"

echo "Creating on-demand simulator Container Apps Job..."
if ! az containerapp job show \
  --resource-group "$RESOURCE_GROUP" \
  --name "$SIMULATOR_JOB" \
  --only-show-errors >/dev/null 2>&1; then
  az containerapp job create \
    --resource-group "$RESOURCE_GROUP" \
    --name "$SIMULATOR_JOB" \
    --environment "$ACA_ENVIRONMENT" \
    --trigger-type Manual \
    --replica-timeout 7200 \
    --replica-retry-limit 0 \
    --replica-completion-count 1 \
    --parallelism 1 \
    --image "${ACR_LOGIN_SERVER}/${IMAGE_NAME}:${IMAGE_TAG}" \
    --registry-server "$ACR_LOGIN_SERVER" \
    --registry-identity "$PULL_IDENTITY_ID" \
    --mi-system-assigned \
    --mi-user-assigned "$PULL_IDENTITY_ID" \
    --command overseer-simulator \
    --env-vars \
      "EVENTHUB_NAMESPACE=${EVENTHUB_FQDN}" \
      "EVENTHUB_NAME=${EVENTHUB_NAME}" \
      "SIMULATOR_SPEED=${SIMULATOR_SPEED}" \
    --cpu 0.5 \
    --memory 1.0Gi \
    --only-show-errors \
    -o none
else
  echo "Simulator job already exists; retaining its current image and settings."
fi
SIMULATOR_PRINCIPAL_ID="$(az containerapp job show \
  --resource-group "$RESOURCE_GROUP" \
  --name "$SIMULATOR_JOB" \
  --query identity.principalId -o tsv)"
assign_azure_role "$SIMULATOR_PRINCIPAL_ID" "Azure Event Hubs Data Sender" "$EVENTHUB_SCOPE"

secret_args=("appinsights-connection=${APP_INSIGHTS_CONNECTION_STRING}")
secret_env_args=("APPLICATIONINSIGHTS_CONNECTION_STRING=secretref:appinsights-connection")
if [[ -n "${FOUNDRY_API_KEY:-}" ]]; then
  secret_args+=("foundry-api-key=${FOUNDRY_API_KEY}")
  secret_env_args+=("FOUNDRY_API_KEY=secretref:foundry-api-key")
fi
if [[ -n "${WEB_PUBSUB_CONNECTION:-}" ]]; then
  secret_args+=("web-pubsub-connection=${WEB_PUBSUB_CONNECTION}")
  secret_env_args+=("WEB_PUBSUB_CONNECTION=secretref:web-pubsub-connection")
fi
az containerapp secret set \
  --resource-group "$RESOURCE_GROUP" \
  --name "$CONTAINER_APP" \
  --secrets "${secret_args[@]}" \
  --only-show-errors \
  -o none
az containerapp update \
  --resource-group "$RESOURCE_GROUP" \
  --name "$CONTAINER_APP" \
  --set-env-vars "${secret_env_args[@]}" \
  --only-show-errors \
  -o none
sleep 30
az containerapp update \
  --resource-group "$RESOURCE_GROUP" \
  --name "$CONTAINER_APP" \
  --min-replicas 1 \
  --only-show-errors \
  -o none

echo "Waiting for API readiness..."
ready=false
for attempt in $(seq 1 36); do
  if curl --fail --silent "${API_ORIGIN}/state" >/dev/null; then
    ready=true
    break
  fi
  sleep 10
done
if [[ "$ready" != "true" ]]; then
  echo "API did not become ready at ${API_ORIGIN}/state; inspect Container Apps logs." >&2
  exit 1
fi

echo "Deploying static web assets with the API base URL..."
SWA_DEPLOYMENT_TOKEN="$(az staticwebapp secrets list \
  --resource-group "$RESOURCE_GROUP" \
  --name "$STATIC_WEB_APP" \
  --query properties.apiKey -o tsv)"
STAGING_DIR="$(mktemp -d)"
trap 'rm -rf "$STAGING_DIR"' EXIT
cp -R web/. "$STAGING_DIR/"
API_ORIGIN="$API_ORIGIN" python3 - "$STAGING_DIR/config.js" <<'PY'
import json
import os
import sys

with open(sys.argv[1], "w", encoding="utf-8") as config:
    config.write(
        "window.OVERSEER_API_BASE_URL = "
        + json.dumps(os.environ["API_ORIGIN"])
        + ";\n"
    )
PY
npx --yes @azure/static-web-apps-cli@latest deploy "$STAGING_DIR" \
  --deployment-token "$SWA_DEPLOYMENT_TOKEN" \
  --env production
unset SWA_DEPLOYMENT_TOKEN

cat <<SUMMARY

Deployment completed.
Resource group:       ${RESOURCE_GROUP}
Processor/API:        ${API_ORIGIN}
Simulator job:        ${SIMULATOR_JOB} (manual start)
Static Web App:       ${SWA_ORIGIN}
OBS browser source:   ${API_ORIGIN}/overlay
Event Hubs namespace: ${EVENTHUB_FQDN}
Event Hubs hub:       ${EVENTHUB_NAME}
Cosmos DB:            ${COSMOS_ACCOUNT}/${COSMOS_DATABASE}
Foundry endpoint:     ${FOUNDRY_PROJECT_ENDPOINT}
SUMMARY

echo "Start the on-demand simulator with:"
echo "  az containerapp job start --resource-group ${RESOURCE_GROUP} --name ${SIMULATOR_JOB}"
