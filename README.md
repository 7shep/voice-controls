# voice-controls

Say **"Hey Claude, …"** into your headset and it runs in the background, so you can stay in your game or away from the desk. Built to sit inside a [Brain](https://github.com/7shep/newbrain) checkout as `voice-controls/`, the same way `personal-notes/` does: its own repo, ignored by Brain.

| You say | What happens |
| --- | --- |
| "Hey Claude, note / idea / to-do …" or "remind me to …" | Appended to Brain's `inbox.md` in the capture format, confirmed aloud |
| "Hey Claude, …" | `claude -p` runs in the Brain checkout; the short reply is read aloud |
| "Hey Claude, also …" | Continues the last voice conversation |
| "Hey Claude." *(pause)* | Chime, then your next sentence (within 8 s) is the request |

Everything is local: WebRTC VAD splits the mic into utterances, faster-whisper (`base.en`, CPU, int8) transcribes them, and only utterances starting with the wake phrase are acted on or logged. It also accepts common mishearings ("hey cloud", "hey clawed").

## Setup (Windows)

```powershell
cd voice-controls
py -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python voice.py --list-devices      # find your mic
copy config.example.json config.json             # optional: set input_device etc.
.venv\Scripts\python voice.py                    # listen
```

The first run downloads the Whisper model (~150 MB). Needs the `claude` CLI on PATH and logged in.

**Run at login, without a window:** put a shortcut in `shell:startup` targeting
`<path>\voice-controls\.venv\Scripts\pythonw.exe <path>\voice-controls\voice.py`.

## Config

`config.json` (ignored by Git) overrides any key in `DEFAULTS` at the top of `voice.py`:

- `input_device`: device index or name substring, such as `"DualSense"`. Defaults to the system mic.
- `whisper_model`: `tiny.en` is faster, `small.en` is more accurate.
- `allowed_tools`: tools Claude may use without asking. Voice runs are headless, so anything outside this list is refused. The default is read-only: `Read`, `Glob`, `Grep`, `WebSearch`, `WebFetch`.
- `notes`: Brain notes root. Defaults to `$BRAIN_NOTES`, then `../personal-notes`.
- `claude_cwd`: where Claude runs. Defaults to the Brain checkout.
- `speak`, `silence_ms`, `vad_aggressiveness`, `follow_window_s`: see `voice.py`.

## Testing without a mic

```powershell
.venv\Scripts\python voice.py --dry-run --text "hey claude, note buy milk"   # routing only
.venv\Scripts\python voice.py --dry-run --wav clip.wav                       # 16 kHz mono WAV through Whisper
py -m unittest discover -s tests                                             # parsing/routing tests, no deps needed
```

## Things to know

- **Bluetooth headphones:** while any app holds a Bluetooth headset's mic open, Windows switches it to the Hands-Free profile, and game audio drops to low-quality mono. Use a separate mic or a 2.4 GHz dongle if you have one, or only run this when you're away from the desk.
- **Open mic in Discord:** your squad hears you say "hey claude".
- Prompts and replies are logged to `voice.log` (ignored by Git). Speech without the wake phrase is never logged.
