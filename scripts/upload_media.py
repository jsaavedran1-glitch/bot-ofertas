"""Upload private Reels audio to the database (run once from your computer).

Usage: python scripts/upload_media.py <folder>
The folder holds musica/ and efectos/ subfolders (e.g. tracks from Meta Sound Collection).
Their license only allows use inside Meta products, and this repository is public, so the files
live in the private database instead of git. Music is cut to its most energetic 12 seconds (a reel
lasts 9); effects are stored as they are. Prints names and sizes only.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile

from dotenv import dotenv_values

sys.path.insert(0, ".")
from app.database import Database  # noqa: E402

AUDIO = {".mp3", ".m4a", ".wav", ".ogg"}


def loudest_start(path: Path, window: int = 9, skip: int = 1) -> int:
    """Second where the louder `window`-second stretch of the track begins."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-af",
         "aresample=44100,asetnsamples=n=44100:p=0,astats=metadata=1:reset=1,"
         "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-", "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    ).stdout
    levels = []
    for line in out.splitlines():
        if "RMS_level=" in line:
            value = line.split("=", 1)[1]
            levels.append(0.0 if value in ("-inf", "inf", "nan") else 10 ** (float(value) / 20))
    if len(levels) <= window + skip:
        return 0
    return max(range(skip, len(levels) - window), key=lambda s: sum(levels[s:s + window]))


def cut(path: Path, start: int, seconds: int = 12) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "cut.m4a"
        subprocess.run(["ffmpeg", "-v", "error", "-ss", str(start), "-t", str(seconds), "-i", str(path),
                        "-c:a", "aac", "-b:a", "192k", str(target)], check=True)
        return target.read_bytes()


def main(folder: str) -> int:
    root = Path(folder).expanduser()
    env = dotenv_values(".env")
    db = Database(env["DATABASE_URL"])
    db.initialize()
    for kind in ("musica", "efectos"):
        for path in sorted((root / kind).glob("*")):
            if path.suffix.lower() not in AUDIO:
                continue
            if kind == "musica":
                start = loudest_start(path)
                data = cut(path, start)
                note = f"desde {start}s"
            else:
                data, note = path.read_bytes(), "completo"
            db.put_media(f"{path.stem}.m4a" if kind == "musica" else path.name, kind, data)
            print(f"{kind}: {path.name} ({note}, {len(data) // 1024} KB)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
