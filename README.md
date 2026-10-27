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
