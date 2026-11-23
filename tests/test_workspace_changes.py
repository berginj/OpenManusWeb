import os
import tempfile
import unittest
from pathlib import Path

from app.web.workspace_changes import compare_workspace_snapshots, snapshot_workspace


class WorkspaceChangesTests(unittest.TestCase):
    def test_nested_changes_and_exclusions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'nested').mkdir()
            (root / 'nested' / 'changed').write_text('before')
            (root / 'nested' / 'removed').write_text('secret contents')
            (root / '.git').mkdir()
            (root / '.git' / 'ignored').write_text('ignored')
            before = snapshot_workspace(root)
            (root / 'nested' / 'changed').write_text('longer after')
            (root / 'nested' / 'removed').unlink()
            (root / 'nested' / 'added').write_text('private content')
            after = snapshot_workspace(root)
            result = compare_workspace_snapshots(before, after)
            self.assertTrue(result['complete'])
            self.assertEqual(result['added']['paths'], ['nested/added'])
            self.assertEqual(result['removed']['paths'], ['nested/removed'])
            self.assertEqual(result['modified']['paths'], ['nested/changed'])
            self.assertNotIn('private content', str(result))
            self.assertNotIn('.git/ignored', str(result))

    def test_incomplete_scan_does_not_claim_additions_or_removals(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'one').write_text('one')
            (root / 'two').write_text('two')
            before = snapshot_workspace(root, max_entries=1)
            after = snapshot_workspace(root)
            result = compare_workspace_snapshots(before, after)
            self.assertFalse(result['complete'])
            self.assertIsNone(result['added'])
            self.assertIsNone(result['removed'])

    def test_path_limit_preserves_total(self):
        before = {'files': {}, 'complete': True, 'entries_checked': 0, 'errors': 0, 'skipped_links': 0}
        after = dict(before, files={'a': (1, 1), 'b': (2, 2)})
        result = compare_workspace_snapshots(before, after, max_paths=1)
        self.assertEqual(result['added'], {'count': 2, 'paths': ['a']})

    def test_symlink_is_not_followed(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as outside:
            root = Path(temp)
            (Path(outside) / 'private').write_text('secret')
            try:
                os.symlink(outside, root / 'link', target_is_directory=True)
            except OSError:
                self.skipTest('Creating symlinks is unavailable')
            result = snapshot_workspace(root)
            self.assertFalse(result['files'])
            self.assertEqual(result['skipped_links'], 1)
