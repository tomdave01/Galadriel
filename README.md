# Overseer

Local-first football match intelligence demo for the Inside the Game hackathon.

## Run locally

Install the small runtime dependency set once:

```powershell
python -m pip install -r requirements.txt
```

Start the dashboard and API with one command:

```powershell
python -m uvicorn api.main:app --reload
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). If the LLM environment
variables are not configured, narration automatically uses the visible MOCK
templates; to explicitly force that mode, set `LLM_MOCK=1` before launching.
The default replay is the seeded synthetic match and runs at 50×. The speed,
mode, language, and favourite team/player can be changed while it is running.

To use an offline-processed video clip before the synthetic match, set
`CLIP_EVENTS_PATH` and `CLIP_FRAMES_PATH` to its schema-compatible JSON files.
The video panel can also preview a local video file; playback synchronization
uses event `t` values when available.

## API

- `GET /state` returns the full current match snapshot.
- `GET /stream` streams compact state and agent updates over server-sent events.
- `POST /control` accepts replay action and presentation preferences.
- `POST /fan-query` compiles a natural-language request into matching events.
- `GET /frame?minute=...` returns the closest frame for moment replay.

Install the clean-machine test dependencies and run all tests plus the
ten-minute MOCK end-to-end smoke check:

```powershell
python -m pip install -r requirements-test.txt
python -m pytest -q
python tests\smoke.py
```

## Microsoft Agent Framework and Azure deployment

The existing synchronous analysts remain in `agents/analysts.py`; a thin
Microsoft Agent Framework functional workflow in `agents/workflow.py` runs them
in the existing dependency order over the same `MatchState`. `Orchestrator.tick`
remains synchronous for the replay engine. The workflow function/step API is
currently marked experimental by Microsoft.

Dependency assumptions in `requirements.txt`:

- `agent-framework-core==1.23.0` (Microsoft Agent Framework core/workflows
  package; this exact release is installed and exercised by the tests).
- `microsoft-opentelemetry==1.5.0` (Microsoft OpenTelemetry distro; repository
  release installed and exercised by the tests).
- `azure-ai-translation-text==2.0.0` (Azure AI Translator Python SDK; official
  Azure SDK release installed for the optional Translator integration).
- `fastapi` and `uvicorn` remain version-range dependencies as before.

The current Agent Framework overview uses `pip install agent-framework`, while
this project installs the lower-level `agent-framework-core` package directly
to use its functional workflow API without a model-provider integration. The
three versions above are the exact packages tested here, not Microsoft Learn
compatibility recommendations; verify them again before upgrading. The
functional workflow API is experimental. Azure Monitor export/tracing is
initialized only when `APPLICATIONINSIGHTS_CONNECTION_STRING` is set. Each
agent call emits an OpenTelemetry span with the agent name, match minute/source,
event/frame counts, bounded output counts, duration, and failure status; raw frames,
narration prompts, and credentials are not added as span attributes.

Azure AI Translator is opt-in (`AZURE_TRANSLATOR_ENABLED=1`) and requires
`AZURE_TEXT_TRANSLATION_ENDPOINT`, `AZURE_TEXT_TRANSLATION_APIKEY`, and
`AZURE_TEXT_TRANSLATION_REGION`. In that mode narration is generated in English
and then translated; known club/player names are shielded and restored. With
the flag unset, narration behavior is unchanged.

For local development, copy `.env.example` to `.env` and set values in your
shell (the application does not load dotenv files automatically). To build and
run the backend container:

```bash
docker build -t overseer-api .
docker run --rm -p 8000:8000 --env-file .env overseer-api
```

The API serves a local dashboard too. For a separate Static Web Apps frontend,
`web/config.js` supplies `window.OVERSEER_API_BASE_URL`; deployment below
replaces it with the Container App URL. Set the API's
`CORS_ALLOWED_ORIGINS` to the Static Web App origin. Local default CORS is
permissive (`*`); configure the production origin explicitly.

### Deploy backend to Azure Container Apps and frontend to Static Web Apps

The following exact Azure CLI flow uses Bash (including Azure Cloud Shell).
It creates a resource group, ACR, Application Insights resource, Container Apps
environment/app, and Static Web App. Names must be globally unique where Azure
requires it.

```bash
az login
az extension add --name containerapp --upgrade --allow-preview true
az provider register --namespace Microsoft.App
az provider register --namespace Microsoft.OperationalInsights
az provider register --namespace Microsoft.Insights
az provider register --namespace Microsoft.ContainerRegistry

RG=overseer-rg
LOCATION=eastus
SUFFIX=$(date +%s)
ACR="overseeracr${SUFFIX}"
AI="overseer-ai-${SUFFIX}"
ENV="overseer-env"
APP="overseer-api"
SWA="overseer-web-${SUFFIX}"

az group create --name "$RG" --location "$LOCATION"
az acr create --resource-group "$RG" --name "$ACR" --sku Basic --admin-enabled true
az acr build --registry "$ACR" --image overseer-api:latest .
ACR_USER=$(az acr credential show --name "$ACR" --query username -o tsv)
ACR_PASSWORD=$(az acr credential show --name "$ACR" --query passwords[0].value -o tsv)

az monitor app-insights component create \
  --app "$AI" --location "$LOCATION" --resource-group "$RG" \
  --application-type web
AI_CONNECTION=$(az monitor app-insights component show \
  --app "$AI" --resource-group "$RG" --query connectionString -o tsv)

az containerapp env create \
  --name "$ENV" --resource-group "$RG" --location "$LOCATION"
