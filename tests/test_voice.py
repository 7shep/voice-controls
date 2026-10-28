import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import voice  # noqa: E402


class WakeTest(unittest.TestCase):
    def test_request_after_wake(self):
        self.assertEqual(voice.parse_wake("Hey Claude, what's on my calendar?"), "what's on my calendar?")

    def test_common_mishearings(self):
        for heard in ("Hey Cloud, check the build.", "Okay Claude check the build", "hey, clawed. check the build"):
            self.assertEqual(voice.parse_wake(heard).rstrip("."), "check the build")

    def test_bare_wake(self):
        self.assertEqual(voice.parse_wake("Hey Claude."), "")

    def test_short_lead_in_allowed(self):
        self.assertEqual(voice.parse_wake("Uh, hey Claude, note buy milk"), "note buy milk")

    def test_not_addressed(self):
        self.assertIsNone(voice.parse_wake("push B, they're all rotating"))
        self.assertIsNone(voice.parse_wake("I was telling my friend yesterday about hey Claude and stuff"))
        self.assertIsNone(voice.parse_wake("Claude is pretty good at this"))


class RouteTest(unittest.TestCase):
    def test_captures(self):
        self.assertEqual(voice.route("note: buy milk"), ("capture", "note", "buy milk"))
        self.assertEqual(voice.route("take a note that the lab is due Friday"),
                         ("capture", "note", "the lab is due Friday"))
        self.assertEqual(voice.route("Idea, a voice-controlled planner."), ("capture", "idea", "a voice-controlled planner."))
        self.assertEqual(voice.route("to-do email the TA"), ("capture", "todo", "email the TA"))
        self.assertEqual(voice.route("remind me to stretch"), ("capture", "todo", "stretch"))

    def test_words_that_only_look_like_captures(self):
        self.assertEqual(voice.route("notes app sync is broken, look into it")[0], "claude")
        self.assertEqual(voice.route("note")[0], "claude")

    def test_follow_up(self):
        self.assertEqual(voice.route("also check the tests"), ("claude", "check the tests", True))
        self.assertEqual(voice.route("summarize today's inbox"), ("claude", "summarize today's inbox", False))


class AgentTest(unittest.TestCase):
    def test_name_from_first_words(self):
        self.assertEqual(voice.agent_name("check the build and fix any failing tests please."),
                         "voice: check the build and fix any")
        self.assertEqual(voice.spoken("voice: check the build"), "check the build")
        self.assertEqual(voice.spoken("Morning brief"), "Morning brief")


class InboxTest(unittest.TestCase):
    def test_appends_inbox_lines(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "inbox.md")
            voice.append_inbox(path, "note", "first  thing")
            voice.append_inbox(path, "todo", "second")
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
        self.assertEqual(lines[0], "# Inbox")
        self.assertRegex(lines[1], r"^- \d{4}-\d\d-\d\d \d\d:\d\d · note · first thing$")
        self.assertRegex(lines[2], r"· todo · second$")


class ConfigTest(unittest.TestCase):
    def test_defaults_need_nothing_else_installed(self):
        cfg = voice.load_config(None)
        self.assertEqual(cfg["inbox"], os.path.join(voice.HERE, "inbox.md"))
        self.assertEqual(cfg["claude_cwd"], os.path.expanduser("~"))

    def test_config_file_overrides(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"inbox": "~/notes/inbox.md", "claude_cwd": "%s"}' % d.replace("\\", "/"))
            cfg = voice.load_config(path)
        self.assertEqual(cfg["inbox"], os.path.expanduser("~/notes/inbox.md"))
        self.assertEqual(os.path.normpath(cfg["claude_cwd"]), os.path.normpath(d))


class HandlerTest(unittest.TestCase):
    def test_bare_wake_then_request(self):
        h = voice.Handler(voice.load_config(None), dry_run=True)
        self.assertEqual(h.text("Hey Claude."), ("wake",))
        self.assertEqual(h.text("note pick up laundry"), ("capture", "note", "pick up laundry"))
        self.assertIsNone(h.text("nice shot"))  # the window closed after the request


class MicTest(unittest.TestCase):
    def frames_from(self, streams, n):
        """The first n frames mic_frames yields when each opened stream delivers the next list of frames, then stops."""
        opened = []

        class Stream:
            def __init__(self, callback):
                self.callback = callback

            def __enter__(self):
                for f in streams[len(opened)]:
                    self.callback(f, voice.FRAME, None, None)
                opened.append(self)

            def __exit__(self, *exc):
                return False

        old = voice.STALL_S
        voice.STALL_S = 0.1  # 3 frames, and a 0.1 s wait for audio
        try:
            gen = voice.mic_frames(None, open_stream=Stream)
            return [next(gen) for _ in range(n)], len(opened)
        finally:
            voice.STALL_S = old

    def test_reopens_a_stream_that_stops_sending(self):
        a, b = b"\x01" * voice.FRAME * 2, b"\x02" * voice.FRAME * 2
        got, opened = self.frames_from([[a], [b]], 2)
        self.assertEqual((got, opened), ([a, b], 2))

    def test_reopens_a_stream_of_digital_silence(self):
        zero, b = bytes(voice.FRAME * 2), b"\x02" * voice.FRAME * 2
        got, opened = self.frames_from([[zero] * 10, [b]], 4)
        self.assertEqual((got[-1], opened), (b, 2))
        self.assertEqual(got[:3], [zero] * 3)


if __name__ == "__main__":
    unittest.main()
