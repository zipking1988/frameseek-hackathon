# FrameSeek Basketball Film Room

A desktop workspace for questions, cited video frames and contextual follow-ups.

## Current scope

The default basketball demo uses generated illustrations and preset answers; it
sends no model requests. The three still scenes do not establish continuous play,
player identity, or whether the shot scores. Upload, media preflight, private
playback and durable investigations work locally. An earlier limited basketball
run used NVIDIA Nemotron 3.5 Super VL on Nebius Token Factory; its separately
saved results were partial. The newer local Qwen visual-index experiment is a
separate route. Fresh-judge live sports analysis, exhaustive event counts and
identity tracking are not established. Check the workspace runtime status for
current model availability.

The application includes guarded request recovery, source-generation checks
and evidence checkpoint isolation. Historical partial checkpoints without enough
identity information cannot be restored automatically; completed answers remain
viewable. This public source repository excludes private media, account records,
operational credentials and budget approvals.

## Run locally

Use Python 3.13, uv, and FFmpeg/ffprobe.

```sh
uv sync --frozen --python 3.13
make run
```

Open http://127.0.0.1:8765/ and choose **Start basketball demo**. Select
`What is visible in the pass scene?` and send it. Replay the cited frame at
00:06, choose the suggested question `Where is the defender in the shot scene?`,
send it, and replay 00:13. Select `Does the shot score?` to see a partial answer
that explains the limit of the still image. Open Canvas to see the related
questions and evidence; refresh to return to the saved investigation. Selecting
a suggestion fills the composer; it does not send a question automatically.
The English judge guide is `/static/demo-guide-en.html`. These answers are
synthetic presets, not live recognition. Existing parcel investigations and
older clients remain supported for compatibility.

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
third-party notices. Demo asset provenance and regeneration instructions are in
`src/frameseek/static/basketball_demo/ASSETS.md`. Uploaded media and private
model results are never bundled here.

## License

Project source is available under the MIT License; see LICENSE. Existing font,
icon and React license notices remain in their asset folders.
