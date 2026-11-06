"""Turn Discord attachments into something the model can read: transcripts and image frames."""

import asyncio
import base64
import os
import tempfile
from pathlib import Path

from .agent import client

TRANSCRIBE_MODEL = os.environ.get("TRANSCRIBE_MODEL", "voxtral-mini-transcribe-3")
OCR_MODEL = os.environ.get("OCR_MODEL", "mistral-ocr-latest")
VIDEO_FRAMES = int(os.environ.get("VIDEO_FRAMES", "6"))
IMAGE_SIDE = 1568  # longest side sent to the model; larger images only cost time

# Read with Mistral OCR, which returns each page as markdown, tables included.
OCR_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
# Read as they are.
TEXT_SUFFIXES = {
    ".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".html", ".htm", ".yaml", ".yml", ".toml",
    ".ini", ".log", ".srt", ".vtt", ".tex", ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".kt",
    ".c", ".h", ".cpp", ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".sh", ".sql", ".css",
}


async def transcribe(data: bytes, file_name: str) -> str:
    response = await client.audio.transcriptions.complete_async(
        model=TRANSCRIBE_MODEL,
        file={"file_name": file_name, "content": data},
    )
    return response.text.strip()


def is_document(file_name: str, content_type: str | None) -> bool:
    suffix = Path(file_name).suffix.lower()
    return suffix in OCR_TYPES or suffix in TEXT_SUFFIXES or (content_type or "").startswith("text/")


async def document_text(data: bytes, file_name: str) -> str:
    """The text of a PDF, Word or PowerPoint file (by OCR), or of a text file."""
    mime = OCR_TYPES.get(Path(file_name).suffix.lower())
    if mime is None:
        return data.decode("utf-8", errors="replace").strip()
    response = await client.ocr.process_async(
        model=OCR_MODEL,
        document={
            "type": "document_url",
            "document_url": f"data:{mime};base64,{base64.b64encode(data).decode()}",
            "document_name": file_name,
        },
        image_limit=0,  # only the text
    )
    return "\n\n".join(page.markdown.strip() for page in response.pages).strip()


async def image(data: bytes, file_name: str) -> bytes:
    """The image as JPEG bytes, scaled down to IMAGE_SIDE (the first frame of a GIF)."""
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / f"input{Path(file_name).suffix or '.png'}"
        source.write_bytes(data)
        jpeg = Path(tmp) / "image.jpg"
        await _ffmpeg(
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-vf",
            f"scale='min({IMAGE_SIDE},iw)':'min({IMAGE_SIDE},ih)':force_original_aspect_ratio=decrease",
            "-q:v",
            "3",
            str(jpeg),
        )
        return jpeg.read_bytes()


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
