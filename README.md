# FrameSeek Basketball Film Room

A desktop workspace for questions, cited video frames and contextual follow-ups.

## Current scope

The default synthetic parcel demo uses preset answers and sends no model requests.
The sports landing images are illustrative. Upload, media preflight, private
playback and durable investigations work locally. An earlier limited basketball
run used NVIDIA Nemotron 3.5 Super VL on Nebius Token Factory; its results were
partial. The newer local Qwen visual-index experiment is a separate route.
Fresh-judge live sports analysis, exhaustive event counts and identity tracking
are not established.

## Run locally

Use Python 3.13, uv, and FFmpeg/ffprobe.

```sh
uv sync --frozen --python 3.13
make run
```

Open http://127.0.0.1:8765/ and choose Open workspace. The English judge guide
is /static/demo-guide-en.html. Try `When is the parcel in the center?`, replay
00:06, choose Ask a follow-up, ask `What happens next?`, and replay 00:13.
These two answers are explicitly synthetic presets, not live recognition.

The application starts one API/worker process per workspace. Private sources,
frames and investigations require their existing session ownership. Do not
serve the private data directory as static files.

## NVIDIA and Nebius route

Nemotron 3.5 Super VL through Token Factory provides visual inspection and
answer generation. Qwen3 Embedding 8B provides text embeddings. A private
runtime must explicitly permit the provider's fixed media origin and include
verified configuration and calibrated limits; both owner and deployment
approvals must be active. No configuration flag installs spending approvals.
The runtime examples in docs are deliberately blocked templates. Keep fixture
mode until real execution is separately configured and authorized.

## Verify and build

```sh
make test
make check
npm ci --ignore-scripts
npm test
npm run build
```

Node 20+ is needed for frontend development. The committed React bundle includes
third-party notices. Uploaded media and model results are never bundled here.

## License

Project source is available under the MIT License; see LICENSE. Existing font,
icon and React license notices remain in their asset folders.
