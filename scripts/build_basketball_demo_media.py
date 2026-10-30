"""Encode the public synthetic basketball stills and extract their cited frames."""

import subprocess
from pathlib import Path


def main() -> None:
    static = Path(__file__).resolve().parents[1] / "src" / "frameseek" / "static"
    target = static / "basketball_demo"
    frames = target / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for name, duration in (("court", 5), ("pass", 6), ("shot", 7)):
        command.extend(
            ["-loop", "1", "-t", str(duration), "-i", str(static / "landing" / f"{name}.jpg")]
        )
    filters = [
        f"[{index}:v]scale=1600:900:force_original_aspect_ratio=decrease,"
        f"pad=1600:900:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=12[v{index}]"
        for index in range(3)
    ]
    filters.append("[v0][v1][v2]concat=n=3:v=1:a=0[out]")
    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[out]",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            "20",
            "-movflags",
            "+faststart",
            str(target / "basketball.mp4"),
        ]
    )
    subprocess.run(command, check=True)
    for second in (0, 6, 13):
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                str(second),
                "-i",
                str(target / "basketball.mp4"),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(frames / f"{second}.jpg"),
            ],
            check=True,
        )


if __name__ == "__main__":
    main()
