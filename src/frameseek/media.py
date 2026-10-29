"""Bounded decoding in an OS file/network sandbox. No arbitrary URL ingestion."""

import json
import math
import os
import platform
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from frameseek.windows import plan_windows

MAX_UPLOAD = 500 * 1024 * 1024


class MediaError(ValueError):
    pass


def sandbox_command(directory: Path, args: list[str]):
    directory = directory.resolve()
    if platform.system() == "Darwin" and shutil.which("sandbox-exec"):
        # No network and no user files beyond this upload directory. Homebrew
        # libraries are read-only runtime dependencies, never writable outputs.
        profile = "(version 1)(deny default)(allow process*)(allow sysctl-read)"
        profile += "(allow file-read-metadata)(allow file-map-executable)"
        profile += '(allow file-read* (literal "/") (subpath "/System") (subpath "/usr/lib")'
        profile += ' (subpath "/opt/homebrew") (subpath "/private/var/db/dyld")'
        profile += ' (literal "/dev/null") (literal "/dev/urandom")'
        profile += ' (literal "/dev/random") (subpath ' + json.dumps(str(directory)) + "))"
        profile += (
            '(allow file-write* (literal "/dev/null") (subpath ' + json.dumps(str(directory)) + "))"
        )
        return ["/usr/bin/sandbox-exec", "-p", profile, *args]
    if platform.system() == "Linux" and shutil.which("bwrap"):
        command = [shutil.which("bwrap"), "--unshare-all", "--die-with-parent", "--new-session"]
        for path in ("/usr", "/lib", "/lib64", "/bin"):
            if Path(path).exists():
                command += ["--ro-bind", path, path]
        # Debian/Ubuntu multimedia libraries use the loader cache and alternatives
        # symlinks. Expose only these runtime paths, never the whole /etc tree.
        for path in ("/etc/ld.so.cache", "/etc/alternatives"):
            if Path(path).exists():
                command += ["--ro-bind", path, path]
        return command + [
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--bind",
            str(directory),
            str(directory),
            "--chdir",
            str(directory),
            *args,
        ]
    raise MediaError("media_isolation_unavailable")


def run(directory, args, timeout=45, output_limit=2 * 1024 * 1024):
    # The standalone wrapper sets resource limits before exec, avoiding unsafe
    # preexec_fn calls in the threaded API process.
    command = sandbox_command(directory, args)
    wrapper = Path(__file__).with_name("media_limits.py")
    p = subprocess.Popen(
        [sys.executable, str(wrapper), *command],
        cwd=directory,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env={"PATH": "/usr/bin:/bin:/opt/homebrew/bin"},
    )
    poller = selectors.DefaultSelector()
    output = [bytearray(), bytearray()]
    for i, pipe in enumerate((p.stdout, p.stderr)):
        poller.register(pipe, selectors.EVENT_READ, i)
    deadline = time.monotonic() + timeout
    rss_check = 0
    try:
        while poller.get_map():
            if time.monotonic() > deadline:
                raise MediaError("media_timeout")
            if platform.system() == "Darwin" and time.monotonic() >= rss_check:
                rss_check = time.monotonic() + 0.5
                sample = subprocess.run(
                    ["/bin/ps", "-o", "rss=", "-p", str(p.pid)], capture_output=True, timeout=1
                ).stdout.strip()
                if sample and int(sample) > 2 * 1024**2:
                    raise MediaError("media_memory_limit")
            for key, _ in poller.select(0.1):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    poller.unregister(key.fileobj)
                else:
                    output[key.data].extend(chunk)
                    if sum(map(len, output)) > output_limit:
                        raise MediaError("media_output_limit")
            if sum(f.stat().st_size for f in directory.rglob("*") if f.is_file()) > 1024**3:
                raise MediaError("media_disk_limit")
        if p.wait(timeout=max(0.1, deadline - time.monotonic())) != 0:
            raise MediaError("invalid_media")
        return tuple(bytes(x).decode("utf-8", errors="replace") for x in output)
    finally:
        if p.poll() is None:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait()
        poller.close()
        p.stdout.close()
        p.stderr.close()


def probe(directory):
    directory = directory.resolve()
    binary = shutil.which("ffprobe")
    if not binary:
        raise MediaError("ffprobe_unavailable")
    output, _ = run(
        directory,
        [
            binary,
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-format_whitelist",
            "mov,matroska,webm,avi",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(directory / "original"),
        ],
    )
    try:
        info = json.loads(output)
        video = next(s for s in info["streams"] if s["codec_type"] == "video")
        duration = float(info["format"]["duration"])
        width, height = int(video["width"]), int(video["height"])
        start = float(video.get("start_time", info["format"].get("start_time", 0)))
        if not all(math.isfinite(x) for x in (duration, start)) or not 0 < duration <= 3600:
            raise MediaError("duration_limit")
        if not 0 < width <= 7680 or not 0 < height <= 4320 or width * height > 33177600:
            raise MediaError("pixel_limit")
        return {
            "duration": duration,
            "width": width,
            "height": height,
            "start_pts": start,
            "audio": any(s["codec_type"] == "audio" for s in info["streams"]),
            "windows": [{"start": w.start, "end": w.end} for w in plan_windows(duration)],
        }
    except (KeyError, StopIteration, ValueError, TypeError) as error:
        raise MediaError("invalid_media") from error


