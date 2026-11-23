'''Lightweight local task/LLM metrics for OpenManusWeb. No third-party dependencies.'''
from __future__ import annotations
import contextvars
import json
import math
import re
import sqlite3
import threading
import time
import statistics
from pathlib import Path
from urllib.request import urlopen

TASK_ID = contextvars.ContextVar('openmanus_gui_task_id', default=None)
CANCEL_EVENT = contextvars.ContextVar('openmanus_gui_cancel_event', default=None)
RUNS = {}
DB = Path(__file__).resolve().parents[2] / 'logs' / 'openmanus_agent_metrics.sqlite3'
OLLAMA_CACHE = {'at': 0.0, 'base_url': None, 'models': [], 'loaded': [],
                'loaded_details': [], 'loaded_error': None, 'observed_at': None, 'error': None}
OLLAMA_LOCK = threading.Lock()

def _conn():
    DB.parent.mkdir(exist_ok=True, parents=True)
    c = sqlite3.connect(str(DB), timeout=4)
    c.execute('''CREATE TABLE IF NOT EXISTS tasks (
        session_id TEXT PRIMARY KEY, model TEXT, status TEXT,
        started_at REAL, finished_at REAL, elapsed REAL, calls INTEGER,
        output_tokens INTEGER, estimated_tokens INTEGER, prompt_tokens INTEGER,
        llm_seconds REAL, error_count INTEGER)''')
    columns = {row[1] for row in c.execute('PRAGMA table_info(tasks)')}
    if 'complexity' not in columns:
        c.execute('ALTER TABLE tasks ADD COLUMN complexity TEXT')
    if 'task_type' not in columns:
        c.execute('ALTER TABLE tasks ADD COLUMN task_type TEXT')
    for name, column_type in [('input_reported_calls', 'INTEGER'), ('output_reported_calls', 'INTEGER'),
                              ('measured_output_seconds', 'REAL'), ('forecast_median', 'REAL'),
                              ('forecast_low', 'REAL'), ('forecast_high', 'REAL'), ('forecast_basis', 'TEXT')]:
        if name not in columns:
            c.execute(f'ALTER TABLE tasks ADD COLUMN {name} {column_type}')
    c.execute('''CREATE TABLE IF NOT EXISTS reports (
        session_id TEXT PRIMARY KEY, saved_at REAL NOT NULL, report TEXT NOT NULL)''')
    c.commit()
    return c

def save_report(sid, report):
    report = dict(report)
    report['checkpoint_at'] = time.time()
    c = _conn()
    try:
        with c:
            c.execute('INSERT OR REPLACE INTO reports VALUES (?, ?, ?)',
                      (sid, time.time(), json.dumps(report, ensure_ascii=False, default=str)))
    finally:
        c.close()

def load_report(sid):
    c = _conn()
    try:
        row = c.execute('SELECT report FROM reports WHERE session_id = ?', (sid,)).fetchone()
        if not row:
            return None
        report = json.loads(row[0])
        if report.get('status') == 'processing' and sid not in RUNS:
            report['status'] = 'interrupted'
            report['note'] = 'The server stopped before this task finished. This is the last saved checkpoint; the task was not resumed. ' + report.get('note', '')
            if report.get('metrics'):
                report['metrics']['status'] = 'interrupted'
        return report
    finally:
        c.close()

def recent_reports(limit=50, before_time=None, before_id=None, query=''):
    c = _conn()
    try:
        clauses, args = [], []
        if before_time is not None and before_id is not None:
            clauses.append('(saved_at < ? OR (saved_at = ? AND session_id < ?))')
            args.extend((before_time, before_time, before_id))
        if query.strip():
            term = query.strip().lower().replace('!', '!!').replace('%', '!%').replace('_', '!_')
            match = f'%{term}%'
            clauses.append('''(lower(session_id) LIKE ? ESCAPE '!'
                OR lower(coalesce(json_extract(report, '$.prompt'), '')) LIKE ? ESCAPE '!'
                OR lower(coalesce(json_extract(report, '$.workspace'), '')) LIKE ? ESCAPE '!'
                OR lower(coalesce(json_extract(report, '$.project_path'), '')) LIKE ? ESCAPE '!')''')
            args.extend((match, match, match, match))
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        rows = c.execute('SELECT session_id, saved_at, report FROM reports' + where +
                         ' ORDER BY saved_at DESC, session_id DESC LIMIT ?', (*args, limit)).fetchall()
        return [{'session_id': sid, 'saved_at': saved, 'status': ('interrupted' if (report := json.loads(data)).get('status') == 'processing' and sid not in RUNS else report.get('status')),
                 'prompt': (report.get('prompt') or '')[:120],
                 'workspace': report.get('project_path') or report.get('workspace') or ''} for sid, saved, data in rows]
    finally:
        c.close()

