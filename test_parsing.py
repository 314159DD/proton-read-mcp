"""Tiny unit tests for the pure parsing helpers (no live Bridge needed).

Run: python -m unittest test_parsing
These cover the realistic failure points against live Bridge FETCH/LIST output.
"""

import unittest

import server


class FlagsTests(unittest.TestCase):
    def test_flags_in_preamble(self):
        blob = b"1 (UID 5 FLAGS (\\Seen \\Answered) BODY[HEADER] {12}"
        self.assertEqual(server._flags_from(blob), b"\\Seen \\Answered")

    def test_seen_detection_case_insensitive(self):
        self.assertTrue(server._is_seen(b"\\Seen"))
        self.assertTrue(server._is_seen(b"\\seen \\flagged"))
        self.assertFalse(server._is_seen(b"\\Answered \\Flagged"))

    def test_no_flags(self):
        self.assertEqual(server._flags_from(b"1 (UID 5 BODY[] {3}"), b"")
        self.assertFalse(server._is_seen(b""))


class FolderNameTests(unittest.TestCase):
    def test_quoted_name(self):
        self.assertEqual(server._folder_name(b'(\\HasNoChildren) "/" "INBOX"'), "INBOX")

    def test_name_with_spaces(self):
        self.assertEqual(
            server._folder_name(b'(\\HasNoChildren) "/" "Folders/Recruiter Replies"'),
            "Folders/Recruiter Replies",
        )

    def test_escaped_quote(self):
        self.assertEqual(server._folder_name(b'(\\Noselect) "/" "Weird \\"name\\""'), 'Weird "name"')

    def test_unquoted_atom_fallback(self):
        self.assertEqual(server._folder_name(b"(\\HasNoChildren) / INBOX"), "INBOX")


class HtmlTests(unittest.TestCase):
    def test_strip_basic(self):
        out = server._strip_html("<p>Hello&nbsp;<b>world</b></p><p>Next</p>")
        self.assertIn("Hello world", out)
        self.assertIn("Next", out)
        self.assertNotIn("<", out)

    def test_br_becomes_newline(self):
        out = server._strip_html("line one<br>line two")
        self.assertEqual(out.splitlines(), ["line one", "line two"])

    def test_script_removed(self):
        out = server._strip_html("<style>.x{}</style><script>alert(1)</script>text")
        self.assertEqual(out, "text")


class ExtractBodyTests(unittest.TestCase):
    def test_multipart_alternative_prefers_plain(self):
        import email
        raw = (
            b"MIME-Version: 1.0\r\n"
            b'Content-Type: multipart/alternative; boundary="b"\r\n\r\n'
            b"--b\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
            b"plain body\r\n"
            b"--b\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
            b"<p>html body</p>\r\n--b--\r\n"
        )
        msg = email.message_from_bytes(raw)
        body, attachments = server._extract_body(msg)
        self.assertEqual(body, "plain body")
        self.assertEqual(attachments, [])

    def test_html_only_falls_back_to_stripped(self):
        import email
        raw = (
            b"MIME-Version: 1.0\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
            b"<p>only&nbsp;html</p>"
        )
        msg = email.message_from_bytes(raw)
        body, _ = server._extract_body(msg)
        self.assertEqual(body, "only html")


if __name__ == "__main__":
    unittest.main()