def frames(directory, video_id, metadata, window, count=12, prefix="c"):
    directory = directory.resolve()
    binary = shutil.which("ffmpeg")
    if not binary:
        raise MediaError("ffmpeg_unavailable")
    destination = directory / "frames"
    destination.mkdir(exist_ok=True)
    start, end = window["start"], window["end"]
    step = max(1, (end - start) / count)
    pattern = destination / f"{prefix}_%03d.jpg"
    # copyts + subtraction of source start PTS yields absolute video time;
    # select retains decoded frame PTS rather than fabricating fps timestamps.
    expression = (
        f"setpts=PTS-({metadata['start_pts']})/TB,"
        f"select='gte(t,{start})*lt(t,{end})*"
        f"(isnan(prev_selected_t)+gte(t-prev_selected_t,{step}))',"
        "scale='min(1280,iw)':-2,showinfo"
    )
    _, stderr = run(
        directory,
        [
            binary,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "info",
            "-protocol_whitelist",
            "file,pipe",
            "-format_whitelist",
            "mov,matroska,webm,avi",
            "-copyts",
            "-ss",
            str(start),
            # Bound input decoding relative to the seek. An output -t would be
            # compared with copyts absolute PTS and truncate later windows.
            "-t",
            str(end - start + 1),
            "-i",
            str(directory / "original"),
            "-map",
            "0:v:0",
            "-an",
            "-threads",
            "1",
            "-filter_threads",
            "1",
            "-vf",
            expression,
            "-fps_mode",
            "vfr",
            "-frames:v",
            str(count),
            "-q:v",
            "3",
            "-y",
            str(pattern),
        ],
        timeout=90,
    )
    pts = [float(t) for t in re.findall(r"\bn:\s*\d+.*?pts_time:([\d.e+-]+)", stderr)]
    files = sorted(destination.glob(f"{prefix}_*.jpg"))
    if not files or len(files) != len(pts):
        raise MediaError("frame_pts_missing")
    result = []
    for file, timestamp in zip(files, pts, strict=True):
        if not start <= timestamp < end or not math.isfinite(timestamp):
            raise MediaError("frame_pts_invalid")
        result.append(
            {
                "t": timestamp,
                "file": file.name,
                "frame_id": video_id + "_" + file.stem,
                "uri": f"/media/{video_id}/frames/{file.name}",
            }
        )
    return result


def scene_windows(directory, metadata, threshold):
    directory = directory.resolve()
    binary = shutil.which("ffmpeg")
    _, stderr = run(
        directory,
        [
            binary,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "info",
            "-protocol_whitelist",
            "file,pipe",
            "-format_whitelist",
            "mov,matroska,webm,avi",
            "-copyts",
            "-i",
            str(directory / "original"),
            "-map",
            "0:v:0",
            "-an",
            "-threads",
            "1",
            "-filter_threads",
            "1",
            "-vf",
            f"setpts=PTS-({metadata['start_pts']})/TB,"
            f"scale=320:-2,select='gt(scene,{threshold})',showinfo",
            "-f",
            "null",
            "/dev/null",
        ],
        timeout=90,
    )
    cuts = sorted(set(float(t) for t in re.findall(r"\bn:\s*\d+.*?pts_time:([\d.e+-]+)", stderr)))
    cuts = tuple(t for t in cuts if 0 < t < metadata["duration"])
    metadata["scene_boundaries"] = list(cuts)
    metadata["windows"] = [
        {"start": w.start, "end": w.end} for w in plan_windows(metadata["duration"], cuts)
    ]
    return metadata


def frame_timeline(directory, metadata):
    """All decoded source frame PTS for an offline full-scan plan, never fps math.

    Refuse missing, repeated, non-monotonic, trimmed or excessive timelines rather
    than labelling a sampled/partially decoded catalog as full-source coverage.
    """
    binary = shutil.which("ffprobe")
    if not binary:
        raise MediaError("ffprobe_unavailable")
    output, _ = run(
        directory.resolve(),
        [
            binary,
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-format_whitelist",
            "mov,matroska,webm,avi",
            "-select_streams",
            "v:0",
            "-show_frames",
            "-show_entries",
            "frame=best_effort_timestamp_time,duration_time,pkt_duration_time",
            "-of",
            "json",
            str(directory.resolve() / "original"),
        ],
        timeout=90,
        output_limit=32 * 1024 * 1024,
    )
    try:
        raw = json.loads(output)["frames"]
        if not isinstance(raw, list) or not 1 <= len(raw) <= 216000:
            raise MediaError("observation_frame_limit")
        times = [
            float(frame["best_effort_timestamp_time"]) - metadata["start_pts"] for frame in raw
        ]
        duration = metadata["duration"]
        last_duration = float(raw[-1].get("duration_time", raw[-1].get("pkt_duration_time", "nan")))
        if (
            not math.isfinite(last_duration)
            or last_duration <= 0
            or not math.isclose(times[-1] + last_duration, duration, abs_tol=0.00001, rel_tol=0)
            or times[0] != 0
            or not all(math.isfinite(t) and 0 <= t < duration for t in times)
            or any(b <= a for a, b in zip(times, times[1:], strict=False))
        ):
            raise MediaError("incomplete_source_timeline")
        return times
    except (KeyError, TypeError, ValueError) as error:
        raise MediaError("incomplete_source_timeline") from error
