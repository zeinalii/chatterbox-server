from __future__ import annotations

import argparse
import base64
import configparser
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, fields
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


DEFAULT_CONFIG_NAME = "config.conf"
SUPPORTED_OUTPUTS = {".wav", ".mp3"}
BOOL_TRUE = {"1", "yes", "true", "on"}
BOOL_FALSE = {"0", "no", "false", "off"}
SERVER_DEFAULT_HOST = "127.0.0.1"
SERVER_DEFAULT_PORT = 7860


@dataclass(frozen=True)
class Settings:
    text_file: Path
    out: Path
    config: Path | None = None
    device: str = "cuda"
    chunk_chars: int = 260
    pause_seconds: float = 0.35
    seed: int | None = None
    audio_prompt_path: Path | None = None
    repetition_penalty: float = 1.2
    min_p: float = 0.05
    top_p: float = 1.0
    exaggeration: float = 0.5
    cfg_weight: float = 0.5
    temperature: float = 0.8
    overwrite: bool = True
    quiet: bool = False


DEFAULTS: dict[str, Any] = {
    "out": "audio.wav",
    "device": "cuda",
    "chunk_chars": 260,
    "pause_seconds": 0.35,
    "seed": None,
    "audio_prompt_path": None,
    "repetition_penalty": 1.2,
    "min_p": 0.05,
    "top_p": 1.0,
    "exaggeration": 0.5,
    "cfg_weight": 0.5,
    "temperature": 0.8,
    "overwrite": True,
    "quiet": False,
}


CONFIG_TEMPLATE = """[chatterbox]
# Output path. Supported suffixes: .wav, .mp3
out = audio.wav

# Device: cuda, auto, cpu, or mps
device = cuda

# Long text is split at sentence boundaries before synthesis.
chunk_chars = 260
pause_seconds = 0.35

# Optional deterministic seed. Leave blank for random generation.
seed =

# Optional 5+ second reference voice clip for voice cloning.
audio_prompt_path =

# Chatterbox generation parameters.
repetition_penalty = 1.2
min_p = 0.05
top_p = 1.0
exaggeration = 0.5
cfg_weight = 0.5
temperature = 0.8

overwrite = true
quiet = false
"""


