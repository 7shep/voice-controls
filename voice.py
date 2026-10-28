#!/usr/bin/env python3
"""Voice controls for Claude Code: say "Hey Claude, <request>" and an agent starts without leaving your game.

Listens on the mic, cuts speech into utterances with WebRTC VAD, transcribes each one locally with
faster-whisper, and acts only on utterances that start with the wake phrase:
  "hey claude, note/idea/todo ..."  -> appended to a markdown inbox
  "hey claude, also ..."            -> continues the last voice agent
  "hey claude, <anything else>"     -> a new Claude Code background agent (claude --bg)
Agents show up in `claude agents`; when no interactive Claude terminal is open, one opens on the agent view,
minimised and without taking focus. A spoken line says when an agent finishes or is waiting on you.
Saying just "hey claude" chimes and treats the next utterance (within a few seconds) as the request.

Run: python voice.py [--config config.json] [--list-devices] [--text "..."] [--wav file.wav] [--dry-run] [--stop]
Only wake-phrase utterances are logged; other speech is transcribed and discarded.
"""
import argparse
import collections
import datetime
import json
import logging
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
RATE = 16000
FRAME_MS = 30
FRAME = RATE * FRAME_MS // 1000  # samples per VAD frame
GATE_S = 3                        # seconds transcribed to look for the wake phrase
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # keep child processes from stealing focus on Windows
PID_FILE = os.path.join(HERE, "voice.pid")

DEFAULTS = {
    "input_device": None,          # index or name substring; None = system default mic
    "whisper_model": "base.en",
    "whisper_device": "cpu",       # cpu keeps the GPU free for the game
    "whisper_compute": "int8",
    "whisper_threads": 2,          # leave the other cores to the game
    "vad_aggressiveness": 2,       # 0-3, higher ignores more non-speech
    "silence_ms": 900,             # this much silence ends an utterance
    "max_utterance_s": 30,
    "follow_window_s": 8,          # after a bare "hey claude", how long to wait for the request
    "inbox": None,                 # markdown file for note/idea/todo; None = inbox.md next to this script
    "claude_cwd": None,            # folder agents start in; None = your home folder
    "permission_mode": None,       # None = your normal Claude Code setting
    "open_terminal": True,         # open the agent view when no interactive Claude terminal is running
    "poll_s": 15,                  # how often to check on running voice agents
    "speak": True,
    "max_spoken_chars": 400,
}

VOICE_NOTE = ("This task was dictated by voice and transcribed automatically, so words may be misheard. "
              "If the request is ambiguous or something looks like a transcription error, ask before acting on a guess.")
TERMINAL_TITLE = "Claude voice agents"
BACKGROUNDED = re.compile(r"backgrounded[^\w\n]+([0-9a-f]+)(?:[^\w\n]+(.+))?")

WAKE = re.compile(r"\b(?:hey|hi|okay|ok|a)[\s,.!?-]+(?:claude|claud|clawed|cloud|clod|clyde|claudia)\b[\s,.!?:;-]*", re.I)
CAPTURE = re.compile(r"^(?:(?:take|make|add)\s+an?\s+)?(note|idea|to[\s-]?do)\b[\s,.:;-]*(?:that\s+)?(.*)$", re.I | re.S)
REMIND = re.compile(r"^remind\s+me\s+to\s+(.*)$", re.I | re.S)
FOLLOW = re.compile(r"^(?:also|and|follow\s+up|continue)\b[\s,.:;-]*", re.I)

log = logging.getLogger("voice")


def load_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    cfg["inbox"] = os.path.expanduser(cfg["inbox"] or os.path.join(HERE, "inbox.md"))
    cfg["claude_cwd"] = os.path.expanduser(cfg["claude_cwd"] or "~")
    return cfg


def parse_wake(text):
    """The request after the wake phrase, "" for a bare wake phrase, or None when it wasn't addressed to Claude."""
    m = WAKE.search(text)
    if not m or m.start() > 20:  # allow a little lead-in ("uh, hey claude") but not a mid-sentence mention
        return None
    return text[m.end():].strip()


def route(request):
    """("capture", kind, text) | ("claude", prompt, follow_up)."""
    request = request.strip()
    m = REMIND.match(request)
    if m and m.group(1).strip():
        return ("capture", "todo", m.group(1).strip())
    m = CAPTURE.match(request)
    if m and m.group(2).strip():
        kind = m.group(1).lower()
        return ("capture", "todo" if kind.startswith("to") else kind, m.group(2).strip())
    m = FOLLOW.match(request)
    if m and request[m.end():].strip():
        return ("claude", request[m.end():].strip(), True)
    return ("claude", request, False)


