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


class InboxTest(unittest.TestCase):
    def test_appends_in_brain_format(self):
        with tempfile.TemporaryDirectory() as d:
            voice.append_inbox(d, "note", "first  thing")
            voice.append_inbox(d, "todo", "second")
            with open(os.path.join(d, "inbox.md"), encoding="utf-8") as f:
                lines = f.read().splitlines()
        self.assertEqual(lines[0], "# Inbox")
        self.assertRegex(lines[1], r"^- \d{4}-\d\d-\d\d \d\d:\d\d · note · first thing$")
        self.assertRegex(lines[2], r"· todo · second$")


class HandlerTest(unittest.TestCase):
    def test_bare_wake_then_request(self):
        h = voice.Handler(voice.load_config(None), dry_run=True)
        self.assertEqual(h.text("Hey Claude."), ("wake",))
        self.assertEqual(h.text("note pick up laundry"), ("capture", "note", "pick up laundry"))
        self.assertIsNone(h.text("nice shot"))  # the window closed after the request


if __name__ == "__main__":
    unittest.main()
