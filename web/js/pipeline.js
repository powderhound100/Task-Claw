// Task-Claw Pipeline Monitor

const PIPELINE_STAGES = ['rewrite', 'plan', 'code', 'simplify', 'test', 'review', 'publish'];
let currentConfigTab = 'pipeline';
let pipelineConfig = null;
let providersConfig = null;
let statusPollTimer = null;

// -- Agent Status + Pipeline Monitor --

async function fetchStatus() {
    try {
        const data = await api('/status');
        renderAgentStatus(data);
        renderStageFlow(data);
        return data;
    } catch (e) {
        console.warn('Status fetch failed:', e);
        return null;
    }
}

function renderAgentStatus(data) {
    const stateEl = document.getElementById('pAgentState');
    const taskEl = document.getElementById('pCurrentTask');
    const apiEl = document.getElementById('pApiCalls');
    const lastRunEl = document.getElementById('pLastRun');
    const triggerEl = document.getElementById('pLastTrigger');
    const stageEl = document.getElementById('pCurrentStage');

    if (stateEl) stateEl.textContent = data.state || 'unknown';
    if (taskEl) taskEl.textContent = data.current_task || '--';
    if (apiEl) apiEl.textContent = (data.api_calls_today || 0) + '/' + (data.api_limit || 10);
    if (lastRunEl) lastRunEl.textContent = data.last_run ? timeAgo(data.last_run) : '--';
    if (triggerEl) triggerEl.textContent = data.last_trigger ? timeAgo(data.last_trigger) : '--';
    if (stageEl) stageEl.textContent = data.current_stage || '--';
}

function renderStageFlow(data) {
    const flow = document.getElementById('stageFlow');
    if (!flow) return;

    const currentStage = data.current_stage;
    const isRunning = data.state && data.state.startsWith('pipeline:');
    const stages = data.pipeline_stages || {};

    let html = '';
    PIPELINE_STAGES.forEach((stage, i) => {
        const cfg = stages[stage] || {};
        const enabled = cfg.enabled !== false;
        let cls = 'stage-node';
        if (!enabled) cls += ' disabled';
        else if (isRunning && stage === currentStage) cls += ' active';
        else if (isRunning && PIPELINE_STAGES.indexOf(currentStage) > i) cls += ' completed';
        else cls += ' pending';

        html += '<div class="' + cls + '">';
        html += '<div class="stage-name">' + escHtml(stage) + '</div>';
        if (cfg.team) html += '<div class="stage-time">' + escHtml(cfg.team.join(', ')) + '</div>';
        html += '</div>';
        if (i < PIPELINE_STAGES.length - 1) {
            html += '<div class="stage-arrow">&#8594;</div>';
        }
    });
    flow.innerHTML = html;
}

// -- Pipeline Stats --

async function fetchPipelineStats() {
    try {
        const data = await api('/api/pipeline-stats');
        renderStageStats(data);
    } catch (e) {
        // No stats available yet
    }
}

function renderStageStats(data) {
    const el = document.getElementById('stageStats');
    if (!el || !data || !data.stats) return;
    const stats = data.stats;
    if (Object.keys(stats).length === 0) {
        el.innerHTML = '<div class="empty-state">No pipeline data yet</div>';
        return;
    }

    let html = '<div class="grid-3">';
    for (const [stage, sdata] of Object.entries(stats)) {
        html += '<div class="card stat-card">';
        html += '<h4>' + escHtml(stage) + '</h4>';
        html += '<div class="stat-row">CLI calls: <strong>' + (sdata.cli_calls || 0) + '</strong></div>';
        html += '<div class="stat-row">Subagents: <strong>' + (sdata.subagents || 0) + '</strong></div>';
        const tools = sdata.tool_calls || {};
        if (Object.keys(tools).length > 0) {
            html += '<div class="tool-breakdown"><strong>Tools:</strong>';
            for (const [tool, count] of Object.entries(tools).sort((a, b) => b[1] - a[1])) {
                const pct = Math.min(100, (count / Math.max(1, ...Object.values(tools))) * 100);
                html += '<div class="stat-bar-row">';
                html += '<span class="tool-name">' + escHtml(tool) + '</span>';
                html += '<div class="stat-bar"><div class="bar" style="width:' + pct + '%"></div></div>';
                html += '<span class="tool-count">' + count + '</span>';
                html += '</div>';
            }
            html += '</div>';
        }
        html += '</div>';
    }
    html += '</div>';
    el.innerHTML = html;
}