def append_inbox(path, kind, text):
    """Adds "- 2026-10-28 14:05 · note · text", the line format Brain's inbox.md uses, so it can point at one."""
    text = " ".join(text.split())[:2000]
    current = "# Inbox\n"
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            current = f.read() or current
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(current.rstrip("\n") + "\n- %s · %s · %s\n" % (stamp, kind, text))


# --- feedback -------------------------------------------------------------------------------

def chime(*tones):
    """Short beeps on a side thread: (frequency, ms) pairs."""
    def play():
        try:
            import winsound
            for freq, ms in tones:
                winsound.Beep(freq, ms)
        except Exception:
            pass  # no winsound off Windows; the spoken reply still lands
    threading.Thread(target=play, daemon=True).start()


WAKE_TONE = ((880, 90),)
OK_TONE = ((880, 70), (1320, 90))
ERROR_TONE = ((330, 250),)


class Speaker:
    """Serialises text-to-speech so replies never talk over each other. Windows System.Speech, no extra deps."""

    PS = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
          "$s.Speak([Console]::In.ReadToEnd())")

    def __init__(self, enabled, limit):
        self.enabled, self.limit, self.q = enabled, limit, queue.Queue()
        threading.Thread(target=self._run, daemon=True).start()

    def say(self, text):
        print("say:", text)
        if self.enabled and text:
            self.q.put(text)

    def _run(self):
        while True:
            text = self.q.get()
            if len(text) > self.limit:
                text = text[:self.limit].rsplit(" ", 1)[0] + "."
            try:
                subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", self.PS],
                               input=text.encode("ascii", "replace"), creationflags=NO_WINDOW, timeout=120)
            except Exception as e:
                log.warning("speech failed: %s", e)
            self.q.task_done()


# --- actions --------------------------------------------------------------------------------

def agent_name(prompt):
    """A short session name from the request's first words, e.g. "voice: check the build"."""
    words = " ".join(prompt.split()[:6]).rstrip(".,!?;:")
    return ("voice: " + words)[:50]


def parse_backgrounded(output):
    """(short id, name) from `claude --bg` output ("backgrounded · d50e5122 · name"), or None."""
    m = BACKGROUNDED.search(output)
    return (m.group(1), (m.group(2) or "").strip()) if m else None


def spoken(name):
    return name[len("voice: "):] if name.startswith("voice: ") else name


def user_busy():
    """True while a fullscreen game or presentation is in front: Windows' own do-not-disturb check, plus a
    foreground-window-covers-the-monitor check for borderless games it misses."""
    import ctypes
    from ctypes import wintypes
    state = ctypes.c_int()
    if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0 and state.value in (2, 3, 4):
        return True  # QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    cls = ctypes.create_unicode_buffer(64)
    if not hwnd or not user32.GetClassNameW(hwnd, cls, 64) or cls.value in ("Progman", "WorkerW"):  # the desktop
        return False

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD)]
    rect, info = wintypes.RECT(), MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    user32.MonitorFromWindow.restype = wintypes.HMONITOR
    monitor = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)) or not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return False
    m = info.rcMonitor
    return (rect.left, rect.top, rect.right, rect.bottom) == (m.left, m.top, m.right, m.bottom)


def terminal_open():
    """Whether the agent view this tool opens (titled TERMINAL_TITLE) is still on screen."""
    import ctypes
    return bool(ctypes.windll.user32.FindWindowW(None, TERMINAL_TITLE))


def claude_exe():
    exe = shutil.which("claude") or shutil.which(os.path.expanduser("~/.local/bin/claude"))  # pythonw at sign-in may lack PATH
    if not exe:
        raise RuntimeError("the claude command isn't installed or on PATH")
    return exe


