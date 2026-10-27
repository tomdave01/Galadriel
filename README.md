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

Run the test suite with `python -m pytest -q`.

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