def parse_bool(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    normalized = value.strip().lower()
    if normalized in BOOL_TRUE:
        return True
    if normalized in BOOL_FALSE:
        return False
    raise ValueError(f"expected boolean, got {value!r}")


def normalized_key(key: str) -> str:
    return key.strip().replace("-", "_")


def load_config(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    if not path.exists():
        raise FileNotFoundError(f"config file does not exist: {path}")

    parser = configparser.ConfigParser()
    parser.read(path)
    if parser.has_section("chatterbox"):
        values = dict(parser.items("chatterbox"))
    else:
        values = dict(parser.defaults())
    return {normalized_key(key): value for key, value in values.items()}


def coerce_value(key: str, value: Any) -> Any:
    if value in ["", "none", "None", None]:
        return None if key in {"seed", "audio_prompt_path", "config"} else value
    if key in {"text_file", "out", "config", "audio_prompt_path"}:
        return Path(value)
    if key in {"chunk_chars", "seed"}:
        return int(value)
    if key in {
        "pause_seconds",
        "repetition_penalty",
        "min_p",
        "top_p",
        "exaggeration",
        "cfg_weight",
        "temperature",
    }:
        return float(value)
    if key in {"overwrite", "quiet"}:
        return parse_bool(value)
    return value


def coerce_values(values: dict[str, Any]) -> dict[str, Any]:
    return {key: coerce_value(key, value) for key, value in values.items()}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chatterbox",
        description="Generate speech from a text file with Chatterbox.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("text_file", nargs="?", type=Path, help="Text file to synthesize.")
    parser.add_argument("--out", type=Path, help="Output path. Supported suffixes: .wav, .mp3.")
    parser.add_argument("--config", type=Path, help=f"Optional config file, for example {DEFAULT_CONFIG_NAME}.")
    parser.add_argument("--url", help="Remote Chatterbox server URL, for example HOST_OR_IP:7860.")
    parser.add_argument("--serve", action="store_true", help="Run a Chatterbox HTTP server.")
    parser.add_argument("--host", default=SERVER_DEFAULT_HOST, help="Server host for --serve.")
    parser.add_argument("--port", type=int, default=SERVER_DEFAULT_PORT, help="Server port for --serve.")
    parser.add_argument("--write-default-config", type=Path, metavar="PATH", help="Write a default config file and exit.")
    parser.add_argument("--print-config", action="store_true", help="Print the resolved config and exit.")
    parser.add_argument("--device", choices=["auto", "cpu", "cuda", "mps"], help="Inference device.")
    parser.add_argument("--chunk-chars", type=int, help="Approximate max characters per generation chunk.")
    parser.add_argument("--pause-seconds", type=float, help="Silence inserted between generated chunks.")
    parser.add_argument("--seed", type=int, help="Optional random seed.")
    parser.add_argument("--audio-prompt-path", type=Path, help="Optional 5+ second voice reference audio.")
    parser.add_argument("--repetition-penalty", type=float)
    parser.add_argument("--min-p", type=float)
    parser.add_argument("--top-p", type=float)
    parser.add_argument("--exaggeration", type=float)
    parser.add_argument("--cfg-weight", type=float)
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--overwrite", dest="overwrite", action="store_true", help="Overwrite existing output.")
    parser.add_argument("--no-overwrite", dest="overwrite", action="store_false", help="Refuse to overwrite existing output.")
    parser.add_argument("--quiet", action="store_true", help="Only print the final output path.")
    parser.set_defaults(overwrite=None)
    return parser


def resolve_settings(args: argparse.Namespace) -> Settings:
    values = dict(DEFAULTS)
    values.update(coerce_values(load_config(args.config)))

    cli_values = {}
    for key, value in vars(args).items():
        if key in {"write_default_config", "print_config", "url", "serve", "host", "port"}:
            continue
        if value is not None:
            cli_values[normalized_key(key)] = value
    values.update(coerce_values(cli_values))

    if values.get("text_file") is None:
        raise ValueError("text_file is required")

    valid_fields = {field.name for field in fields(Settings)}
    unknown = sorted(set(values) - valid_fields)
    if unknown:
        raise ValueError(f"unknown config option(s): {', '.join(unknown)}")

    return Settings(**values)


def validate(settings: Settings, *, check_text_file: bool = True, check_ffmpeg: bool = True) -> None:
    if check_text_file and not settings.text_file.exists():
        raise FileNotFoundError(f"text file does not exist: {settings.text_file}")
    if settings.chunk_chars < 80:
        raise ValueError("--chunk-chars must be at least 80")
    if settings.pause_seconds < 0:
        raise ValueError("--pause-seconds cannot be negative")
    if settings.out.suffix.lower() not in SUPPORTED_OUTPUTS:
        supported = ", ".join(sorted(SUPPORTED_OUTPUTS))
        raise ValueError(f"unsupported output suffix {settings.out.suffix!r}; use one of: {supported}")
    if settings.out.exists() and not settings.overwrite:
        raise FileExistsError(f"output already exists: {settings.out}")
    if settings.audio_prompt_path is not None and not settings.audio_prompt_path.exists():
        raise FileNotFoundError(f"audio prompt does not exist: {settings.audio_prompt_path}")
    if check_ffmpeg and settings.out.suffix.lower() == ".mp3" and shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is required for MP3 output but was not found")


def log(settings: Settings, message: str) -> None:
    if not settings.quiet:
        print(message, file=sys.stderr)


def split_text(text: str, chunk_chars: int) -> list[str]:
    text = " ".join(text.split())
    if not text:
        raise ValueError("text file is empty")

    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        candidate = f"{current} {sentence}".strip()
        if len(candidate) > chunk_chars and current:
            chunks.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def resolve_device(device: str) -> str:
    if device != "auto":
        return device

    import torch

    if torch.cuda.is_available():
        return "cuda"
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def save_audio(wav: Any, sample_rate: int, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)

    import torchaudio as ta

    if out_path.suffix.lower() == ".wav":
        ta.save(str(out_path), wav, sample_rate)
        return

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        ta.save(str(tmp_path), wav, sample_rate)
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(tmp_path),
                "-codec:a",
                "libmp3lame",
                "-q:a",
                "2",
                str(out_path),
            ],
            check=True,
        )
    finally:
        tmp_path.unlink(missing_ok=True)


def print_settings(settings: Settings) -> None:
    for field in fields(settings):
        value = getattr(settings, field.name)
        print(f"{field.name} = {value}")


def write_default_config(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    print(path)


def load_model(device_name: str) -> tuple[Any, Any]:
    import torch
    from chatterbox.tts import ChatterboxTTS

    device = resolve_device(device_name)
    model = ChatterboxTTS.from_pretrained(device=device)
    default_conds = copy.deepcopy(model.conds)
    return model, default_conds


def generate_wav(settings: Settings, text: str, model: Any, default_conds: Any) -> tuple[Any, int]:
    import torch

    if settings.seed is not None:
        torch.manual_seed(settings.seed)

    chunks = split_text(text, settings.chunk_chars)

    if settings.audio_prompt_path is not None:
        log(settings, f"Preparing voice reference: {settings.audio_prompt_path}")
        model.prepare_conditionals(
            settings.audio_prompt_path,
            exaggeration=settings.exaggeration,
        )
    elif default_conds is not None:
        model.conds = copy.deepcopy(default_conds)

    wavs = []
    pause = torch.zeros(1, int(model.sr * settings.pause_seconds))

    for index, chunk in enumerate(chunks, start=1):
        log(settings, f"Generating chunk {index}/{len(chunks)}")
        wavs.append(
            model.generate(
                chunk,
                repetition_penalty=settings.repetition_penalty,
                min_p=settings.min_p,
                top_p=settings.top_p,
                audio_prompt_path=None,
                exaggeration=settings.exaggeration,
                cfg_weight=settings.cfg_weight,
                temperature=settings.temperature,
            )
        )
        if index < len(chunks) and settings.pause_seconds > 0:
            wavs.append(pause)

    wav = torch.cat(wavs, dim=1)
    return wav, model.sr


def run(settings: Settings) -> Path:
    validate(settings)

    device = resolve_device(settings.device)
    log(settings, f"Loading Chatterbox on {device}")
    model, default_conds = load_model(device)
    wav, sample_rate = generate_wav(
        settings,
        settings.text_file.read_text(encoding="utf-8"),
        model,
        default_conds,
    )
    save_audio(wav, sample_rate, settings.out)
    return settings.out


def normalize_server_url(url: str) -> str:
    if "://" not in url:
        url = f"http://{url}"
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"invalid server URL: {url}")
    return f"{url.rstrip('/')}/synthesize"


