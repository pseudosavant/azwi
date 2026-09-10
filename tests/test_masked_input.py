from __future__ import annotations

from contextlib import redirect_stdout
import io
import unittest
from unittest.mock import patch

from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output

from azwi.onboarding import read_masked_pat


class Terminal(io.StringIO):
    def isatty(self):
        return True


class MaskedInputTests(unittest.TestCase):
    def run_prompt(self, keys, *, error=None):
        stderr, stdout = Terminal(), io.StringIO()
        output = Vt100_Output(stderr, lambda: Size(rows=24, columns=80), enable_cpr=False)
        with create_pipe_input() as input_stream:
            input_stream.send_text(keys)
            with patch("prompt_toolkit.input.create_input", return_value=input_stream), patch(
                "prompt_toolkit.output.create_output", return_value=output
            ), redirect_stdout(stdout):
                if error:
                    with self.assertRaises(error):
                        read_masked_pat(Terminal(), stderr)
                    result = None
                else:
                    result = read_masked_pat(Terminal(), stderr)
        self.assertEqual(stdout.getvalue(), "")
        return result, stderr.getvalue()

    def test_paste_and_backspace_show_asterisks_without_exposing_pat(self):
        secret = "disposable-PAT-probe"
        result, screen = self.run_prompt("\x1b[200~" + secret + "x\x1b[201~\x7f\r")
        self.assertEqual(result, secret)
        self.assertIn("*" * len(secret), screen)
        self.assertIn("PAT:", screen)
        self.assertNotIn(secret, screen)

    def test_cancel_and_eof_do_not_expose_input(self):
        for keys, error in [("disposable-probe\x03", KeyboardInterrupt), ("\x04", EOFError)]:
            with self.subTest(error=error):
                _, screen = self.run_prompt(keys, error=error)
                self.assertNotIn("disposable-probe", screen)

    def test_redirected_streams_cannot_fall_back_to_plaintext(self):
        for stdin, stderr in [(io.StringIO("secret\n"), Terminal()), (Terminal("secret\n"), io.StringIO())]:
            with self.subTest(stdin=type(stdin)), self.assertRaises(OSError):
                read_masked_pat(stdin, stderr)
            self.assertEqual(stdin.tell(), 0)
            self.assertEqual(stderr.getvalue(), "")
