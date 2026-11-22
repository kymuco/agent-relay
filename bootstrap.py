"""Local AgentRelay bootstrap: venv, Python dependencies, portable Node for CWA.

Manual auth stays manual: never reads browser cookies or secrets from other apps.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import venv
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
RUNTIME = ROOT / ".runtime"
REQUIREMENTS = ROOT / "requirements.txt"
NODE_BASE = "https://nodejs.org/dist/latest-v24.x/"
MAX_ARCHIVE_BYTES = 120 * 1024 * 1024


def supported_python(version: tuple[int, int]) -> bool:
    return (3, 10) <= version <= (3, 14)


def node_major(version: str) -> int | None:
    match = re.fullmatch(r"v(\d+)\.\d+\.\d+(?:[-+].+)?", version.strip())
    return int(match.group(1)) if match else None


def validate_python() -> None:
    if not supported_python(sys.version_info[:2]):
        raise RuntimeError("Python 3.10–3.14 required; install a supported Python first")


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def setup_venv() -> Path:
    target = venv_python()
    if not target.exists():
        print("[AgentRelay] Creating isolated .venv ...", flush=True)
        venv.EnvBuilder(with_pip=True).create(VENV)
    if not target.exists():
        raise RuntimeError("Virtual environment creation did not produce Python")
    version = subprocess.check_output(
        [str(target), "-c", "import sys;print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
        text=True,
    ).strip()
    if not supported_python(tuple(map(int, version.split(".")))):
        raise RuntimeError("Existing .venv uses unsupported Python; remove .venv and restart")
    return target


def install_requirements(python: Path) -> None:
    wanted_hash = hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()
    stamp = VENV / ".agentrelay-requirements.sha256"
    if stamp.is_file() and stamp.read_text(encoding="ascii").strip() == wanted_hash:
        check = subprocess.run(
            [str(python), "-c", "import chatgpt_web_adapter"],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False,
        )
        if check.returncode == 0:
            print("[AgentRelay] Python dependencies already installed.", flush=True)
            return
    print("[AgentRelay] Installing Python dependencies ...", flush=True)
    subprocess.run(
        [str(python), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(REQUIREMENTS)],
        cwd=ROOT, check=True,
    )
    stamp.write_text(wanted_hash + "\n", encoding="ascii")


def installed_node() -> str | None:
    binary = shutil.which("node")
    if not binary:
        return None
    try:
        result = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    version = node_major(result.stdout) if result.returncode == 0 else None
    return binary if version is not None and version >= 24 else None


def node_archive_entry(shasums: str, architecture: str) -> tuple[str, str]:
    if architecture not in ("x64", "arm64"):
        raise RuntimeError("Portable Node installer supports Windows x64/ARM64 only")
    pattern = re.compile(
        rf"^([0-9a-f]{{64}})\s+(node-v24\.\d+\.\d+-win-{architecture}\.zip)$",
        re.MULTILINE,
    )
    matches = pattern.findall(shasums)
    if len(matches) != 1:
        raise RuntimeError("Node checksum manifest has no unique Windows archive")
    digest, filename = matches[0]
    return filename, digest


def get_bytes(url: str, *, max_bytes: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "AgentRelay-Bootstrap/0.1"})
    with urllib.request.urlopen(request, timeout=60) as response:
        chunks: list[bytes] = []
        count = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            count += len(chunk)
            if count > max_bytes:
                raise RuntimeError("Download exceeded the allowed size")
            chunks.append(chunk)
    return b"".join(chunks)


def portable_node() -> Path:
    if sys.platform != "win32":
        raise RuntimeError("Install Node.js 24+ through your system package manager")
    machine = platform.machine().lower()
    architecture = "arm64" if machine in ("arm64", "aarch64") else "x64" if machine in ("amd64", "x86_64") else ""
    if not architecture:
        raise RuntimeError("Portable Node setup supports Windows x64 and ARM64 only")
    RUNTIME.mkdir(parents=True, exist_ok=True)
    portable = RUNTIME / "node.exe"
    if portable.exists():
        result = subprocess.run([str(portable), "--version"], capture_output=True, text=True, check=False)
        if result.returncode == 0 and (node_major(result.stdout) or 0) >= 24:
            return portable
    print("[AgentRelay] Fetching official Node.js 24 checksums ...", flush=True)
    manifest = get_bytes(NODE_BASE + "SHASUMS256.txt", max_bytes=128 * 1024).decode("ascii")
    filename, expected = node_archive_entry(manifest, architecture)
    print("[AgentRelay] Downloading portable Node.js 24 into .runtime/ ...", flush=True)
    archive = get_bytes(NODE_BASE + filename, max_bytes=MAX_ARCHIVE_BYTES)
    if hashlib.sha256(archive).hexdigest() != expected:
        raise RuntimeError("Node.js archive SHA-256 mismatch; refusing to execute")
    with tempfile.TemporaryDirectory(dir=RUNTIME) as workspace:
        staged = Path(workspace) / "node.exe"
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            expected_path = filename[:-4] + "/node.exe"
            if expected_path not in bundle.namelist():
                raise RuntimeError("Node.js archive lacks expected node.exe")
            with bundle.open(expected_path) as stream, staged.open("wb") as destination:
                shutil.copyfileobj(stream, destination)
        staged.replace(portable)
    return portable


def read_env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", key.strip()):
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def validate_local_config() -> None:
    env_file = ROOT / ".env"
    if not env_file.is_file():
        shutil.copyfile(ROOT / ".env.example", env_file)
        raise RuntimeError("Created .env from template; fill bot/group IDs and rerun start.cmd")
    config = read_env_values(env_file)
    required = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ALLOWED_USER_IDS", "CWA_CONVERSATION_ID")
    missing = [key for key in required if not config.get(key) or "REPLACE" in config[key].upper()]
    if missing:
        raise RuntimeError("Configure these .env fields: " + ", ".join(missing))
    name = os.environ.get("CWA_AUTH_FILE") or config.get("CWA_AUTH_FILE") or "auth_data.json"
    auth = Path(name)
    if not auth.is_absolute():
        auth = ROOT / auth
    if not auth.is_file():
        raise RuntimeError("Missing auth_data.json; save the ChatGPT /api/auth/session JSON locally")
    # Never read, print, or copy credentials here; the CWA client handles them.


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install AgentRelay dependencies and run the bot")
    parser.add_argument("--setup-only", action="store_true", help="Install dependencies without launching Telegram bot")
    parser.add_argument("--no-node-download", action="store_true", help="Require already installed Node.js 24+")
    args = parser.parse_args(argv)
    try:
        validate_python()
        python = setup_venv()
        install_requirements(python)
        node = installed_node()
        if node is None:
            if args.no_node_download:
                raise RuntimeError("Node.js 24+ missing; remove --no-node-download to install portable Node")
            node = str(portable_node())
        env = os.environ.copy()
        env["PATH"] = str(Path(node).parent) + os.pathsep + env.get("PATH", "")
        if sys.platform == "win32" and not shutil.which("curl.exe", path=env["PATH"]):
            raise RuntimeError("curl.exe missing; Windows 10/11 normally includes it")
        print("[AgentRelay] Python and Node.js dependencies ready.", flush=True)
        if args.setup_only:
            return 0
        validate_local_config()
        print("[AgentRelay] Starting bot (Ctrl+C to stop)...", flush=True)
        return subprocess.call([str(python), str(ROOT / "agent_relay.py")], cwd=ROOT, env=env)
    except KeyboardInterrupt:
        # Ctrl+C reaches both wrapper and child on Windows. No wrapper traceback.
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, zipfile.BadZipFile) as exc:
        print(f"[AgentRelay] Setup stopped: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
