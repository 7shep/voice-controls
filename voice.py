#!/usr/bin/env python3
"""Voice controls for Brain: say "Hey Claude, <request>" and it runs without leaving your game.

Listens on the mic, cuts speech into utterances with WebRTC VAD, transcribes each one locally with
faster-whisper, and acts only on utterances that start with the wake phrase:
  "hey claude, note/idea/todo ..."  -> appended to the Brain inbox (inbox.md)
  "hey claude, also ..."            -> continues the last voice conversation
  "hey claude, <anything else>"     -> claude -p in the background, reply spoken aloud
Saying just "hey claude" chimes and treats the next utterance (within a few seconds) as the request.

Run: python voice.py [--config config.json] [--list-devices] [--text "..."] [--wav file.wav] [--dry-run]
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
import subprocess
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
RATE = 16000
FRAME_MS = 30
FRAME = RATE * FRAME_MS // 1000  # samples per VAD frame
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # keep child processes from stealing focus on Windows

DEFAULTS = {
    "input_device": None,          # index or name substring; None = system default mic
    "whisper_model": "base.en",
    "whisper_device": "cpu",       # cpu keeps the GPU free for the game
    "whisper_compute": "int8",
    "vad_aggressiveness": 2,       # 0-3, higher ignores more non-speech
    "silence_ms": 900,             # this much silence ends an utterance
    "max_utterance_s": 30,
    "follow_window_s": 8,          # after a bare "hey claude", how long to wait for the request
    "notes": None,                 # Brain notes root; None = $BRAIN_NOTES or ../personal-notes
    "claude_cwd": None,            # None = the Brain checkout this folder lives in
    "allowed_tools": ["Read", "Glob", "Grep", "WebSearch", "WebFetch"],
    "claude_timeout_s": 600,
    "speak": True,
    "max_spoken_chars": 400,
}

SPOKEN = ("This request came by voice. The user cannot see the screen, so your final reply is read aloud "
          "by text-to-speech: keep it to one to three short plain sentences with no markdown, code, lists or URLs.")

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
    if not cfg["notes"]:
        cfg["notes"] = os.environ.get("BRAIN_NOTES") or os.path.join(os.path.dirname(HERE), "personal-notes")
    if not cfg["claude_cwd"]:
        cfg["claude_cwd"] = os.path.dirname(HERE)
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


def append_inbox(notes, kind, text):
    """Same line format as Brain's capture palette (see FORMATS.md, inbox.md)."""
    path = os.path.join(notes, "inbox.md")
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

class Claude:
    """Runs voice requests one at a time with claude -p, remembering the session for follow-ups."""

    def __init__(self, cfg, speaker):
        self.cfg, self.speaker, self.session, self.q = cfg, speaker, None, queue.Queue()
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, prompt, follow_up):
        self.q.put((prompt, follow_up))

    def _run(self):
        while True:
            prompt, follow_up = self.q.get()
            try:
                reply = self.ask(prompt, follow_up)
            except Exception as e:
                log.exception("claude failed")
                chime(*ERROR_TONE)
                reply = "Claude hit an error: %s" % e
            log.info("reply: %s", reply)
            self.speaker.say(reply)
            self.q.task_done()

    def ask(self, prompt, follow_up):
        exe = shutil.which("claude")
        if not exe:
            raise RuntimeError("the claude command isn't on PATH")
        cmd = [exe, "-p", "--output-format", "json", "--append-system-prompt", SPOKEN,
               "--allowedTools", ",".join(self.cfg["allowed_tools"])]
        if follow_up and self.session:
            cmd += ["--resume", self.session]
        out = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                             cwd=self.cfg["claude_cwd"], timeout=self.cfg["claude_timeout_s"], creationflags=NO_WINDOW)
        try:
            data = json.loads(out.stdout)
        except ValueError:
            raise RuntimeError((out.stderr or out.stdout or "no output").strip()[:200])
        self.session = data.get("session_id") or self.session
        if data.get("is_error"):
            raise RuntimeError(str(data.get("result") or data.get("subtype") or "unknown error")[:200])
        return str(data.get("result") or "Done.").strip()


class Handler:
    """Turns transcripts into actions; holds the "bare wake phrase, waiting for the request" state."""

    def __init__(self, cfg, dry_run=False):
        self.cfg, self.dry_run, self.awaiting = cfg, dry_run, 0.0
        self.speaker = Speaker(cfg["speak"] and not dry_run, cfg["max_spoken_chars"])
        self.claude = None if dry_run else Claude(cfg, self.speaker)

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
            append_inbox(self.cfg["notes"], action[1], action[2])
            self.speaker.say("Saved %s." % ("a " + action[1] if action[1] != "idea" else "an idea"))
        else:
            self.claude.submit(action[1], action[2])
        return action


# --- audio ----------------------------------------------------------------------------------

def load_whisper(cfg):
    from faster_whisper import WhisperModel
    print("loading whisper %s on %s..." % (cfg["whisper_model"], cfg["whisper_device"]))
    return WhisperModel(cfg["whisper_model"], device=cfg["whisper_device"], compute_type=cfg["whisper_compute"])


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


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--list-devices", action="store_true", help="print audio devices and exit")
    ap.add_argument("--text", help="handle one transcript instead of listening")
    ap.add_argument("--wav", help="transcribe and handle one 16 kHz mono WAV instead of listening")
    ap.add_argument("--dry-run", action="store_true", help="print what would happen without doing it")
    args = ap.parse_args()

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
        if not args.dry_run:  # let a background claude run and its reply finish before exiting
            handler.claude.q.join()
            handler.speaker.q.join()
        return

    model = load_whisper(cfg)
    try:
        for pcm in utterances(cfg):
            transcript = transcribe(model, pcm)
            if transcript:
                handler.text(transcript)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
