"""Render a private demo cut; ElevenLabs narration is one explicit, cached request.

Run with system Python (Pillow/httpx) and FFmpeg. No FrameSeek model API is called.
"""

import argparse
import base64
import hashlib
import json
import math
import os
import re
import subprocess
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_CHARACTERS = 3600
MAX_RESPONSE_BYTES = 12 * 1024 * 1024
VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"
MODEL_ID = "eleven_multilingual_v2"


def script_text(scenes):
    if not isinstance(scenes, list) or not 1 <= len(scenes) <= 16:
        raise ValueError("A storyboard needs 1–16 scenes")
    parts = []
    for scene in scenes:
        text = scene.get("narration", "").strip()
        if not text or not scene.get("label"):
            raise ValueError("Every scene needs a label and narration")
        parts.append(text)
    text = "\n\n".join(parts)
    if len(text) > MAX_CHARACTERS:
        raise ValueError(f"Narration exceeds the {MAX_CHARACTERS}-character ceiling")
    return text


def load_voice_settings(path):
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("The private voice file must be a regular file with mode 0600")
    values = {}
    allowed = {"ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID", "ELEVENLABS_MODEL_ID"}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or name not in allowed or name in values:
            raise ValueError("Invalid or duplicate voice setting name")
        values[name] = value.strip().strip("\"'")
    key = values.get("ELEVENLABS_API_KEY", "")
    if not key:
        raise ValueError("Add ELEVENLABS_API_KEY to the private .env.elevenlabs file")
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,256}", key):
        raise ValueError("ELEVENLABS_API_KEY has invalid characters")
    voice = values.get("ELEVENLABS_VOICE_ID") or VOICE_ID
    model = values.get("ELEVENLABS_MODEL_ID") or MODEL_ID
    if not re.fullmatch(r"[A-Za-z0-9]{16,64}", voice) or model != MODEL_ID:
        raise ValueError("Use a valid stock voice ID and eleven_multilingual_v2")
    return key, voice, model


def timing_arrays(alignment, text):
    if not isinstance(alignment, dict) or "".join(alignment.get("characters", [])) != text:
        raise ValueError("Speech timing does not match the requested narration")
    starts = alignment.get("character_start_times_seconds", [])
    ends = alignment.get("character_end_times_seconds", [])
    if len(starts) != len(text) or len(ends) != len(text):
        raise ValueError("Incomplete speech timing")
    previous = 0.0
    for start, end in zip(starts, ends, strict=True):
        if any(isinstance(v, bool) or not isinstance(v, (float, int)) for v in (start, end)):
            raise ValueError("Invalid speech timing value")
        if not math.isfinite(start + end) or start < previous or end < start or end >= 178:
            raise ValueError("Speech timing is nonmonotonic or exceeds the video limit")
        previous = start
    return starts, ends


def private_json(path, value):
    data = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as file:
        file.write(data)


def narration_request(text, voice, model):
    return {
        "text": text,
        "model_id": model,
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": 0.15,
            "use_speaker_boost": True,
            "speed": 0.98,
        },
    }


