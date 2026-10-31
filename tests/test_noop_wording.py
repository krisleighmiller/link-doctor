"""Presentation-only regression; native semantics are tested separately."""
from contextlib import redirect_stdout
import io
import unittest
from unittest.mock import patch
from link_doctor.__main__ import main


class NoopWordingTests(unittest.TestCase):
    def render(self, status):
        report = {'status': status, 'command': ['ld.bfd', 'main.o'],
                  'repair': {'kind': 'reorder'}, 'verified': False,
                  'verification': {'status': 'not_requested'}}
        output = io.StringIO()
        with patch('sys.argv', ['link-doctor', 'diagnose', '--', 'ld.bfd', 'main.o']), \
             patch('link_doctor.__main__.diagnose', return_value=report), redirect_stdout(output):
            self.assertEqual(main(), 0)
        return output.getvalue()

    def test_satisfied_command_does_not_claim_reorder(self):
        output = self.render('already_satisfied')
        self.assertIn('No repair needed: original command satisfies the supported model.', output)
        self.assertNotIn('Repair strategy:', output)

    def test_candidate_keeps_strategy(self):
        self.assertIn('Repair strategy: reorder', self.render('candidate'))
