"""Build synthetic fixture video from generated stills, and extract its evidence."""

import subprocess
from pathlib import Path


def main() -> None:
    directory = Path(__file__).resolve().parents[1] / "src" / "frameseek" / "static" / "demo"
    frames = directory / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for position, seconds in (("left", 5), ("center", 6), ("right", 7)):
        source = directory / f"source-{position}.png"
        if not source.is_file():
            raise FileNotFoundError(f"Missing generated source: {source}")
        command.extend(["-loop", "1", "-t", str(seconds), "-i", str(source)])
    streams = []
    for index in range(3):
        streams.append(
            f"[{index}:v]scale=1600:900:force_original_aspect_ratio=decrease,"
            f"pad=1600:900:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=12[v{index}]"
        )
    streams.append("[v0][v1][v2]concat=n=3:v=1:a=0[out]")
    command.extend(
        [
            "-filter_complex",
            ";".join(streams),
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
            str(directory / "parcel.mp4"),
        ]
    )
    subprocess.run(command, check=True)
    for second in (0, 6, 10, 13):
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
                str(directory / "parcel.mp4"),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(frames / f"{second}.jpg"),
            ],
            check=True,
        )
    print("Built 18-second synthetic still sequence and 4 extracted evidence frames")


if __name__ == "__main__":
    main()
