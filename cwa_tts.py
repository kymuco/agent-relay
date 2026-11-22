"""Local CLI for ChatGPT Read Aloud; requires chatgpt-web-adapter and curl."""
import argparse
import json
import os
import queue
import shutil
import threading
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from chatgpt_web_adapter import BrowserlessHTTPClient

URL = "https://chatgpt.com/backend-api/read-aloud/synthesize"
DEFAULT_AUTH = Path(r"W:\dev\chatgpt-web-adapter\auth_data.json")


def chunks(text, limit=4096):
    # JavaScript counts UTF-16 code units, including two units for emoji.
    text = " ".join(text.split())
    while text:
        units = 0
        end = 0
        for ch in text:
            size = 2 if ord(ch) > 0xFFFF else 1
            if units + size > limit:
                break
            units += size
            end += 1
        if end < len(text):
            prefix = text[:end]
            sentence = max((i + 1 for i, ch in enumerate(prefix[:-1])
                            if ch in ".!?。！？" and prefix[i + 1] == " "),
                           default=0)
            end = sentence or max(prefix.rfind(" "), 0) or end
        piece = text[:end].strip()
        if not piece:
            raise ValueError("Could not split text")
        yield piece
        text = text[end:].strip()


class StreamPlayer:
    def __init__(self):
        executable = shutil.which("ffplay")
        if not executable:
            raise RuntimeError("Streaming requires ffplay on PATH")
        self.process = subprocess.Popen(
            [executable, "-nodisp", "-autoexit", "-loglevel", "error",
             "-probesize", "2048", "-analyzeduration", "0",
             "-f", "aac", "-i", "pipe:0"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        self.queue = queue.Queue()
        self.failed = False
        self.thread = threading.Thread(target=self._feed, daemon=True)
        self.thread.start()

    def _feed(self):
        try:
            while True:
                data = self.queue.get()
                if data is None:
                    break
                self.process.stdin.write(data)
                self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            self.failed = True
        finally:
            try:
                self.process.stdin.close()
            except OSError:
                pass

    def write(self, data):
        if self.failed or self.process.poll() is not None:
            raise RuntimeError("Streaming player stopped unexpectedly")
        self.queue.put(data)

    def finish(self):
        self.queue.put(None)
        self.thread.join()
        code = self.process.wait()
        if self.failed or code:
            raise RuntimeError(f"Streaming player failed (exit {code})")

    def abort(self):
        if self.process.poll() is None:
            self.process.kill()
        self.queue.put(None)
        self.process.wait()
        self.thread.join(timeout=2)


def request_audio(client, text, timeout, player=None):
    headers = client._build_headers({
        "accept": "*/*", "content-type": "application/json",
        "origin": "https://chatgpt.com", "referer": "https://chatgpt.com/",
    })
    names = []
    proc = None
    try:
        for data in (b"", json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")):
            with tempfile.NamedTemporaryFile(delete=False) as f:
                names.append(f.name)
                f.write(data)
        cmd = client._build_curl_command("POST", URL, headers, names[0], names[1], no_buffer=True)
        start = time.perf_counter()
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        audio = bytearray()
        first = None
        stream_started = False
        while True:
            part = proc.stdout.read(16384)
            if not part:
                break
            if first is None:
                first = time.perf_counter() - start
            audio.extend(part)
            if player is not None:
                if not stream_started and len(audio) >= 2:
                    header = Path(names[0]).read_text(encoding="utf-8", errors="replace")
                    status = client._extract_status_code(header)
                    types = [line.split(":", 1)[1].strip().lower().split(";", 1)[0]
                             for line in header.splitlines()
                             if line.lower().startswith("content-type:")]
                    if status == 200 and types and types[-1] == "audio/aac" and audio[0] == 255 and audio[1] & 246 == 240:
                        player.write(bytes(audio))
                        stream_started = True
                        print(f"  streaming to player: {time.perf_counter() - start:.3f}s", flush=True)
                elif stream_started:
                    player.write(part)
        code = proc.wait()
        elapsed = time.perf_counter() - start
        header = Path(names[0]).read_text(encoding="utf-8", errors="replace")
        status = client._extract_status_code(header)
        types = [line.split(":", 1)[1].strip().lower().split(";", 1)[0]
                 for line in header.splitlines() if line.lower().startswith("content-type:")]
        mime = types[-1] if types else None
        if code != 0 or status != 200:
            raise RuntimeError(f"Request failed (HTTP {status}, curl exit {code}); no automatic retry")
        if mime != "audio/aac" or len(audio) < 101 or audio[0] != 255 or audio[1] & 246 != 240:
            raise RuntimeError(f"Unexpected response (HTTP {status}, content type {mime})")
        return bytes(audio), first, elapsed
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        for name in names:
            try:
                os.unlink(name)
            except OSError:
                pass


def main():
    parser = argparse.ArgumentParser(description="Synthesize text using the local Codex Read Aloud route.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--text", help="Text to read")
    group.add_argument("--file", type=Path, help="UTF-8 text file")
    parser.add_argument("--auth", type=Path, default=DEFAULT_AUTH, help="Path to CWA auth_data.json")
    parser.add_argument("--output", type=Path, help="Output AAC path; must not already exist")
    parser.add_argument("--timeout", type=int, default=120, help="Maximum seconds per request (default: 120)")
    playback = parser.add_mutually_exclusive_group()
    playback.add_argument("--play", action="store_true", help="Open the completed audio with Windows default player")
    playback.add_argument("--stream", action="store_true", help="Play audio while downloading (requires ffplay)")
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    player = None
    try:
        text = args.file.read_text(encoding="utf-8-sig") if args.file else args.text
        pieces = list(chunks(text))
        if not pieces:
            raise ValueError("Text is empty")
        output = args.output or Path(tempfile.gettempdir()) / f"cwa-read-aloud-{time.time_ns()}.aac"
        if output.exists():
            raise FileExistsError("Output path already exists")
        client = BrowserlessHTTPClient(auth_file=str(args.auth), timeout=args.timeout)
        audios = []
        if args.stream:
            player = StreamPlayer()
        for i, piece in enumerate(pieces, 1):
            print(f"Part {i}/{len(pieces)}: sending...", flush=True)
            audio, first, elapsed = request_audio(client, piece, args.timeout, player)
            audios.append(audio)
            print(f"  first bytes: {first:.3f}s; complete: {elapsed:.3f}s; bytes: {len(audio)}", flush=True)
        with output.open("xb") as f:
            for audio in audios:
                f.write(audio)
        print(f"Saved: {output.resolve()}", flush=True)
        if player:
            print("Waiting for streaming playback to finish...", flush=True)
            player.finish()
            player = None
        if args.play:
            os.startfile(output.resolve())
    except (Exception, KeyboardInterrupt) as error:
        if player is not None:
            player.abort()
        print(f"Error: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