class Agents:
    """Starts voice requests as Claude Code background agents, opens the agent view when no Claude terminal is open,
    and says when a voice agent finishes or is waiting on you."""

    def __init__(self, cfg, speaker):
        self.cfg, self.speaker, self.q = cfg, speaker, queue.Queue()
        self.last = None        # short id of the most recent voice agent, for "also ..."
        self.watched = {}       # short id -> [name, last state seen]
        self.terminal_pending = False  # an agent view is queued to open once you leave a fullscreen game
        self.lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True).start()
        threading.Thread(target=self._watch, daemon=True).start()

    def submit(self, prompt, follow_up):
        self.q.put((prompt, follow_up))

    def claude(self, args, timeout=60):
        out = subprocess.run([claude_exe()] + args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                             cwd=self.cfg["claude_cwd"], timeout=timeout, creationflags=NO_WINDOW)
        if out.returncode:
            raise RuntimeError((out.stderr or out.stdout or "exit %d" % out.returncode).strip()[:200])
        return out.stdout

    def sessions(self):
        return json.loads(self.claude(["agents", "--json", "--all"]))

    def _run(self):
        while True:
            prompt, follow_up = self.q.get()
            try:
                self.speaker.say("Continuing the agent." if self.launch(prompt, follow_up) else "Agent started.")
                self.ensure_terminal()
            except Exception as e:
                log.exception("agent launch failed")
                chime(*ERROR_TONE)
                self.speaker.say("Couldn't start the agent: %s" % e)
            self.q.task_done()

    def launch(self, prompt, follow_up):
        """Starts the agent; True when it continued the previous voice agent rather than starting fresh."""
        args = ["--bg", "--append-system-prompt", VOICE_NOTE]
        if self.cfg["permission_mode"]:
            args += ["--permission-mode", self.cfg["permission_mode"]]
        previous = self.find(self.last) if follow_up and self.last else None
        if previous:
            if previous.get("state") == "done":  # an idle finished agent; stop it so --resume continues it in place
                self.claude(["stop", self.last])
            args += ["--resume", previous["sessionId"]]
        else:
            args += ["-n", agent_name(prompt)]
        parsed = parse_backgrounded(self.claude(args + [prompt]))
        if not parsed:
            raise RuntimeError("claude didn't report a background session")
        short, name = parsed
        name = name or (previous or {}).get("name") or agent_name(prompt)
        with self.lock:
            self.watched[short] = [name, None]
            self.last = short
        log.info("agent %s: %s", short, name)
        return bool(previous)

    def find(self, short):
        return next((s for s in self.sessions() if s.get("id") == short), None)

    def ensure_terminal(self):
        if not self.cfg["open_terminal"] or os.name != "nt" or self.terminal_pending or not self.needs_terminal():
            return
        self.terminal_pending = True
        threading.Thread(target=self._open_terminal, daemon=True).start()

    def needs_terminal(self):
        return not terminal_open() and not any(s.get("kind") == "interactive" for s in self.sessions())

    def _open_terminal(self):
        """Opens the agent view, waiting until you leave a fullscreen game: any new window would take focus."""
        try:
            while user_busy():
                time.sleep(2)
            if self.needs_terminal():
                wt = shutil.which("wt")
                if wt:
                    subprocess.Popen([wt, "-w", "new", "new-tab", "--title", TERMINAL_TITLE, "--suppressApplicationTitle",
                                      claude_exe(), "agents"], cwd=self.cfg["claude_cwd"], creationflags=NO_WINDOW)
                else:
                    subprocess.Popen([claude_exe(), "agents"], cwd=self.cfg["claude_cwd"],
                                     creationflags=subprocess.CREATE_NEW_CONSOLE)
        except Exception:
            log.exception("couldn't open the agent view")
        self.terminal_pending = False

    def _watch(self):
        while True:
            time.sleep(self.cfg["poll_s"])
            with self.lock:
                if not self.watched:
                    continue
            try:
                states = {s.get("id"): s.get("state") for s in self.sessions()}
            except Exception:
                log.exception("agent poll failed")
                continue
            with self.lock:
                for short, entry in list(self.watched.items()):
                    state = states.get(short, "gone")
                    if state == entry[1]:
                        continue
                    entry[1] = state
                    if state == "done":
                        self.speaker.say("%s is done." % spoken(entry[0]))
                    elif state == "blocked":
                        self.speaker.say("%s needs you." % spoken(entry[0]))
                    elif state in ("failed", "error"):
                        self.speaker.say("%s stopped with an error." % spoken(entry[0]))
                    if state in ("done", "failed", "error", "stopped", "gone"):
                        del self.watched[short]


class Handler:
    """Turns transcripts into actions; holds the "bare wake phrase, waiting for the request" state."""

    def __init__(self, cfg, dry_run=False):
        self.cfg, self.dry_run, self.awaiting = cfg, dry_run, 0.0
        self.speaker = Speaker(cfg["speak"] and not dry_run, cfg["max_spoken_chars"])
        self.agents = None if dry_run else Agents(cfg, self.speaker)

    def expecting(self):
        """Inside the window after a bare "hey claude", when the next utterance is the request."""
        return time.time() <= self.awaiting

    def text(self, transcript):
        request = parse_wake(transcript)
        if request is None:
            if time.time() > self.awaiting or not transcript.strip():
                return None
            request = transcript.strip()  # the follow-on utterance after a bare "hey claude"
        log.info("heard: %s", transcript)
        if not request:
            self.awaiting = time.time() + self.cfg["follow_window_s"]
            chime(*WAKE_TONE)
            return ("wake",)
        self.awaiting = 0.0
        action = route(request)
        print("->", action)
        if self.dry_run:
            return action
        chime(*OK_TONE)
        if action[0] == "capture":
            append_inbox(self.cfg["inbox"], action[1], action[2])
            self.speaker.say("Saved %s." % ("a " + action[1] if action[1] != "idea" else "an idea"))
        else:
            self.agents.submit(action[1], action[2])
        return action


