"""Transcribe a local audio file using the desktop dictation upload route."""
import argparse
import json
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from chatgpt_web_adapter import BrowserlessHTTPClient
from chatgpt_web_adapter.web_session import suppress_web_session_debug_trace


def transcribe(audio_path, auth_path, timeout=60):
    """Return recognized text; sends exactly one transcription request."""
    with tempfile.TemporaryDirectory(prefix="cwa-dictation-") as directory:
        converted = Path(directory) / "audio.webm"
        try:
            result = subprocess.run(
                ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(audio_path),
                 "-c:a", "libopus", "-ac", "1", "-ar", "48000", str(converted)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout,
            )
        except FileNotFoundError as error:
            raise RuntimeError("ffmpeg is required and must be on PATH") from error
        if result.returncode != 0:
            raise RuntimeError("ffmpeg could not decode the audio file")
        audio = converted.read_bytes()
        if not audio:
            raise RuntimeError("Converted audio is empty")
        boundary = "----cwa" + uuid.uuid4().hex
        body = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            'filename="audio.webm"\r\nContent-Type: audio/webm\r\n\r\n'
        ).encode("ascii") + audio + f"\r\n--{boundary}--\r\n".encode("ascii")
        client = BrowserlessHTTPClient(auth_file=str(auth_path), timeout=timeout)
        headers = client._build_headers({
            "accept": "application/json",
            "content-type": "multipart/form-data; boundary=" + boundary,
            "origin": "https://chatgpt.com",
            "referer": "https://chatgpt.com/",
        })
        with suppress_web_session_debug_trace():
            status, response, _ = client._run_curl(
                "POST", "https://chatgpt.com/backend-api/transcribe", headers, body
            )
        if status != 200:
            raise RuntimeError(f"Transcription failed (HTTP {status}); no automatic retry")
        try:
            data = json.loads(response)
        except (ValueError, UnicodeDecodeError) as error:
            raise RuntimeError("Unexpected transcription response") from error
        if not isinstance(data, dict) or not isinstance(data.get("text"), str):
            raise RuntimeError("Transcription response does not contain text")
        return data["text"]


def main():
    parser = argparse.ArgumentParser(description="Transcribe a local audio file.")
    parser.add_argument("audio", type=Path, help="Audio file supported by ffmpeg")
    parser.add_argument("--auth", type=Path, default=Path(r"W:\dev\chatgpt-web-adapter\auth_data.json"))
    parser.add_argument("--output", type=Path, help="Save transcript as UTF-8; must not already exist")
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    if not args.audio.is_file():
        parser.error("audio file does not exist")
    if args.output and args.output.exists():
        parser.error("output file already exists")
    try:
        start = time.perf_counter()
        text = transcribe(args.audio, args.auth, args.timeout)
        if args.output:
            with args.output.open("x", encoding="utf-8", newline="\n") as f:
                f.write(text + "\n")
            print(f"Saved: {args.output.resolve()}")
        else:
            print(text)
        print(f"Finished in {time.perf_counter()-start:.2f}s", file=sys.stderr)
    except Exception as error:
        print(f"Error: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())