// -- Pipeline History --

async function fetchPipelineHistory() {
    try {
        const data = await api('/api/pipeline-history');
        renderPipelineHistory(data);
    } catch (e) {
        console.warn('History fetch failed:', e);
    }
}

function renderPipelineHistory(data) {
    const el = document.getElementById('pipelineHistory');
    if (!el) return;
    const items = data.runs || [];
    if (items.length === 0) {
        el.innerHTML = '<div class="empty-state">No completed runs</div>';
        return;
    }

    let html = '';
    items.forEach(run => {
        html += '<div class="card history-item" id="history-' + escHtml(run.task_id) + '">';
        html += '<div class="history-header" onclick="toggleHistory(\'' + escHtml(run.task_id) + '\')">';
        html += '<span class="history-title">' + escHtml(run.task_id) + '</span>';
        html += '<span class="text-muted text-sm">' + escHtml(run.timestamp || '') + '</span>';
        html += '</div>';
        html += '<div class="history-stages" style="display:none" id="stages-' + escHtml(run.task_id) + '">';
        html += '<div class="empty-state text-sm">Loading...</div>';
        html += '</div>';
        html += '</div>';
    });
    el.innerHTML = html;
}

async function toggleHistory(taskId) {
    const stagesEl = document.getElementById('stages-' + taskId);
    const item = document.getElementById('history-' + taskId);
    if (!stagesEl || !item) return;

    if (item.classList.contains('expanded')) {
        item.classList.remove('expanded');
        stagesEl.style.display = 'none';
        return;
    }

    item.classList.add('expanded');
    stagesEl.style.display = 'block';

    try {
        const data = await api('/pipeline-output/' + taskId);
        let html = '';
        for (const [stage, content] of Object.entries(data.stages || {})) {
            html += '<div class="detail-section">';
            html += '<h4>' + escHtml(stage) + '</h4>';
            html += '<div class="plan-content">' + renderMarkdown(content.substring(0, 2000)) + '</div>';
            html += '</div>';
        }
        stagesEl.innerHTML = html || '<div class="empty-state text-sm">No stage data</div>';
    } catch (e) {
        stagesEl.innerHTML = '<div class="text-muted text-sm">Failed to load stage data</div>';
    }
}

// -- Configuration --

async function loadPipelineConfig() {
    try {
        const data = await api('/api/config/pipeline');
        pipelineConfig = data;
        renderPipelineStagesConfig(data);
        if (currentConfigTab === 'pipeline') {
            document.getElementById('configEditor').value = JSON.stringify(data, null, 2);
        }
    } catch (e) {
        console.warn('Failed to load pipeline config:', e);
    }
}

async function loadProvidersConfig() {
    try {
        const data = await api('/api/config/providers');
        providersConfig = data;
        renderProviderList(data);
        if (currentConfigTab === 'providers') {
            document.getElementById('configEditor').value = JSON.stringify(data, null, 2);
        }
    } catch (e) {
        console.warn('Failed to load providers config:', e);
    }
}