# --- audio ----------------------------------------------------------------------------------

def load_whisper(cfg):
    from faster_whisper import WhisperModel
    print("loading whisper %s on %s..." % (cfg["whisper_model"], cfg["whisper_device"]))
    return WhisperModel(cfg["whisper_model"], device=cfg["whisper_device"], compute_type=cfg["whisper_compute"],
                        cpu_threads=cfg["whisper_threads"])


def transcribe(model, pcm):
    import numpy as np
    audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
    segments, _ = model.transcribe(audio, language="en", beam_size=1, condition_on_previous_text=False)
    return " ".join(s.text for s in segments if s.no_speech_prob < 0.6).strip()


def resolve_device(spec):
    if spec is None or isinstance(spec, int):
        return spec
    import sounddevice as sd
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and str(spec).lower() in d["name"].lower():
            return i
    raise SystemExit("No input device matching %r (try --list-devices)" % spec)


def utterances(cfg):
    """Yields raw 16 kHz mono int16 PCM for each stretch of speech."""
    import sounddevice as sd
    import webrtcvad
    vad = webrtcvad.Vad(cfg["vad_aggressiveness"])
    frames = queue.Queue()
    pre = collections.deque(maxlen=10)    # 300 ms of audio kept from before speech starts
    recent = collections.deque(maxlen=10)
    end_frames = cfg["silence_ms"] // FRAME_MS
    max_frames = cfg["max_utterance_s"] * 1000 // FRAME_MS
    buf, silent = None, 0

    def callback(indata, n, t, status):
        frames.put(bytes(indata))

    with sd.RawInputStream(samplerate=RATE, blocksize=FRAME, channels=1, dtype="int16",
                           device=resolve_device(cfg["input_device"]), callback=callback):
        print("listening. Say \"hey claude, ...\" (Ctrl+C to stop)")
        while True:
            f = frames.get()
            speech = vad.is_speech(f, RATE)
            if buf is None:
                pre.append(f)
                recent.append(speech)
                if sum(recent) >= 6:
                    buf, silent = list(pre), 0
                    recent.clear()
                continue
            buf.append(f)
            silent = 0 if speech else silent + 1
            if silent >= end_frames or len(buf) >= max_frames:
                if len(buf) - silent > 13:  # ignore blips under ~0.4 s of speech
                    yield b"".join(buf)
                buf = None
                pre.clear()


def read_wav(path):
    import wave
    with wave.open(path, "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (RATE, 1, 2):
            raise SystemExit("--wav needs 16 kHz mono 16-bit audio")
        return w.readframes(w.getnframes())


def single_instance():
    """Holds a lock on voice.lock so the sign-in launch and a manual run don't both listen."""
    f = open(os.path.join(HERE, "voice.lock"), "a")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def stop_running():
    free = single_instance()
    if free:  # nobody holds the lock, so any saved pid is stale and may belong to another process now
        free.close()
        return False
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
        os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError):
        return False
    os.remove(PID_FILE)
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--list-devices", action="store_true", help="print audio devices and exit")
    ap.add_argument("--text", help="handle one transcript instead of listening")
    ap.add_argument("--wav", help="transcribe and handle one 16 kHz mono WAV instead of listening")
    ap.add_argument("--dry-run", action="store_true", help="print what would happen without doing it")
    ap.add_argument("--stop", action="store_true", help="stop the background listener and exit")
    args = ap.parse_args()

    if args.stop:
        print("stopped" if stop_running() else "not running")
        return

    if args.list_devices:
        import sounddevice as sd
        print(sd.query_devices())
        return
    logging.basicConfig(filename=os.path.join(HERE, "voice.log"), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
    cfg = load_config(args.config)
    handler = Handler(cfg, dry_run=args.dry_run)

    if args.text or args.wav:
        transcript = args.text or transcribe(load_whisper(cfg), read_wav(args.wav))
        print("transcript:", transcript)
        if handler.text(transcript) is None:
            print("(not addressed to Claude)")
        if not args.dry_run:  # let the agent launch and its spoken confirmation finish before exiting
            handler.agents.q.join()
            handler.speaker.q.join()
        return

    lock = single_instance()
    if not lock:
        print("already listening (python voice.py --stop to stop it)")
        return
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    model = load_whisper(cfg)
    try:
        for pcm in utterances(cfg):
            head = pcm[:RATE * 2 * GATE_S]  # the wake phrase opens the utterance, so check there before paying for the rest
            if len(pcm) > len(head) and not handler.expecting() and parse_wake(transcribe(model, head)) is None:
                continue
            transcript = transcribe(model, pcm)
            if transcript:
                handler.text(transcript)
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("listener crashed")
        raise


if __name__ == "__main__":
    main()
