# voice-controls

Say **"Hey Claude, …"** into your headset to start a Claude Code agent, without leaving your game or coming back to the desk. It sits inside a [Brain](https://github.com/7shep/newbrain) checkout as `voice-controls/`, the same way `personal-notes/` does: its own repo, ignored by Brain.

| You say | What happens |
| --- | --- |
| "Hey Claude, …" | A new Claude Code background agent (`claude --bg`) starts in the Brain checkout with that prompt |
| "Hey Claude, also …" | Continues the last voice agent, with its conversation |
| "Hey Claude, note / idea / to-do …" or "remind me to …" | Appended to Brain's `inbox.md` in the capture format |
| "Hey Claude." *(pause)* | Chime, then your next sentence (within 8 s) is the request |

Voice agents are ordinary background sessions. They show up in `claude agents`, and `claude attach <id>` opens one. If no interactive Claude terminal is open, a Windows Terminal window titled "Claude voice agents" opens on the agent view. While a fullscreen game is in front, it waits and opens when you leave the game, because a new window would take focus. A spoken line confirms each request and says when an agent is done or needs you, such as for a permission prompt. Agents use your normal Claude Code permission settings.

Everything before the agent runs locally. WebRTC VAD splits the mic into utterances, faster-whisper (`base.en`, CPU, int8, 2 threads) transcribes them, and only utterances starting with the wake phrase are acted on or logged. It also accepts common mishearings ("hey cloud", "hey clawed"). When idle, it's one `pythonw` process doing VAD on 30 ms frames. Whisper runs only when someone is speaking.

## Setup (Windows)

```powershell
cd voice-controls
py -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python voice.py --list-devices      # find your mic
copy config.example.json config.json             # optional: set input_device etc.
.venv\Scripts\python voice.py                    # try it in the foreground (Ctrl+C to stop)
powershell -ExecutionPolicy Bypass -File install-startup.ps1   # then: always on, from sign-in, no window
```

The first run downloads the Whisper model (~150 MB). Needs the `claude` CLI installed and logged in. Only one listener runs at a time. To stop the background one, run `.venv\Scripts\python voice.py --stop`. To undo the sign-in launch, delete "Brain voice controls" from `shell:startup`.

## Config

`config.json` (ignored by Git) overrides any key in `DEFAULTS` at the top of `voice.py`:

- `input_device`: device index or name substring, such as `"DualSense"`. Defaults to the system mic.
- `whisper_model`: `tiny.en` is lighter, `small.en` is more accurate. `whisper_threads` caps CPU use.
- `permission_mode`: passed to the agents, such as `"acceptEdits"`. Defaults to your normal setting.
- `open_terminal`: set to `false` to never open the agent view.
- `notes`: Brain notes root. Defaults to `$BRAIN_NOTES`, then `../personal-notes`.
- `claude_cwd`: where agents run. Defaults to the Brain checkout.
- `speak`, `poll_s`, `silence_ms`, `vad_aggressiveness`, `follow_window_s`: see `voice.py`.

## Testing without a mic

```powershell
.venv\Scripts\python voice.py --dry-run --text "hey claude, note buy milk"   # routing only
.venv\Scripts\python voice.py --dry-run --wav clip.wav                       # 16 kHz mono WAV through Whisper
py -m unittest discover -s tests                                             # parsing/routing tests, no deps needed
```

## Things to know

- **Bluetooth headphones:** while any app holds a Bluetooth headset's mic open, Windows switches it to the Hands-Free profile, and game audio drops to low-quality mono. With this always listening, that's all the time. Use a separate mic or a 2.4 GHz dongle if you can.
- **Open mic in Discord:** your squad hears you say "hey claude".
- Agents get a note that the request was dictated, so they ask instead of guessing at likely mishearings.
- Requests and agent IDs are logged to `voice.log` (ignored by Git). Speech without the wake phrase is never logged.