function renderPipelineStagesConfig(cfg) {
    const el = document.getElementById('pipelineStagesConfig');
    if (!el) return;
    const stages = cfg.stages || {};
    let html = '<table class="pipeline-stats-table"><tr><th>Stage</th><th>Enabled</th><th>Team</th><th>Timeout</th></tr>';
    for (const [name, scfg] of Object.entries(stages)) {
        html += '<tr>';
        html += '<td><strong>' + escHtml(name) + '</strong></td>';
        html += '<td>' + (scfg.enabled !== false ? 'Yes' : 'No') + '</td>';
        html += '<td>' + escHtml((scfg.team || ['claude']).join(', ')) + '</td>';
        html += '<td>' + (scfg.timeout || 300) + 's</td>';
        html += '</tr>';
    }
    html += '</table>';
    if (cfg.program_manager) {
        const pm = cfg.program_manager;
        html += '<div class="mt-8"><strong>PM Backend:</strong> ' + escHtml(pm.backend || 'github_models');
        html += ' | <strong>Model:</strong> ' + escHtml(pm.model || 'gpt-4o') + '</div>';
    }
    el.innerHTML = html;
}

function renderProviderList(cfg) {
    const el = document.getElementById('providerList');
    if (!el) return;
    const providers = cfg.providers || {};
    if (Object.keys(providers).length === 0) {
        el.innerHTML = '<div class="empty-state">No providers configured</div>';
        return;
    }
    let html = '<div class="grid-3">';
    for (const [key, p] of Object.entries(providers)) {
        html += '<div class="card provider-card">';
        html += '<h4>' + escHtml(p.name || key) + '</h4>';
        html += '<div class="text-sm text-muted">Binary: ' + escHtml(p.binary || '?') + '</div>';
        const phases = ['plan', 'implement', 'simplify', 'test', 'security', 'review']
            .filter(ph => p[ph + '_args']);
        html += '<div class="text-sm mt-8">Phases: ' + (phases.length ? escHtml(phases.join(', ')) : 'default') + '</div>';
        html += '</div>';
    }
    html += '</div>';
    el.innerHTML = html;
}

function showConfigTab(tab) {
    currentConfigTab = tab;
    document.querySelectorAll('.config-editor .tab').forEach(t => t.classList.remove('active'));
    event.target.classList.add('active');
    loadCurrentConfig();
}

function loadCurrentConfig() {
    const editor = document.getElementById('configEditor');
    if (!editor) return;
    if (currentConfigTab === 'pipeline' && pipelineConfig) {
        editor.value = JSON.stringify(pipelineConfig, null, 2);
    } else if (currentConfigTab === 'providers' && providersConfig) {
        editor.value = JSON.stringify(providersConfig, null, 2);
    }
}

async function saveConfig() {
    const editor = document.getElementById('configEditor');
    if (!editor) return;
    let parsed;
    try {
        parsed = JSON.parse(editor.value);
    } catch (e) {
        alert('Invalid JSON: ' + e.message);
        return;
    }
    try {
        const path = currentConfigTab === 'pipeline' ? '/api/config/pipeline' : '/api/config/providers';
        await api(path, { method: 'PUT', body: parsed });
        if (currentConfigTab === 'pipeline') {
            pipelineConfig = parsed;
            renderPipelineStagesConfig(parsed);
        } else {
            providersConfig = parsed;
            renderProviderList(parsed);
        }
        alert('Saved!');
    } catch (e) {
        alert('Save failed: ' + e.message);
    }
}

// -- Polling --

function startPolling() {
    fetchStatus();
    fetchPipelineStats();
    fetchPipelineHistory();
    loadPipelineConfig();
    loadProvidersConfig();

    // Poll status every 5s when pipeline is running, 30s otherwise
    let lastState = '';
    async function poll() {
        const data = await fetchStatus();
        if (data) {
            const isActive = data.state && data.state.startsWith('pipeline:');
            const interval = isActive ? 5000 : 30000;
            if (isActive) {
                fetchPipelineStats();
            }
            if (data.state !== lastState) {
                lastState = data.state;
                if (!isActive) {
                    fetchPipelineHistory();
                    fetchPipelineStats();
                }
            }
            statusPollTimer = setTimeout(poll, interval);
        } else {
            statusPollTimer = setTimeout(poll, 10000);
        }
    }
    statusPollTimer = setTimeout(poll, 5000);
}

// -- Init --
startPolling();
