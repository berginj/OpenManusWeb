// UI panel for Ollama/OpenManus task telemetry; loaded beside existing GUI.
(() => {
  const $ = (id) => document.getElementById(id);
  const seconds = (value) => Number.isFinite(value) ? `${value.toFixed(1)}s` : '—';
  const duration = (value) => {
    if (!Number.isFinite(value)) return '—';
    const total = Math.round(Math.max(0, value));
    if (total < 60) return `${total}s`;
    const hours = Math.floor(total / 3600), minutes = Math.floor(total % 3600 / 60), secs = total % 60;
    return hours ? `${hours}h ${minutes}m` : `${minutes}m ${secs}s`;
  };
  const gib = value => Number.isFinite(value) && value >= 0 ? `${(value / 1073741824).toFixed(2)} GiB` : 'unknown';
  function modelLoadText(status) {
    if (!status) return '';
    const when = status.observed_at ? `; observed ${new Date(status.observed_at * 1000).toLocaleTimeString()}` : '';
    if (status.state !== 'loaded') return `Model load: ${status.state}${when}.`;
    const data = status.details || {};
    return `Model load: loaded; Ollama reports ${gib(data.vram_bytes)} VRAM / ${gib(data.size_bytes)} model memory; context ${data.context_length ?? 'unknown'}, quantization ${data.quantization || 'unknown'}${when}.`;
  }
  let currentReport = null;
  let verifiedModels = false;
  let modelListSignature = null;
  let lastModelNames = [];
  const safe = (value) => String(value ?? '—').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function conciseReport(report) {
    const cut = (value, max = 2000) => {
      const text = String(value ?? 'Not available');
      return text.length > max ? text.slice(0, max) + `\n[${text.length - max} characters omitted; see recorded data]` : text;
    };
    const cutMiddle = (value, max = 1000) => {
      const text = String(value ?? 'Not available');
      if (text.length <= max) return text;
      const half = Math.floor(max / 2);
      return text.slice(0, half) + `\n[${text.length - max} characters omitted; see recorded data]\n` + text.slice(-(max - half));
    };
    const m = report.metrics || {}, p = report.progress || {}, call = m.active_call;
    const lines = ['OpenManus troubleshooting report', `Task ID: ${report.session_id}`,
      `Status: ${report.status}`, `Workspace: ${report.project_path || report.workspace || 'Not available'}`,
      `Execution requested: ${report.requested_mode || 'auto'}; route: ${m.complexity || 'not yet reviewed'}`,
      `Model: ${m.model || 'Not available'}`, `Elapsed: ${seconds(m.elapsed)}`,
      `Current step: ${p.current_step || m.stage || 'Not available'}`,
      `Model calls: ${m.completed_calls ?? 0} completed / ${m.calls ?? 0} started; ${m.errors ?? 0} errors or retries, ${m.cancelled_calls ?? 0} canceled`,
      `Input tokens: ${m.prompt_tokens ?? 0} reported (${m.input_reported_calls ?? 0} calls); output: ${m.output_tokens ?? 0} reported, ${m.estimated_tokens ?? 0} estimated (${m.output_reported_calls ?? 0} calls reported)`,
      'Token totals may be incomplete, especially for failed or in-flight calls.'];
    if (call) lines.push(`Active model request: ${seconds(call.in_flight_seconds)} in flight; ${call.received_chunks} chunks received.`);
    if (m.review) lines.push(`Task review: ${cut(m.review, 1000)}`);
    if (m.active_tool) lines.push(`Active tool: ${m.active_tool.name}, running for ${seconds(m.active_tool.in_flight_seconds)}.`);
    if (report.runtime_environment) {
      const env = report.runtime_environment;
      lines.push(`Endpoint: ${env.endpoint_origin} (${env.api_type || 'default API'}); output limit: ${env.max_output_tokens}, temperature: ${env.temperature}, tool-call timeout: ${env.request_timeout_seconds}s.`,
        `Runtime: Python ${env.python_version}, ${env.platform} ${env.platform_release}.`);
    }
    if (report.model_load) lines.push(modelLoadText(report.model_load));
    if (m.estimate?.median_seconds != null) lines.push(`Historical estimate: ${duration(m.estimate.low_seconds)}–${duration(m.estimate.high_seconds)} total (${m.estimate.sample_size} runs; ${m.estimate.basis}).`);
    else if (m.estimate?.reference) lines.push(`Historical reference only: ${m.estimate.reference.sample_size} completed runs of this model took ${duration(m.estimate.reference.low_seconds)}–${duration(m.estimate.reference.high_seconds)}; tasks may not be comparable.`);
    if (report.status === 'interrupted') {
      if (report.checkpoint_at) lines.push(`Last checkpoint: ${new Date(report.checkpoint_at * 1000).toISOString()}`);
      lines.push('The server stopped before completion; the task was not resumed.');
    }
    lines.push('', 'REQUEST', cut(report.prompt, 4000), '', 'RESULT / ERROR', cut(report.result, 4000));
    if (report.error_traceback) {
      const trace = String(report.error_traceback);
      lines.push('', `TRACEBACK (${report.error_phase || 'task error'})`,
        trace.length > 5000 ? '[Earlier traceback omitted; see recorded data]\n' + trace.slice(-5000) : trace);
    }
    if (report.workspace_changes) {
      const changes = report.workspace_changes;
      lines.push('', 'OBSERVED WORKSPACE METADATA CHANGES');
      if (changes.error) lines.push(`Scan failed: ${cut(changes.error, 500)}`);
      else {
        if (!changes.complete) lines.push('Scan incomplete; added and removed paths could not be determined.');
        for (const kind of ['added', 'modified', 'removed']) {
          const group = changes[kind];
          if (!group) continue;
          lines.push(`${kind}: ${group.count}${group.count > group.paths.length ? ` (showing ${group.paths.length})` : ''}`,
            ...group.paths.map(path => `- ${cut(path, 500)}`));
        }
        lines.push('Metadata comparison can miss changes and include changes made outside this task. Scans are bounded; links and common dependency directories are excluded.');
      }
    }
    if (report.context_exchanges?.length) {
      lines.push('', 'PREVIOUS CONTEXT INCLUDED', ...report.context_exchanges.map(x =>
        `- ${x.session_id} (${x.status}): ${cut(x.request, 500)}\n  Result: ${cut(x.result, 1000)}`));
    }
    const steps = (report.recent_steps || []).filter(s => s.type !== 'communication').slice(-8);
    if (steps.length) lines.push('', 'RECENT STEPS', ...steps.map(s => `- ${cut(s.message, 500)}`));
    const tools = (m.tool_events || []).slice(-3);
    if (tools.length) {
      lines.push('', 'RECENT TOOL CALLS (excerpts)');
      for (const tool of tools) {
        lines.push(`- ${tool.name} (${tool.state}${Number.isFinite(tool.elapsed) ? `, ${seconds(tool.elapsed)}` : ''})`,
          `  Input: ${cut(tool.arguments || '(none)', 1000)}`,
          `  Output: ${tool.state === 'running' ? '(still running)' : cut(tool.result || '(none)', 1000)}`);
        if (tool.error) lines.push(`  Tool reported error: ${cutMiddle(tool.error, 1000)}`);
        if (tool.error_traceback) {
          const trace = String(tool.error_traceback);
          lines.push('  Tool traceback:', trace.length > 2500 ? '[Earlier traceback omitted; see recorded data]\n' + trace.slice(-2500) : trace);
        }
      }
    }
    const important = report.important_logs || (report.recent_session_logs || []).filter(l => ['WARNING','ERROR','CRITICAL'].includes(l.level));
    if (important.length) lines.push('', 'WARNINGS AND ERRORS', ...important.slice(-6).map(l => `- ${l.level || 'LOG'}: ${cut(l.message, 1000)}`));
    lines.push('', 'Review for sensitive information before sharing. Use Show recorded data for more events and metrics.');
    return lines.join('\n');
  }
  function renderHistory(rows) {
    const target = $('monitor-history');
    if (!target) return;
    // Quick aggregated model comparison; equivalent tasks are still required for fair benchmarks.
    const groups = {};
    for (const r of rows) {
      const g = groups[r.model] ||= {n:0, completed:0, elapsed:0, output:0, seconds:0, rates:[], input:0, reported:0, estimated:0, errors:[], inRange:0};
      g.n++; if (r.status === 'completed') g.completed++;
      g.elapsed += r.elapsed || 0;
      g.input += r.prompt_tokens || 0;
      g.reported += r.output_tokens || 0;
      g.estimated += r.estimated_tokens || 0;
      if (r.status === 'completed' && r.elapsed > 0 && r.forecast_median != null && r.forecast_low != null && r.forecast_high != null) {
        g.errors.push(Math.abs(r.forecast_median - r.elapsed) / r.elapsed);
        if (r.elapsed >= r.forecast_low && r.elapsed <= r.forecast_high) g.inRange++;
      }
      if ((r.measured_output_seconds || 0) > 0) {
        g.output += r.output_tokens || 0;
        g.seconds += r.measured_output_seconds;
        if (r.status === 'completed' && (r.output_tokens || 0) > 0) g.rates.push(r.output_tokens / r.measured_output_seconds);
      }
    }
    const keys = Object.keys(groups);
    target.innerHTML = keys.length ? keys.map(name => {
      const g = groups[name];
      const tps = g.output && g.seconds ? (g.output / g.seconds).toFixed(1) + ' tok/s*' : 'n/a';
      let trend = 'Need 6 measured tasks';
      if (g.rates.length >= 6) {
        const avg = a => a.reduce((x,y)=>x+y,0)/a.length;
        const recent = avg(g.rates.slice(0,3));
        const previous = avg(g.rates.slice(3,6));
        trend = recent < previous*0.8 ? 'Slower recently' : recent > previous*1.2 ? 'Faster recently' : 'Roughly steady';
      }
      const calibration = g.errors.length ? `${g.inRange}/${g.errors.length} in range` : 'No forecasts checked';
      return `<tr><td>${safe(name)}</td><td>${g.n}</td><td>${Math.round(100*g.completed/g.n)}%</td><td>${seconds(g.elapsed/g.n)}</td><td>${g.input.toLocaleString()}</td><td>${g.reported.toLocaleString()} + ${g.estimated.toLocaleString()} est.</td><td>${tps}</td><td>${trend}</td><td>${calibration}</td></tr>`;
    }).join('') : '<tr><td colspan="9">No task history yet</td></tr>';
  }
  async function update() {
    try {
      const sid = window.app?.sessionId || '';
      const res = await fetch('/api/agent-monitor?session_id=' + encodeURIComponent(sid), {cache:'no-store'});
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const d = await res.json();
      const select = $('monitor-model');
      const active = window.app?.isProcessing || false;
      if (!select.options.length) select.add(new Option(d.default_model, d.default_model));
      if (!d.error && !active) {
        const names = [...new Set(d.models)].sort();
        const signature = JSON.stringify(names);
        if (!verifiedModels || signature !== modelListSignature) {
        const chosen = select.value;
        const preserveMissing = verifiedModels && chosen && !names.includes(chosen) &&
          (lastModelNames.includes(chosen) || select.selectedOptions[0]?.dataset.unavailable === 'true');
        select.innerHTML = '';
        if (preserveMissing) {
          const missing = new Option(`${chosen} (no longer installed)`, chosen);
          missing.disabled = true;
          missing.dataset.unavailable = 'true';
          select.add(missing);
        }
        for (const name of names) {
          const o = document.createElement('option'); o.value = name; o.textContent = name;
          select.appendChild(o);
        }
        if (!select.options.length) {
          const unavailable = new Option(`${d.default_model} (not installed)`, d.default_model);
          unavailable.disabled = true;
          select.add(unavailable);
        }
        select.value = names.includes(chosen) || preserveMissing ? chosen :
          (names.includes(d.default_model) ? d.default_model : names[0] || d.default_model);
        modelListSignature = signature;
        lastModelNames = names;
        verifiedModels = true;
        }
      }
      select.disabled = active;
      $('execution-mode').disabled = active;
      const run = d.run;
      $('monitor-backend').textContent = d.error ? `Ollama not reachable: ${d.error}` :
        `Ollama online · ${d.loaded.length} loaded · ${d.models.length} installed`;
      $('monitor-active-model').textContent = run?.model || select.value || d.default_model;
      $('monitor-calls').textContent = run ? `${run.completed_calls} / ${run.calls}` : '0 / 0';
      $('monitor-output').textContent = run && run.completed_calls ?
        `${run.output_reported_calls ? (run.output_tokens || 0).toLocaleString() + ' reported' : 'not reported'} · ${(run.estimated_tokens || 0).toLocaleString()} est. (${run.output_reported_calls ?? 0}/${run.completed_calls} calls reported)` : '—';
      $('monitor-input').textContent = run && run.completed_calls ?
        `${run.input_reported_calls ? (run.prompt_tokens || 0).toLocaleString() : 'not reported'} (${run.input_reported_calls ?? 0}/${run.completed_calls} calls reported)` : '—';
      $('monitor-elapsed').textContent = run ? seconds(run.elapsed) : '—';
      const estimate = run?.estimate;
      $('monitor-estimate').textContent = !run ? 'Task review and estimate will appear here.' :
        !estimate ? 'Reviewing task scope; estimate pending.' :
        estimate.median_seconds == null ? `${run.complexity} · ${run.task_type} · ${estimate.reason} (${estimate.sample_size} comparable).${estimate.reference ? ` Historical reference only: ${estimate.reference.sample_size} completed runs of this model took ${duration(estimate.reference.low_seconds)}–${duration(estimate.reference.high_seconds)}; tasks may not be comparable.` : ''}` :
        `${run.complexity} · ${run.task_type} · Typical total ${duration(estimate.median_seconds)}; historical range ${duration(estimate.low_seconds)}–${duration(estimate.high_seconds)} (${estimate.sample_size} runs: ${estimate.basis}, same model). ${run.status === 'processing' && run.elapsed > estimate.high_seconds ? 'Past the historical range; completion time is unknown.' : 'Actual time may vary.'}`;
      let state = 'Idle'; let detail = 'Ready for a new task';
      if (run && run.status === 'processing') {
        if (run.active_call) {
          if (run.active_call.received_chunks) {
            state = 'Streaming text';
            detail = `${run.active_call.approx_streamed_tokens} estimated tokens received in ${seconds(run.active_call.in_flight_seconds)}; last chunk ${seconds(run.active_call.seconds_since_chunk)} ago`;
          } else {
            const age = run.active_call.in_flight_seconds;
            state = age > 120 ? 'Long-running inference (check)' : 'Waiting for model';
            detail = `Model request in flight for ${seconds(age)}. No token stream available for this call; it may still be generating.`;
          }
        } else if (run.active_tool) {
          state = 'Running tool';
          detail = `${run.active_tool.name} has been running for ${seconds(run.active_tool.in_flight_seconds)}.`;
        } else {
          state = 'Agent/tool processing';
          const lag = (Date.now() / 1000) - (run.last_activity || 0);
          detail = `${seconds(lag)} since last recorded agent/LLM event. This is not a stall detector for tools.`;
        }
      } else if (run) {
        state = run.status.toUpperCase();
        detail = `Task ran for ${seconds(run.elapsed)}; ${run.completed_calls} model calls completed.`;
      }
      $('monitor-state').textContent = state;
      $('monitor-detail').textContent = detail;
      const modelName = run?.model || select.value;
      const load = (d.loaded_details || []).find(item => item.name === modelName);
      const loadState = d.error || d.loaded_error ? 'unknown' : load ? 'loaded' : 'not loaded';
      $('monitor-placement').textContent = modelLoadText({state: loadState, details: load, observed_at: d.observed_at});
      const progress = d.progress;
      $('monitor-progress').textContent = progress?.total_steps > 0 ?
        `Plan: ${progress.completed_steps}/${progress.total_steps} steps complete · ${progress.current_step}` :
        (run && progress ? progress.current_step : '');
      renderHistory(d.history || []);
    } catch (e) {
      $('monitor-state').textContent = 'Monitor unavailable';
      $('monitor-detail').textContent = e.message;
    }
  }
  document.addEventListener('DOMContentLoaded', () => {
    async function openReport(sid) {
      if (!sid) { $('diagnostics-status').textContent = 'Start a task first.'; return; }
      try {
        $('diagnostics-status').textContent = 'Collecting task details...';
        const response = await fetch('/api/chat/' + encodeURIComponent(sid) + '/diagnostics?refresh_model=true', {cache:'no-store'});
        if (!response.ok) throw new Error('HTTP ' + response.status);
        currentReport = await response.json();
        $('diagnostics-text').value = conciseReport(currentReport);
        $('diagnostics-panel').hidden = false;
        $('diagnostics-status').textContent = 'Report ready to review and copy.';
      } catch (e) { $('diagnostics-status').textContent = 'Could not prepare report: ' + e.message; }
    }
    let reports = [], reportVersion = 0, reportLoading = false, reportExpanded = false;
    let reportCursor = null, searchTimer = null;
    const pageSize = 50;
    async function loadReports(reset = true) {
      if (!reset && (reportLoading || !reportCursor)) return;
      const version = reset ? ++reportVersion : reportVersion;
      const cursor = reset ? null : reportCursor;
      if (reset) reportCursor = null;
      reportLoading = true;
      $('diagnostics-more').disabled = true;
      $('reports-info').textContent = 'Loading tasks...';
      try {
        const params = new URLSearchParams({limit: String(pageSize + 1), q: $('diagnostics-search').value.trim()});
        if (cursor) { params.set('before_time', cursor.saved_at); params.set('before_id', cursor.session_id); }
        const res = await fetch('/api/reports?' + params, {cache:'no-store'});
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        if (version !== reportVersion) return;
        const page = data.slice(0, pageSize);
        if (reset) { reports = page; reportExpanded = false; }
        else {
          const seen = new Set(reports.map(item => item.session_id));
          reports.push(...page.filter(item => !seen.has(item.session_id)));
          reportExpanded = true;
        }
        reportCursor = data.length > pageSize && page.length ? page[page.length - 1] : null;
        const select = $('diagnostics-history'), selected = select.value;
        select.replaceChildren(new Option('Select a previous task', ''));
        for (const report of reports) {
          select.add(new Option(`${new Date(report.saved_at * 1000).toLocaleString()} · ${report.status} · ${report.workspace || 'workspace unavailable'} · ${report.prompt}`, report.session_id));
        }
        select.value = reports.some(item => item.session_id === selected) ? selected : '';
        $('reports-info').textContent = `${reports.length} task${reports.length === 1 ? '' : 's'} shown${reportCursor ? '; more available' : ''}.`;
      } catch (_) {
        if (version === reportVersion) $('reports-info').textContent = 'Saved tasks unavailable.';
      } finally {
        if (version === reportVersion) {
          reportLoading = false;
          $('diagnostics-more').disabled = !reportCursor;
        }
      }
    }
    $('diagnostics-search').addEventListener('input', () => {
      clearTimeout(searchTimer);
      ++reportVersion;
      reportLoading = false;
      $('diagnostics-more').disabled = true;
      $('reports-info').textContent = 'Searching...';
      searchTimer = setTimeout(() => loadReports(true), 250);
    });
    $('diagnostics-refresh').addEventListener('click', () => loadReports(true));
    $('diagnostics-more').addEventListener('click', () => loadReports(false));
    $('diagnostics-button').addEventListener('click', () => openReport(window.app?.sessionId));
    $('diagnostics-open').addEventListener('click', () => openReport($('diagnostics-history').value));
    $('diagnostics-resume').addEventListener('click', async () => {
      const sid = $('diagnostics-history').value;
      if (!sid) { $('diagnostics-status').textContent = 'Select a saved task first.'; return; }
      if (window.app?.isProcessing && window.app.sessionId !== sid) {
        $('diagnostics-status').textContent = 'Finish or stop the current task before switching.'; return;
      }
      $('diagnostics-resume').disabled = true;
      try {
        const restored = await window.app?.restoreSession(sid);
        $('diagnostics-status').textContent = restored ? 'Task opened in chat.' : 'Could not open task in chat.';
      } finally { $('diagnostics-resume').disabled = false; }
    });
    $('diagnostics-summary').addEventListener('click', () => { if (currentReport) $('diagnostics-text').value = conciseReport(currentReport); });
    $('diagnostics-full').addEventListener('click', () => { if (currentReport) $('diagnostics-text').value = 'OpenManus task diagnostics\n' + JSON.stringify(currentReport, null, 2); });
    $('diagnostics-log-append').addEventListener('click', async () => {
      const report = currentReport;
      if (!report) { $('diagnostics-status').textContent = 'Prepare a task report first.'; return; }
      const button = $('diagnostics-log-append');
      button.disabled = true;
      $('diagnostics-status').textContent = 'Searching the task log...';
      try {
        const response = await fetch('/api/chat/' + encodeURIComponent(report.session_id) + '/log-excerpt', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({query: $('diagnostics-log-query').value, limit: 60})
        });
        if (!response.ok) {
          const detail = await response.json().catch(() => ({}));
          throw new Error(detail.detail || 'HTTP ' + response.status);
        }
        const excerpt = await response.json();
        if (currentReport !== report) { $('diagnostics-status').textContent = 'Report changed; no log excerpt was added.'; return; }
        if (!excerpt.entries.length) { $('diagnostics-status').textContent = 'No matching entries in the current task log.'; return; }
        const title = excerpt.query.trim() ? `matches for ${excerpt.query}` : 'recent entries';
        const header = `\n\nTASK LOG EXCERPT (${title}; ${excerpt.matches} matches, ${excerpt.omitted_entries} earlier entries omitted)\n${excerpt.note}`;
        $('diagnostics-text').value += header + '\n' + excerpt.entries.map(entry => `${entry.number}: ${entry.text}`).join('\n');
        $('diagnostics-status').textContent = `Added ${excerpt.entries.length} log entries. Review before copying.`;
      } catch (e) { $('diagnostics-status').textContent = 'Could not add log excerpt: ' + e.message; }
      finally { button.disabled = false; }
    });
    $('diagnostics-copy').addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText($('diagnostics-text').value);
        $('diagnostics-status').textContent = 'Copied.';
      } catch (_) {
        $('diagnostics-text').focus(); $('diagnostics-text').select();
        $('diagnostics-status').textContent = 'Select and copy the report above.';
      }
    });
    loadReports();
    setInterval(() => {
      if (!reportLoading && !reportExpanded && !$('diagnostics-history').value &&
          document.activeElement !== $('diagnostics-history') && document.activeElement !== $('diagnostics-search')) loadReports(true);
    }, 15000);
    update(); setInterval(update, 2000);
  });
})();
