"""Bounded workspace metadata snapshots for troubleshooting, without file contents."""

import os
import time
from pathlib import Path

EXCLUDED = {'.git', '.venv', 'venv', 'node_modules', '__pycache__'}


def snapshot_workspace(root, max_entries=10000, max_seconds=2):
    root = Path(root)
    deadline = time.monotonic() + max_seconds
    files, pending = {}, [root]
    checked = errors = skipped_links = 0
    complete = True
    while pending and complete:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if checked >= max_entries or time.monotonic() >= deadline:
                        complete = False
                        break
                    checked += 1
                    try:
                        path = Path(entry.path)
                        if entry.is_symlink() or getattr(os.path, 'isjunction', lambda _: False)(entry.path):
                            skipped_links += 1
                        elif entry.is_dir(follow_symlinks=False):
                            if entry.name not in EXCLUDED:
                                pending.append(path)
                        elif entry.is_file(follow_symlinks=False):
                            stat = entry.stat(follow_symlinks=False)
                            files[path.relative_to(root).as_posix()] = (stat.st_size, stat.st_mtime_ns)
                    except OSError:
                        errors += 1
        except OSError:
            errors += 1
    return {'files': files, 'complete': complete and errors == 0,
            'entries_checked': checked, 'errors': errors, 'skipped_links': skipped_links}


def compare_workspace_snapshots(before, after, max_paths=50):
    old, new = before['files'], after['files']
    complete = before['complete'] and after['complete']
    changes = {
        'added': sorted(new.keys() - old.keys()) if complete else None,
        'removed': sorted(old.keys() - new.keys()) if complete else None,
        'modified': sorted(path for path in old.keys() & new.keys() if old[path] != new[path]),
    }
    result = {'complete': complete, 'excluded_directories': sorted(EXCLUDED),
              'before_entries_checked': before['entries_checked'],
              'after_entries_checked': after['entries_checked'],
              'errors': before['errors'] + after['errors'],
              'skipped_links': before['skipped_links'] + after['skipped_links']}
    for kind, paths in changes.items():
        result[kind] = None if paths is None else {'count': len(paths), 'paths': paths[:max_paths]}
    return result
