"""Read a bounded, task-specific log excerpt without loading the whole file."""
from collections import deque
import os
from pathlib import Path


def read_log_excerpt(path: Path, query: str = '', limit: int = 60):
    """Find the last matching entries, including two neighboring entries on each side.

    A snapshot of the current file is scanned. Very long lines are read in
    bounded segments; each returned segment is capped while retaining both ends.
    """
    term = query.casefold().strip()
    selected = deque(maxlen=limit)
    previous = deque(maxlen=2)
    last_emitted = 0
    total_selected = matches = number = after = bytes_read = 0

    def emit(item):
        nonlocal last_emitted, total_selected
        if item[0] <= last_emitted:
            return
        last_emitted = item[0]
        total_selected += 1
        selected.append({'number': item[0], 'text': item[1]})

    with path.open('rb') as stream:
        size = os.fstat(stream.fileno()).st_size
        while bytes_read < size:
            chunk = stream.readline(min(65536, size - bytes_read))
            if not chunk:
                break
            bytes_read += len(chunk)
            number += 1
            text = chunk.decode('utf-8', errors='replace').rstrip('\r\n')
            matched = not term or term in text.casefold()
            if len(text) > 4000:
                text = text[:2000] + f' [..{len(text) - 4000} characters omitted..] ' + text[-2000:]
            item = (number, text)
            if matched:
                matches += 1
                if term:
                    for entry in previous:
                        emit(entry)
                emit(item)
                after = 2 if term else 0
            elif after:
                emit(item)
                after -= 1
            previous.append(item)

    return {'query': query, 'entries': list(selected), 'matches': matches,
            'omitted_entries': max(0, total_selected - len(selected)),
            'bytes_scanned': bytes_read, 'snapshot_bytes': size,
            'note': 'Current task log only; rotated files are not included. Long lines may be split or shortened.'}