def generate_voice(text, env_file, output):
    import httpx

    key, voice, model = load_voice_settings(env_file)
    payload = narration_request(text, voice, model)
    identity = {"voice_id": voice, **payload}
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    cache = output / "voice-cache"
    cache.mkdir(mode=0o700, exist_ok=True)
    receipt = cache / f"{fingerprint}.receipt.json"
    audio = cache / f"{fingerprint}.mp3"
    timing = cache / f"{fingerprint}.timing.json"
    if receipt.exists():
        saved = json.loads(receipt.read_text())
        if saved.get("state") != "completed" or not audio.exists() or not timing.exists():
            raise ValueError("A prior voice request is unresolved; it will not be sent again")
        if hashlib.sha256(audio.read_bytes()).hexdigest() != saved.get("audio_sha256"):
            raise ValueError("Cached narration failed its integrity check; no request sent")
        alignment = json.loads(timing.read_text())
        timing_arrays(alignment, text)
        return audio, alignment
    # Durable single-attempt receipt, written before the only POST. Never retry.
    descriptor = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as file:
        json.dump({"state": "dispatched", "characters": len(text), "requests": 1}, file)
        file.flush()
        os.fsync(file.fileno())
    try:
        with httpx.Client(follow_redirects=False, timeout=120, trust_env=False) as client:
            with client.stream(
                "POST",
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice}/with-timestamps",
                params={"output_format": "mp3_44100_128"},
                headers={"xi-api-key": key},
                json=payload,
            ) as response:
                if response.status_code != 200:
                    raise ValueError(f"ElevenLabs returned HTTP {response.status_code}")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise ValueError("ElevenLabs response exceeded the local size limit")
        result = json.loads(body)
        alignment = result.get("alignment")
        timing_arrays(alignment, text)
        decoded = base64.b64decode(result["audio_base64"], validate=True)
        if not decoded:
            raise ValueError("Empty narration audio")
        descriptor = os.open(audio, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as file:
            file.write(decoded)
        private_json(timing, alignment)
        private_json(
            receipt,
            {
                "state": "completed",
                "characters": len(text),
                "requests": 1,
                "audio_sha256": hashlib.sha256(decoded).hexdigest(),
            },
        )
        return audio, alignment
    except (httpx.HTTPError, ValueError, KeyError, OSError, TypeError):
        private_json(receipt, {"state": "unknown", "characters": len(text), "requests": 1})
        # Never print an HTTP body, credential, request object or raw exception.
        raise ValueError("Voice request unresolved; receipt retained, no automatic retry") from None


def cue_ranges(scenes, text, alignment=None):
    if alignment is None:
        duration = max(100, len(text.split()) / 2.15)
        if duration >= 178:
            raise ValueError("Narration is too long for an under-three-minute demo")
        starts = [index * duration / len(text) for index in range(len(text))]
        ends = [(index + 1) * duration / len(text) for index in range(len(text))]
    else:
        starts, ends = timing_arrays(alignment, text)
    cues = []
    cursor = 0
    for scene_index, scene in enumerate(scenes):
        narration = scene["narration"].strip()
        begin = text.index(narration, cursor)
        words = list(re.finditer(r"\S+", narration))
        for index in range(0, len(words), 14):
            group = words[index : index + 14]
            first = begin + group[0].start()
            last = begin + group[-1].end() - 1
            cues.append(
                {
                    "scene": scene_index,
                    "text": text[first : last + 1],
                    "start": float(starts[first]),
                    "end": float(ends[last]),
                }
            )
        cursor = begin + len(narration)
    for index, cue in enumerate(cues):
        cue["start"] = 0.0 if index == 0 else cue["start"]
        cue["end"] = cues[index + 1]["start"] if index + 1 < len(cues) else ends[-1] + 1
    return cues


def render_frame(scene, caption, path, silent):
    # Pillow is a tooling dependency in system Python, not an app runtime dependency.
    from PIL import Image, ImageDraw, ImageFont, ImageOps

    font_path = ROOT / "src/frameseek/static/fonts/InstrumentSans.ttf"
    image = Image.new("RGB", (1920, 1080), "#0b1c28")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(str(font_path), 29)
    heading_font = ImageFont.truetype(str(font_path), 25)
    if scene.get("image"):
        screenshot = Image.open(scene["image"]).convert("RGB")
        screenshot = ImageOps.contain(screenshot, (1920, 900))
        image.paste(
            screenshot, ((1920 - screenshot.width) // 2, 60 + (900 - screenshot.height) // 2)
        )
    else:
        title_font = ImageFont.truetype(str(font_path), 68)
        text_font = ImageFont.truetype(str(font_path), 36)
        draw.text((100, 205), scene["title"], fill="#f1f4f7", font=title_font)
        draw.multiline_text((104, 345), scene["body"], fill="#b8ccdc", font=text_font, spacing=25)
    draw.rectangle((0, 0, 1920, 59), fill="#111f2b")
    draw.text((30, 14), scene["label"], fill="#ff985f", font=heading_font)
    if silent:
        draw.text((1610, 15), "SILENT REVIEW CUT", fill="#a7b8c8", font=heading_font)
    draw.rectangle((0, 960, 1920, 1080), fill="#0b1c28")
    wrapped = textwrap.wrap(caption, width=100)
    draw.multiline_text((65, 983), "\n".join(wrapped), fill="#f1f4f7", font=font, spacing=8)
    image.save(path)


def srt_time(seconds):
    milliseconds = round(seconds * 1000)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, milli = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{milli:03}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("storyboard", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--voice", action="store_true", help="Explicitly allow one capped TTS request"
    )
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.elevenlabs")
    args = parser.parse_args()
    os.umask(0o077)
    scenes = json.loads(args.storyboard.read_text())["scenes"]
    text = script_text(scenes)
    for scene in scenes:
        if scene.get("image") and not Path(scene["image"]).is_file():
            raise ValueError("A required captured screen is missing")
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "narration.txt").write_text(text + "\n")
    audio, alignment = (
        generate_voice(text, args.env_file, args.output) if args.voice else (None, None)
    )
    cues = cue_ranges(scenes, text, alignment)
    cards = args.output / ("voiced-cards" if audio else "silent-cards")
    cards.mkdir(exist_ok=True)
    sequence = []
    subtitles = []
    for index, cue in enumerate(cues):
        path = cards / f"{index:03}.png"
        render_frame(scenes[cue["scene"]], cue["text"], path, silent=audio is None)
        sequence.extend([f"file '{path.resolve()}'", f"duration {cue['end'] - cue['start']:.6f}"])
        subtitles.append(
            f"{index + 1}\n{srt_time(cue['start'])} --> {srt_time(cue['end'])}\n{cue['text']}\n"
        )
    sequence.append(f"file '{path.resolve()}'")
    concat = cards / "sequence.ffconcat"
    concat.write_text("\n".join(sequence) + "\n")
    (args.output / "captions.srt").write_text("\n".join(subtitles))
    output = args.output / ("frameseek-demo-english.mp4" if audio else "frameseek-demo-silent.mp4")
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-n",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat),
    ]
    if audio:
        command.extend(["-i", str(audio), "-c:a", "aac", "-b:a", "160k", "-af", "apad"])
    command.extend(
        [
            "-r",
            "30",
            "-vf",
            "fps=30,tpad=stop_mode=clone:stop_duration=3",
            "-t",
            str(cues[-1]["end"]),
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "22",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )
    subprocess.run(command, check=True, timeout=300)
    private_json(
        args.output / "video-manifest.json",
        {
            "output": str(output.resolve()),
            "seconds": cues[-1]["end"],
            "characters": len(text),
            "voice": "ElevenLabs" if audio else "pending",
            "review_only": True,
            "capture_style": (
                "captioned screen walkthrough with separately labelled archived evidence"
            ),
            "frameseek_model_requests": 0,
            "public_upload": False,
        },
    )
    print(f"Rendered {output} ({cues[-1]['end']:.1f}s, {len(text)} narration characters)")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error)) from None