def remote_payload(settings: Settings) -> dict[str, Any]:
    values: dict[str, Any] = {
        "text": settings.text_file.read_text(encoding="utf-8"),
        "output_suffix": settings.out.suffix.lower(),
        "settings": {
            "device": settings.device,
            "chunk_chars": settings.chunk_chars,
            "pause_seconds": settings.pause_seconds,
            "seed": settings.seed,
            "repetition_penalty": settings.repetition_penalty,
            "min_p": settings.min_p,
            "top_p": settings.top_p,
            "exaggeration": settings.exaggeration,
            "cfg_weight": settings.cfg_weight,
            "temperature": settings.temperature,
            "quiet": settings.quiet,
        },
    }
    if settings.audio_prompt_path is not None:
        values["audio_prompt"] = {
            "name": settings.audio_prompt_path.name,
            "data": base64.b64encode(settings.audio_prompt_path.read_bytes()).decode("ascii"),
        }
    return values


def run_remote(settings: Settings, url: str) -> Path:
    validate(settings, check_ffmpeg=False)
    endpoint = normalize_server_url(url)
    body = json.dumps(remote_payload(settings)).encode("utf-8")
    request = Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "audio/*"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=None) as response:
            audio = response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"server returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"could not reach server {url}: {exc.reason}") from exc

    if settings.out.exists() and not settings.overwrite:
        raise FileExistsError(f"output already exists: {settings.out}")
    settings.out.parent.mkdir(parents=True, exist_ok=True)
    settings.out.write_bytes(audio)
    return settings.out


class ChatterboxServer:
    def __init__(self, device: str) -> None:
        import threading

        self.device = resolve_device(device)
        print(f"Loading Chatterbox on {self.device}", file=sys.stderr)
        self.model, self.default_conds = load_model(self.device)
        self.lock = threading.Lock()

    def synthesize(self, payload: dict[str, Any]) -> tuple[bytes, str]:
        text = str(payload.get("text") or "")
        suffix = str(payload.get("output_suffix") or ".wav").lower()
        request_settings = dict(payload.get("settings") or {})
        request_settings.update(
            {
                "text_file": Path("__request__.txt"),
                "out": Path(f"audio{suffix}"),
                "device": self.device,
                "overwrite": True,
            }
        )

        audio_prompt = payload.get("audio_prompt")
        with tempfile.TemporaryDirectory(prefix="chatterbox-server-") as tmp_dir:
            if audio_prompt:
                prompt_path = Path(tmp_dir) / str(audio_prompt.get("name") or "prompt.wav")
                prompt_path.write_bytes(base64.b64decode(str(audio_prompt["data"])))
                request_settings["audio_prompt_path"] = prompt_path

            settings = Settings(**coerce_values(request_settings))
            validate(settings, check_text_file=False)
            out_path = Path(tmp_dir) / f"response{settings.out.suffix.lower()}"
            settings = Settings(**{**settings.__dict__, "out": out_path})

            with self.lock:
                wav, sample_rate = generate_wav(settings, text, self.model, self.default_conds)
                save_audio(wav, sample_rate, out_path)
            media_type = "audio/mpeg" if out_path.suffix.lower() == ".mp3" else "audio/wav"
            return out_path.read_bytes(), media_type


def make_handler(server_state: ChatterboxServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/health":
                self.send_response(HTTPStatus.OK)
                self.end_headers()
                self.wfile.write(b"ok\n")
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            if self.path != "/synthesize":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                audio, media_type = server_state.synthesize(payload)
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", media_type)
                self.send_header("Content-Length", str(len(audio)))
                self.end_headers()
                self.wfile.write(audio)
            except Exception as exc:
                body = json.dumps({"error": str(exc)}).encode("utf-8")
                self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)

    return Handler


def serve(host: str, port: int, device: str) -> int:
    state = ChatterboxServer(device)
    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    print(f"Chatterbox server listening on http://{host}:{port}", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.write_default_config is not None:
            write_default_config(args.write_default_config)
            return 0

        if args.serve:
            return serve(args.host, args.port, args.device or DEFAULTS["device"])

        settings = resolve_settings(args)
        if args.print_config:
            validate(settings)
            print_settings(settings)
            return 0

        output = run_remote(settings, args.url) if args.url else run(settings)
        print(output)
        return 0
    except Exception as exc:
        print(f"chatterbox: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
