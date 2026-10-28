#!/usr/bin/env python3
"""Voice controls for Claude Code: say "Hey Claude, <request>" and an agent starts without leaving your game.

Listens on the mic, cuts speech into utterances with WebRTC VAD, transcribes each one locally with
faster-whisper, and acts only on utterances that start with the wake phrase:
  "hey claude, note/idea/todo ..."  -> appended to a markdown inbox
  "hey claude, also ..."            -> continues the last voice agent
  "hey claude, <anything else>"     -> a new headless Claude Code agent (claude -p)
Agents run with no window, so nothing takes focus from your game; each one's short summary is read aloud when it
finishes.
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
PHRASE_GAP_MS = 300               # a pause this long starts a new phrase, which is checked for the wake phrase
MIN_VOICED = 12                   # phrases with less speech than this (~0.36 s) are not checked
STALL_S = 3                       # no mic audio (or only exact zeros) for this long reopens the mic
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # keep child processes from stealing focus on Windows
PID_FILE = os.path.join(HERE, "voice.pid")

DEFAULTS = {
    "input_device": None,          # index or name substring; None = system default mic
    "whisper_model": "base.en",
    "whisper_device": "cpu",       # cpu keeps the GPU free for the game
    "whisper_compute": "int8",
    "whisper_threads": 2,          # leave the other cores to the game
    "vad_aggressiveness": 3,       # 0-3, higher ignores more non-speech (2 hears a quiet room as speech)
    "silence_ms": 900,             # this much silence ends an utterance
    "max_utterance_s": 30,
    "follow_window_s": 8,          # after a bare "hey claude", how long to wait for the request
    "inbox": None,                 # markdown file for note/idea/todo; None = inbox.md next to this script
    "claude_cwd": None,            # folder agents start in; None = your home folder
    "permission_mode": "auto",     # agents can't ask you, so auto mode's checks approve or block each action
    "agent_timeout_s": 1800,
    "speak": True,
    "max_spoken_chars": 400,
}

VOICE_NOTE = ("This task was dictated by voice and transcribed automatically, so words may be misheard. If the request "
              "is ambiguous or looks like a transcription error, don't act on a guess: say what you need instead. "
              "Nobody is watching the screen, and your final reply is read aloud by text-to-speech, so make it one or "
              "two short plain sentences saying what you did, with no markdown, code, lists or URLs.")

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


def spoken(name):
    return name[len("voice: "):] if name.startswith("voice: ") else name


def claude_exe():
    exe = shutil.which("claude") or shutil.which(os.path.expanduser("~/.local/bin/claude"))  # pythonw at sign-in may lack PATH
    if not exe:
        raise RuntimeError("the claude command isn't installed or on PATH")
    return exe


class Agents:
    """Runs each voice request as a headless Claude Code agent (claude -p) with no window, several at once if you
    ask for several, and reads out each agent's short summary when it finishes."""

    def __init__(self, cfg, speaker):
        self.cfg, self.speaker = cfg, speaker
        self.last = None          # session id of the most recent voice agent, for "also ..."
        self.last_thread = None
        self.threads = []
        self.lock = threading.Lock()

    def submit(self, prompt, follow_up):
        previous = self.last_thread if follow_up else None
        t = threading.Thread(target=self._run, args=(prompt, follow_up, previous), daemon=True)
        self.threads.append(t)
        self.last_thread = t
        t.start()

    def wait(self):
        for t in list(self.threads):
            t.join()

    def _run(self, prompt, follow_up, previous):
        if previous:
            previous.join()  # "also ..." continues the last agent, so let it finish first
        try:
            session, reply = self.ask(prompt, self.last if follow_up else None)
            with self.lock:
                self.last = session or self.last
            log.info("agent %s finished: %s", session, reply)
            self.speaker.say(reply)
        except Exception as e:
            log.exception("agent failed: %s", prompt)
            chime(*ERROR_TONE)
            self.speaker.say("%s failed: %s" % (spoken(agent_name(prompt)), e))

    def ask(self, prompt, resume):
        """(session id, final reply) from one headless run; resume continues that session."""
        args = ["-p", "--output-format", "json", "--append-system-prompt", VOICE_NOTE, "--permission-prompts", "none"]
        if self.cfg["permission_mode"]:
            args += ["--permission-mode", self.cfg["permission_mode"]]
        if resume:
            args += ["--resume", resume]
        out = subprocess.run([claude_exe()] + args, input=prompt, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", cwd=self.cfg["claude_cwd"], timeout=self.cfg["agent_timeout_s"],
                             creationflags=NO_WINDOW)
        try:
            data = json.loads(out.stdout)
        except ValueError:
            raise RuntimeError((out.stderr or out.stdout or "no output").strip()[:200])
        if data.get("is_error"):
            raise RuntimeError(str(data.get("result") or data.get("subtype") or "unknown error")[:200])
        return data.get("session_id"), str(data.get("result") or "Done.").strip()


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


