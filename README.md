# Audiolab Chatterbox

Small CLI for generating speech from text files with Chatterbox Turbo.

## Requirements

- Python 3.11
- `ffmpeg` for MP3 output
- Network access on first install and first model download

## Install

From this directory:

```bash
python3.11 -m venv .venv-chatterbox-turbo
.venv-chatterbox-turbo/bin/python -m pip install --upgrade pip
.venv-chatterbox-turbo/bin/python -m pip install chatterbox-tts
chmod +x chatterbox
```

## Download The Model

The model is downloaded automatically the first time you run the CLI:

```bash
./chatterbox input.txt --out audio.wav
```

Chatterbox Turbo downloads weights from Hugging Face:

```text
ResembleAI/chatterbox-turbo
```

If you want to prefetch the model without generating a real audio file, create a tiny text file and run:

```bash
printf 'Test.' > /tmp/chatterbox-download-test.txt
./chatterbox /tmp/chatterbox-download-test.txt --out /tmp/chatterbox-download-test.wav
```

After the first successful run, the model should be available from the local Hugging Face cache.

## Usage

Generate WAV:

```bash
./chatterbox story.txt --out audio.wav
```

Generate MP3:

```bash
./chatterbox story.txt --out audio.mp3
```

Use a config file:

```bash
./chatterbox story.txt --config config.conf --out audio.wav
```

Inspect resolved settings without generating audio:

```bash
./chatterbox story.txt --config config.conf --print-config
```

Write a fresh default config:

```bash
./chatterbox --write-default-config my-config.conf
```

## Server Mode

Start a warm Chatterbox server on this machine:

```bash
make up
```

By default it listens on `0.0.0.0:7860`. Override the bind address, port, or device when needed:

```bash
make up HOST=0.0.0.0 PORT=9000 DEVICE=cuda
```

Stop it:

```bash
make down
```

From another device with this repo installed, send work to the server:

```bash
./chatterbox story.txt --url 192.168.1.20:7860 --out audio.wav
```

The client reads the local text file, sends it and the generation settings to the server, then writes the returned audio to `--out` on the client device.

## Notes

- First run may take time because the model weights must download.
- CPU generation works but is slower than CUDA.
- Generated audio files and the local virtualenv are ignored by git.
