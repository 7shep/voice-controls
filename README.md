# voice-controls

**Say "Hey Claude, …" and a [Claude Code](https://claude.com/claude-code) agent starts working, without alt-tabbing out of your game or walking back to your desk.**

I wanted to hand off tasks while mid-match, or from across the room with headphones in. This listens on your mic, picks out requests that start with "Hey Claude", and runs each one as a headless Claude Code agent. When the agent finishes, it reads you a one-sentence summary of what it did.

| You say | What happens |
| --- | --- |
| "Hey Claude, …" | A headless Claude Code agent (`claude -p`) runs that prompt with no window |
| "Hey Claude, also …" | Continues the last voice agent, with its conversation |
| "Hey Claude, note / idea / to-do …" or "remind me to …" | Appended to a markdown inbox file |
| "Hey Claude." *(pause)* | Chime, then your next sentence (within 8 s) is the request |

## How it works

```
mic ─► WebRTC VAD ─► first 3 s through Whisper: "hey claude"? ─► full transcript ─► claude -p "<request>"
          │                         │ no                                                │
     30 ms frames,               dropped, never logged                    one-sentence summary read aloud
     ~0.2% of a core                                                      when the agent finishes
```

- **Local speech recognition.** [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (`base.en`, int8, 2 CPU threads) runs on the CPU, so the GPU stays with the game. Audio never leaves your machine; only the request text goes to Claude.
- **Cheap when idle.** Voice-activity detection runs on 30 ms frames and splits speech into phrases at short pauses. Whisper checks only the first 3 seconds of each phrase for the wake phrase, and transcribes the whole request only after hearing it.
- **Doesn't break your game.** Agents run headless with no window, so nothing takes focus. You can run several at once. Each session is saved, so `claude --resume` opens one later if you want the details.
- **Tolerates mishearings.** "Hey cloud" and "hey clawed" count as the wake phrase. Agents are told the request was dictated, so they ask instead of guessing at a misheard word.

## Requirements

- Windows 10/11 (the chimes and speech use Windows APIs)
- Python 3.9+
- [Claude Code](https://claude.com/claude-code), installed and logged in (`claude` on PATH)
- A microphone

## Setup

```powershell
git clone https://github.com/7shep/voice-controls
cd voice-controls
py -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python voice.py --list-devices      # find your mic
copy config.example.json config.json             # then edit it (optional)
.venv\Scripts\python voice.py                    # try it in the foreground (Ctrl+C to stop)
powershell -ExecutionPolicy Bypass -File install-startup.ps1   # then: always on, from sign-in, no window
```

The first run downloads the Whisper model (~150 MB). Only one listener runs at a time. To stop the background one, run `.venv\Scripts\python voice.py --stop`. To undo the sign-in launch, delete "Voice controls" from `shell:startup`.

## Config

`config.json` (ignored by Git) overrides any key in `DEFAULTS` at the top of `voice.py`. `~` works in paths.

- `claude_cwd`: the folder agents start in, usually your main project. Defaults to your home folder.
- `inbox`: the markdown file for note/idea/to-do captures. Defaults to `inbox.md` next to `voice.py`.
- `input_device`: device index or name substring, such as `"Headset"`. Defaults to the system mic.
- `permission_mode`: defaults to `"auto"`. A headless agent can't ask you for approval, so auto mode's safety checks approve or block each action, and anything that would need a prompt is refused. Use `"acceptEdits"` to allow file edits only, or `null` for your normal setting (headless, that usually means read-only).
- `whisper_model`: `tiny.en` is lighter, `small.en` is more accurate. `whisper_threads` caps CPU use.
- `agent_timeout_s`: how long an agent may run. Defaults to 30 minutes.
- `vad_aggressiveness`: 0–3, default 3. Lower it if quiet speech is missed.
- `speak`, `silence_ms`, `follow_window_s`: see `voice.py`.

### Using it with Brain

Captures are written as `- 2026-10-28 14:05 · note · text`, the same line format as the inbox of [Brain](https://github.com/7shep/newbrain), a personal notes app. To make voice captures land in your Brain inbox and agents start in your Brain checkout, set:

```json
{ "inbox": "~/newbrain/personal-notes/inbox.md", "claude_cwd": "~/newbrain" }
```

## Testing without a mic

```powershell
.venv\Scripts\python voice.py --dry-run --text "hey claude, note buy milk"   # routing only
.venv\Scripts\python voice.py --dry-run --wav clip.wav                       # 16 kHz mono WAV through Whisper
py -m unittest discover -s tests                                             # parsing/routing tests, no deps needed
```

## Things to know

- **Bluetooth headphones:** while any app holds a Bluetooth headset's mic open, Windows switches it to the Hands-Free profile, and game audio drops to low-quality mono. With this always listening, that's all the time. Use a separate mic or a 2.4 GHz dongle if you can.
- **Open mic in Discord:** your squad hears you say "hey claude".
- Agents can edit files and run commands in `claude_cwd`, within what auto mode allows. Point `claude_cwd` at a project you're happy to let a voice request change.
- Requests and agent IDs are logged to `voice.log` (ignored by Git). Speech without the wake phrase is never logged.

## License

[MIT](LICENSE)