def mic_frames(cfg, open_stream=None):
    """Yields 30 ms frames of 16 kHz mono int16 PCM from the mic, forever. Some mics (a Bluetooth controller's
    headset mic, for one) stop delivering audio to a long-lived stream while a fresh one still works, so the stream
    is reopened whenever frames stop arriving for STALL_S or arrive as pure digital silence for that long."""
    if open_stream is None:
        import sounddevice as sd

        def open_stream(callback):
            return sd.RawInputStream(samplerate=RATE, blocksize=FRAME, channels=1, dtype="int16",
                                     device=resolve_device(cfg["input_device"]), callback=callback)
    zero = bytes(FRAME * 2)
    stall_frames = STALL_S * 1000 // FRAME_MS
    first = True
    while True:
        frames = queue.Queue()
        with open_stream(lambda indata, n, t, status: frames.put(bytes(indata))):
            if first:
                print("listening. Say \"hey claude, ...\" (Ctrl+C to stop)")
                first = False
            silent = 0
            while silent < stall_frames:
                try:
                    f = frames.get(timeout=STALL_S)
                except queue.Empty:
                    break
                silent = silent + 1 if f == zero else 0
                yield f
        log.warning("mic stopped sending audio; reopening it")


def phrases(frames, cfg):
    """Splits frames into phrases at short pauses: yields (pcm, voiced frames, ended). ended is True when the pause
    reached silence_ms (or the utterance hit max_utterance_s), closing the utterance the phrase belongs to."""
    import webrtcvad
    vad = webrtcvad.Vad(cfg["vad_aggressiveness"])
    pre = collections.deque(maxlen=10)    # 300 ms of audio kept from before speech starts
    recent = collections.deque(maxlen=10)
    gap_frames = PHRASE_GAP_MS // FRAME_MS
    end_frames = cfg["silence_ms"] // FRAME_MS
    max_frames = cfg["max_utterance_s"] * 1000 // FRAME_MS
    buf, silent, voiced, total = None, 0, 0, 0
    for f in frames:
        speech = vad.is_speech(f, RATE)
        if buf is None:
            pre.append(f)
            recent.append(speech)
            if sum(recent) >= 6:
                buf, silent, voiced, total = list(pre), 0, sum(recent), len(pre)
                recent.clear()
            continue
        buf.append(f)
        total += 1
        silent = 0 if speech else silent + 1
        voiced += speech
        if silent >= end_frames or total >= max_frames:
            yield b"".join(buf), voiced, True
            buf = None
            pre.clear()
        elif silent == gap_frames:
            yield b"".join(buf), voiced, False
            buf, voiced = [], 0


def listen(model, handler, stream):
    """Checks the start of each phrase for the wake phrase (cheap: at most GATE_S of audio), then transcribes the
    whole request, from that phrase to the end of the utterance, in one go."""
    request = []
    for pcm, voiced, ended in stream:
        if request:
            request.append(pcm)
        elif voiced >= MIN_VOICED and (handler.expecting() or
                                       parse_wake(transcribe(model, pcm[:RATE * 2 * GATE_S])) is not None):
            request = [pcm]
        if request and ended:
            transcript = transcribe(model, b"".join(request))
            request = []
            if transcript:
                handler.text(transcript)


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
        if not args.dry_run:  # let the agent finish and its summary be spoken before exiting
            handler.agents.wait()
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
        listen(model, handler, phrases(mic_frames(cfg), cfg))
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("listener crashed")
        raise


if __name__ == "__main__":
    main()
