"""Turn Discord attachments into something the model can read: transcripts and image frames."""

import asyncio
import base64
import os
import tempfile
from pathlib import Path

from .agent import client

TRANSCRIBE_MODEL = os.environ.get("TRANSCRIBE_MODEL", "voxtral-mini-transcribe-3")
VIDEO_FRAMES = int(os.environ.get("VIDEO_FRAMES", "6"))


async def transcribe(data: bytes, file_name: str) -> str:
    response = await client.audio.transcriptions.complete_async(
        model=TRANSCRIBE_MODEL,
        file={"file_name": file_name, "content": data},
    )
    return response.text.strip()


async def video_frames_and_audio(
    data: bytes, file_name: str
) -> tuple[list[str], bytes | None]:
    """Sample frames evenly across the video as JPEG data URIs, and pull out its audio track as MP3."""
    with tempfile.TemporaryDirectory() as tmp:
        video = Path(tmp) / f"input{Path(file_name).suffix or '.mp4'}"
        video.write_bytes(data)

        duration = await _duration(video)
        fps = VIDEO_FRAMES / duration if duration > 0 else 1
        await _ffmpeg(
            "-i",
            str(video),
            "-vf",
            f"fps={fps:.4f},scale='min(1024,iw)':-2",
            "-frames:v",
            str(VIDEO_FRAMES),
            "-q:v",
            "4",
            str(Path(tmp) / "frame_%02d.jpg"),
        )
        frames = [
            "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode()
            for path in sorted(Path(tmp).glob("frame_*.jpg"))
        ]

        audio_path = Path(tmp) / "audio.mp3"
        try:
            await _ffmpeg(
                "-i", str(video), "-vn", "-ac", "1", "-b:a", "64k", str(audio_path)
            )
            audio = audio_path.read_bytes() if audio_path.exists() else None
        except RuntimeError:
            audio = None  # the video has no audio track
        return frames, audio


async def _duration(path: Path) -> float:
    process = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "csv=p=0",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await process.communicate()
    try:
        return float(stdout.decode().strip())
    except ValueError:
        return 0.0


async def _ffmpeg(*args: str) -> None:
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-v",
        "error",
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(stderr.decode().strip())