def begin_task(sid, model):
    RUNS[sid] = dict(session_id=sid, model=model, status='processing',
        started_at=time.time(), stage='queued', last_activity=time.time(),
        active_call=None, active_tool=None, calls=0, completed_calls=0, output_tokens=0,
        estimated_tokens=0, prompt_tokens=0, llm_seconds=0.0, errors=0, cancelled_calls=0,
        input_reported_calls=0, output_reported_calls=0, measured_output_seconds=0.0,
        tool_events=[])

def task_type(prompt):
    text = prompt.lower().strip()
    if re.fullmatch(r'(hi|hello|hey|thanks|thank you)[!.?\s]*', text):
        return 'greeting'
    if re.search(r'\b(code|software|program|script|debug|bug|function|repository|repo|python|javascript)\b', text):
        return 'coding'
    if re.search(r'\b(research|search|browse|find sources|look up)\b', text):
        return 'research'
    if re.search(r'\b(file|folder|document|spreadsheet|pdf|csv)\b', text):
        return 'file work'
    if text.endswith('?'):
        return 'question'
    return 'other'

def set_review(sid, complexity, review, prompt=''):
    run = RUNS[sid]
    run['complexity'] = complexity
    run['review'] = review
    run['task_type'] = task_type(prompt)
    run['estimate'] = estimate(run['model'], complexity, run['task_type'])