az containerapp create \
  --name "$APP" --resource-group "$RG" --environment "$ENV" \
  --image "$ACR.azurecr.io/overseer-api:latest" --target-port 8000 \
  --ingress external --registry-server "$ACR.azurecr.io" \
  --registry-username "$ACR_USER" --registry-password "$ACR_PASSWORD" \
  --secrets ai-connection-string="$AI_CONNECTION" \
  --env-vars LLM_MOCK=1 APPLICATIONINSIGHTS_CONNECTION_STRING=secretref:ai-connection-string
API_URL="https://$(az containerapp show --name "$APP" --resource-group "$RG" --query properties.configuration.ingress.fqdn -o tsv)"

az staticwebapp create \
  --name "$SWA" --resource-group "$RG" --location "$LOCATION" --sku Free
SWA_URL="https://$(az staticwebapp show --name "$SWA" --resource-group "$RG" --query defaultHostname -o tsv)"
az containerapp update --name "$APP" --resource-group "$RG" \
  --set-env-vars "CORS_ALLOWED_ORIGINS=$SWA_URL"

npm install --global @azure/static-web-apps-cli
SWA_TOKEN=$(az staticwebapp secrets list \
  --name "$SWA" --resource-group "$RG" --query properties.apiKey -o tsv)
mkdir -p dist
cp -R web/. dist/
printf "window.OVERSEER_API_BASE_URL = '%s';\n" "$API_URL" > dist/config.js
swa deploy ./dist --deployment-token "$SWA_TOKEN" --env production
```

This deployment uses ACR's admin credential to keep the initial CLI sequence
copy-pasteable; for production, replace it with a managed identity authorized
for `AcrPull`. The Translator credentials are intentionally not deployed in
this default MOCK configuration. If enabling translation in Azure, add the
three Translator values as Container Apps secrets and reference them in
`--env-vars`; do not bake credentials into the image or frontend.

## Offline video pipeline

The vision pipeline is optional, runs offline, and expects a short wide-angle
MP4 with no camera cuts. Install its separate dependencies (YOLO downloads its
small model weights on first use):

```powershell
python -m pip install -r vision\requirements.txt
```

1. Copy [vision/config/clip1.json](./vision/config/clip1.json) for each clip.
   Set `clip_id`, `match_id`, source image dimensions, and the four visible
   pitch-corner points in `reference_points.image` ordered top-left,
   top-right, bottom-right, bottom-left. The corresponding `pitch` points map
   those corners to the 0–100 pitch. The example corners are sample calibration
   values and should be measured on the actual video.
2. Detect people and the sports ball, assigning persistent ByteTrack IDs:

   ```powershell
   python -m vision.track clips\clip1.mp4 --device cpu
   ```

   This writes `outputs\clip1_tracks.json`. Add each visible player's tracker
   ID, confirmed team, and role to `vision\roster_map.json`. Confirm which
   color cluster belongs to each team; the map's `team_clusters` entries name
   the deterministic KMeans cluster labels.
3. Generate normalized frames, events, player speed/distance metrics, and the
   annotated review video:

   ```powershell
   python -m vision.events_from_tracks clips\clip1.mp4 --config vision\config\clip1.json --roster-map vision\roster_map.json --tracks outputs\clip1_tracks.json --device cpu
   ```

   Outputs are `outputs\clip1_events.json`, `outputs\clip1_frames.json`,
   `outputs\clip1_player_metrics.json`, and `outputs\clip1_annotated.mp4`.
   Omit `--tracks` to run YOLO/ByteTrack as part of this command. Both event
   and frame JSON files use the same schema as synthetic data.
4. To manually correct detections-derived events, edit the generated events
   JSON and load it with `--hand-correct`:

   ```powershell
   python -m vision.events_from_tracks clips\clip1.mp4 --config vision\config\clip1.json --roster-map vision\roster_map.json --tracks outputs\clip1_tracks.json --hand-correct outputs\clip1_events.json
   ```

   Corrected event files are validated against the shared Event schema before
   being used. The annotated MP4 is rebuilt using the corrected labels.
5. Replay this clip before the synthetic match in Overseer. The video file is
   served locally and the player follows the clip's event/frame `t` values:

   ```powershell
   $env:CLIP_EVENTS_PATH = "outputs\clip1_events.json"
   $env:CLIP_FRAMES_PATH = "outputs\clip1_frames.json"
   $env:CLIP_VIDEO_PATH = "clips\clip1.mp4"
   python -m uvicorn api.main:app --reload
   ```

   Alternatively, pass `--clip-events`, `--clip-frames`, and `--clip-video`
   to `python -m engine.run_replay`.

### Google Colab (CPU, no GPU)

Upload the clip and its per-clip calibration JSON to a Colab runtime, then run
these notebook cells. CPU is supported; processing will be slower than a GPU.

```python
!git clone https://github.com/tomdave01/Overseer.git
%cd Overseer
!pip install -r vision/requirements.txt
from google.colab import files
uploaded = files.upload()  # Select clip1.mp4 and clip1.json
```

Copy the uploaded calibration file into the project's config directory and use
the CPU device explicitly:

```python
!cp clip1.json vision/config/clip1.json
!python -m vision.track clip1.mp4 --device cpu --output outputs/clip1_tracks.json
```

Edit `vision/roster_map.json` for the detected tracker IDs, then create the
outputs:

```python
!python -m vision.events_from_tracks clip1.mp4 --config vision/config/clip1.json --roster-map vision/roster_map.json --tracks outputs/clip1_tracks.json --output-dir outputs --device cpu
from google.colab import files
files.download("outputs/clip1_events.json")
files.download("outputs/clip1_frames.json")
files.download("outputs/clip1_annotated.mp4")
```