def estimate(model, complexity, task_kind='other'):
    c = _conn()
    try:
        rows = c.execute('''SELECT elapsed FROM tasks WHERE model = ? AND complexity = ? AND task_type = ?
            AND status = 'completed' AND elapsed > 0 ORDER BY started_at DESC LIMIT 30''',
            (model, complexity, task_kind)).fetchall()
        basis = 'same category'
        if len(rows) < 3:
            broader = c.execute('''SELECT elapsed FROM tasks WHERE model = ? AND complexity = ?
                AND status = 'completed' AND elapsed > 0 ORDER BY started_at DESC LIMIT 30''',
                (model, complexity)).fetchall()
            if len(broader) >= 5:
                rows = broader
                basis = 'all categories, same complexity'
        reference_rows = []
        if len(rows) < 3:
            reference_rows = c.execute('''SELECT elapsed FROM tasks WHERE model = ?
                AND status = 'completed' AND elapsed > 0 ORDER BY started_at DESC LIMIT 30''',
                (model,)).fetchall()
    finally:
        c.close()
    values = sorted(row[0] for row in rows)
    if len(values) < 3:
        result = {'sample_size': len(values), 'reason': 'At least 3 comparable completed tasks are needed.'}
        reference = sorted(row[0] for row in reference_rows)
        if len(reference) >= 2:
            result['reference'] = {'sample_size': len(reference),
                'low_seconds': round(reference[0]), 'high_seconds': round(reference[-1])}
        return result
    return {'sample_size': len(values), 'basis': basis, 'median_seconds': round(statistics.median(values)),
            'low_seconds': round(values[(len(values)-1)//4]),
            'high_seconds': round(values[math.ceil(3*(len(values)-1)/4)])}

def enter_task(sid, cancel_event=None):
    return TASK_ID.set(sid), CANCEL_EVENT.set(cancel_event)

def leave_task(token):
    task_token, cancel_token = token
    CANCEL_EVENT.reset(cancel_token)
    TASK_ID.reset(task_token)

def _tool_excerpt(value, limit=1600):
    text = '' if value is None else str(value)
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + f'\n[{len(text) - limit} characters omitted]\n' + text[-(limit - half):]

def start_tool(name, arguments=None):
    run = RUNS.get(TASK_ID.get())
    if run is None:
        return None
    tool = {'name': name, 'started_at': time.time(), 'arguments': _tool_excerpt(arguments),
            'state': 'running'}
    run['tool_events'].append(tool)
    del run['tool_events'][:-12]
    run['active_tool'] = tool
    run['stage'] = 'tool_processing'
    run['last_activity'] = tool['started_at']
    return tool

def end_tool(tool, result=None, state='returned'):
    run = RUNS.get(TASK_ID.get())
    if run is not None and run.get('active_tool') is tool and tool is not None:
        tool['elapsed'] = max(0, time.time() - tool['started_at'])
        tool['state'] = state
        tool['result'] = _tool_excerpt(result)
        run['active_tool'] = None
        run['stage'] = 'agent_work'
        run['last_activity'] = time.time()

def note_tool_error(message, trace=None):
    run = RUNS.get(TASK_ID.get())
    tool = run.get('active_tool') if run else None
    if tool is not None:
        tool['error'] = _tool_excerpt(message)
        if trace:
            tool['error_traceback'] = _tool_excerpt(trace, 4000)

def start_call(model):
    sid = TASK_ID.get()
    run = RUNS.get(sid)
    if not run:
        return None
    now = time.time()
    call = dict(started_at=now, model=model, received_chunks=0, chars=0,
                first_chunk_at=None, last_chunk_at=None)
    run['active_call'] = call
    run['stage'] = 'waiting_for_model'
    run['calls'] += 1
    run['last_activity'] = now
    return call

def chunk(call, piece):
    if call is None or not piece:
        return
    run = RUNS.get(TASK_ID.get())
    if not run:
        return
    now = time.time()
    call['chars'] += len(piece)
    call['received_chunks'] += 1
    call['first_chunk_at'] = call['first_chunk_at'] or now
    call['last_chunk_at'] = now
    run['stage'] = 'streaming_text'
    run['last_activity'] = now

def end_call(call, usage=None, output_text=''):
    if call is None:
        return
    run = RUNS.get(TASK_ID.get())
    if not run:
        return
    elapsed = max(0, time.time() - call['started_at'])
    output = getattr(usage, 'completion_tokens', None) if usage is not None else None
    input_count = getattr(usage, 'prompt_tokens', None) if usage is not None else None
    if isinstance(output, int) and output >= 0:
        run['output_tokens'] += output
        run['output_reported_calls'] += 1
        run['measured_output_seconds'] += elapsed
    elif call['chars'] or output_text:
        # Approximation only. Not model-tokenizer accurate.
        run['estimated_tokens'] += round((call['chars'] or len(output_text)) / 4)
    if isinstance(input_count, int) and input_count >= 0:
        run['prompt_tokens'] += input_count
        run['input_reported_calls'] += 1
    run['llm_seconds'] += elapsed
    run['completed_calls'] += 1
    run['active_call'] = None
    run['stage'] = 'agent_work'
    run['last_activity'] = time.time()

def fail_call(call, cancelled=False):
    if call is None:
        return
    run = RUNS.get(TASK_ID.get())
    if not run:
        return
    if cancelled:
        run['cancelled_calls'] += 1
    else:
        run['errors'] += 1
    run['active_call'] = None
    run['stage'] = 'llm_error_or_retry'
    run['last_activity'] = time.time()

def finish_task(sid, status):
    run = RUNS.get(sid)
    if run is None or (run['status'] != 'processing' and status != 'error'):
        return
    run['status'] = status
    run['stage'] = status
    run['finished_at'] = run.get('finished_at') or time.time()
    c = None
    try:
        c = _conn()
        with c:
            c.execute('''INSERT OR REPLACE INTO tasks
                (session_id, model, status, started_at, finished_at, elapsed, calls,
                 output_tokens, estimated_tokens, prompt_tokens, llm_seconds, error_count, complexity, task_type,
                 input_reported_calls, output_reported_calls, measured_output_seconds,
                 forecast_median, forecast_low, forecast_high, forecast_basis)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (sid, run['model'], status, run['started_at'], run['finished_at'],
                 run['finished_at'] - run['started_at'], run['calls'], run['output_tokens'],
                 run['estimated_tokens'], run['prompt_tokens'], run['llm_seconds'],
                 run['errors'], run.get('complexity'), run.get('task_type'),
                 run['input_reported_calls'], run['output_reported_calls'], run['measured_output_seconds'],
                 run.get('estimate', {}).get('median_seconds'), run.get('estimate', {}).get('low_seconds'),
                 run.get('estimate', {}).get('high_seconds'), run.get('estimate', {}).get('basis')))
    except Exception as e:
        print('Agent monitor: could not save task:', e)
    finally:
        if c is not None:
            c.close()

def snapshot(sid=None):
    if sid is None:
        processing = [r for r in RUNS.values() if r['status'] == 'processing']
        r = max(processing or RUNS.values(), key=lambda x:x['started_at'], default=None)
    else:
        r = RUNS.get(sid)
    if not r:
        return None
    now = time.time()
    call = r['active_call']
    copy = {k:v for k,v in r.items() if k not in ('active_call', 'active_tool')}
    copy['elapsed'] = max(0, (r.get('finished_at') or now) - r['started_at'])
    copy['active_call'] = None
    tool = r.get('active_tool')
    copy['active_tool'] = {'name': tool['name'], 'in_flight_seconds': round(now - tool['started_at'], 1)} if tool else None
    if call:
        copy['active_call'] = {
            'model': call['model'],
            'in_flight_seconds': round(now - call['started_at'], 1),
            'received_chunks': call['received_chunks'],
            'approx_streamed_tokens': round(call['chars'] / 4),
            'seconds_since_chunk': round(now - call['last_chunk_at'], 1) if call['last_chunk_at'] else None,
            'first_chunk_seconds': round(call['first_chunk_at'] - call['started_at'], 2) if call['first_chunk_at'] else None,
        }
    return copy

def history(limit=60):
    c = _conn()
    try:
        c.row_factory = sqlite3.Row
        rows = c.execute('''SELECT model, status, elapsed, calls, output_tokens,
                estimated_tokens, prompt_tokens, llm_seconds, started_at, error_count,
                input_reported_calls, output_reported_calls, measured_output_seconds,
                forecast_median, forecast_low, forecast_high, forecast_basis
                FROM tasks ORDER BY started_at DESC LIMIT ?''', (limit,)).fetchall()
        return [dict(x) for x in rows]
    finally:
        c.close()

def _get_json(url):
    with urlopen(url, timeout=3) as response:
        return json.load(response)

def cached_model_status(model, base_url):
    """Return the last observation without blocking a task on model discovery."""
    base = base_url.rstrip('/')
    if base.endswith('/v1'):
        base = base[:-3]
    cached = dict(OLLAMA_CACHE)
    if cached.get('base_url') != base or cached.get('error') or cached.get('loaded_error'):
        return {'state': 'unknown', 'observed_at': cached.get('observed_at') if cached.get('base_url') == base else None}
    info = next((x for x in cached.get('loaded_details', []) if x.get('name') == model), None)
    return {'state': 'loaded' if info else 'not loaded', 'observed_at': cached.get('observed_at'),
            'details': info}

def ollama_models(base_url, force=False):
    base = base_url.rstrip('/')
    if base.endswith('/v1'):
        base = base[:-3]
    with OLLAMA_LOCK:
        ttl = 3 if OLLAMA_CACHE['error'] else 12
        if not force and OLLAMA_CACHE['base_url'] == base and time.monotonic() - OLLAMA_CACHE['at'] < ttl:
            return dict(OLLAMA_CACHE)
        if not (base.startswith('http://') or base.startswith('https://')):
            result = dict(models=[], loaded=[], loaded_details=[], loaded_error=None, error='Not an HTTP Ollama endpoint')
        else:
            try:
                tags = _get_json(base + '/api/tags')
                model_names = sorted({x['name'] for x in tags.get('models', []) if x.get('name')})
                try:
                    running = _get_json(base + '/api/ps')
                    loaded = [x.get('name') or x.get('model') for x in running.get('models', [])]
                    loaded_details = [{'name': x.get('name') or x.get('model'),
                        'size_bytes': x.get('size'), 'vram_bytes': x.get('size_vram'),
                        'context_length': x.get('context_length'),
                        'quantization': (x.get('details') or {}).get('quantization_level')}
                        for x in running.get('models', [])]
                    loaded_error = None
                except Exception as exc:
                    loaded = []
                    loaded_details = []
                    loaded_error = type(exc).__name__
                result = dict(models=model_names, loaded=loaded, loaded_details=loaded_details,
                              loaded_error=loaded_error, error=None)
            except Exception as e:
                result = dict(models=[], loaded=[], loaded_details=[], loaded_error=None, error=str(e))
        OLLAMA_CACHE.update(result, base_url=base, at=time.monotonic(), observed_at=time.time())
        return dict(OLLAMA_CACHE)
