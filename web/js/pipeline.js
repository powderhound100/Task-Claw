// Task-Claw Pipeline Monitor

const PIPELINE_STAGES = ['rewrite', 'plan', 'code', 'simplify', 'test', 'review', 'publish'];
let currentConfigTab = 'pipeline';
let pipelineConfig = null;
let providersConfig = null;
let statusPollTimer = null;

// -- Visual Editor Stage Definitions --
const STAGE_DEFS = {
    rewrite:  { label: 'Rewrite',  color: '#a855f7', icon: '\u270F\uFE0F' },
    plan:     { label: 'Plan',     color: '#6366f1', icon: '\uD83D\uDCCB' },
    code:     { label: 'Code',     color: '#f59e0b', icon: '\u26A1' },
    simplify: { label: 'Simplify', color: '#06b6d4', icon: '\uD83D\uDD27' },
    test:     { label: 'Test',     color: '#3b82f6', icon: '\uD83E\uDDEA' },
    review:   { label: 'Review',   color: '#ef4444', icon: '\uD83D\uDD12' },
    publish:  { label: 'Publish',  color: '#22c55e', icon: '\uD83D\uDE80' }
};
let _selectedStage = null;
let _templateMenuOpen = false;

// -- Pipeline Templates --
const PIPELINE_TEMPLATES = {
    'fast-fix': {
        label: 'Fast Fix',
        description: 'Bug fixes: code + test only, no planning overhead',
        stages: {
            rewrite:  { enabled: false, team: ['claude'], timeout: 120 },
            plan:     { enabled: false, team: ['claude'], timeout: 900 },
            code:     { enabled: true,  team: ['claude'], timeout: 300 },
            simplify: { enabled: false, team: ['claude'], timeout: 300 },
            test:     { enabled: true,  team: ['claude'], timeout: 180 },
            review:   { enabled: false, team: ['claude'], timeout: 300 }
        }
    },
    'lightweight': {
        label: 'Lightweight',
        description: 'Small tasks: plan + code + test, no security review',
        stages: {
            rewrite:  { enabled: false, team: ['claude'], timeout: 120 },
            plan:     { enabled: true,  team: ['claude'], timeout: 300 },
            code:     { enabled: true,  team: ['claude'], timeout: 300 },
            simplify: { enabled: false, team: ['claude'], timeout: 300 },
            test:     { enabled: true,  team: ['claude'], timeout: 180 },
            review:   { enabled: false, team: ['claude'], timeout: 300 }
        }
    },
    'standard': {
        label: 'Standard',
        description: 'Default: full pipeline, single agent per stage',
        stages: {
            rewrite:  { enabled: true, team: ['claude'], timeout: 120 },
            plan:     { enabled: true, team: ['claude'], timeout: 900 },
            code:     { enabled: true, team: ['claude'], timeout: 600 },
            simplify: { enabled: true, team: ['claude'], timeout: 300 },
            test:     { enabled: true, team: ['claude'], timeout: 300 },
            review:   { enabled: true, team: ['claude'], timeout: 300 }
        }
    },
    'dual-parallel': {
        label: 'Dual Parallel',
        description: 'Complex features: 2 agents implement independently, cross-review + PM merge',
        stages: {
            rewrite:  { enabled: true, team: ['claude'],         timeout: 120 },
            plan:     { enabled: true, team: ['claude', 'claude'], timeout: 900 },
            code:     { enabled: true, team: ['claude', 'claude'], timeout: 900 },
            simplify: { enabled: true, team: ['claude'],         timeout: 300 },
            test:     { enabled: true, team: ['claude', 'claude'], timeout: 600 },
            review:   { enabled: true, team: ['claude'],         timeout: 300 }
        }
    },
    'full-swarm': {
        label: 'Full Swarm',
        description: 'Major features: 3-agent code swarm with full cross-review and dual test pass',
        stages: {
            rewrite:  { enabled: true, team: ['claude'],                   timeout: 120 },
            plan:     { enabled: true, team: ['claude', 'claude'],           timeout: 900 },
            code:     { enabled: true, team: ['claude', 'claude', 'claude'], timeout: 1200 },
            simplify: { enabled: true, team: ['claude'],                   timeout: 300 },
            test:     { enabled: true, team: ['claude', 'claude'],           timeout: 900 },
            review:   { enabled: true, team: ['claude', 'claude'],           timeout: 600 }
        }
    },
    'security-hardened': {
        label: 'Security Hardened',
        description: 'High-trust code: dual implementation with mandatory dual security review',
        stages: {
            rewrite:  { enabled: true, team: ['claude'],         timeout: 120 },
            plan:     { enabled: true, team: ['claude'],         timeout: 900 },
            code:     { enabled: true, team: ['claude', 'claude'], timeout: 900 },
            simplify: { enabled: true, team: ['claude'],         timeout: 300 },
            test:     { enabled: true, team: ['claude'],         timeout: 300 },
            review:   { enabled: true, team: ['claude', 'claude'], timeout: 600 }
        }
    }
};

function applyTemplate(key) {
    const tpl = PIPELINE_TEMPLATES[key];
    if (!tpl || !pipelineConfig) return;
    // Deep-merge stages: template stages override current config stages
    Object.entries(tpl.stages).forEach(([name, stageCfg]) => {
        if (!pipelineConfig.stages[name]) pipelineConfig.stages[name] = {};
        Object.assign(pipelineConfig.stages[name], stageCfg);
        // Preserve description if already set
        if (pipelineConfig.stages[name].description === undefined && stageCfg.description) {
            pipelineConfig.stages[name].description = stageCfg.description;
        }
    });
    markPipelineDirty();
    _selectedStage = null;
    renderPipelineStagesConfig(pipelineConfig);
    closeTemplateMenu();
    showToast('Template applied: ' + tpl.label + '. Save to persist.', 'success');
}

function openTemplateMenu() {
    const menu = document.getElementById('templateMenu');
    if (!menu) return;
    _templateMenuOpen = !_templateMenuOpen;
    menu.style.display = _templateMenuOpen ? 'block' : 'none';
}

function closeTemplateMenu() {
    _templateMenuOpen = false;
    const menu = document.getElementById('templateMenu');
    if (menu) menu.style.display = 'none';
}

function renderTemplateMenu() {
    const menu = document.getElementById('templateMenu');
    if (!menu) return;
    let html = '';
    Object.entries(PIPELINE_TEMPLATES).forEach(([key, tpl]) => {
        const agentCounts = Object.entries(tpl.stages)
            .filter(([, s]) => s.enabled)
            .map(([n, s]) => {
                const cnt = (s.team || []).length;
                return cnt > 1 ? n + '\xD7' + cnt : null;
            })
            .filter(Boolean);
        html += '<div class="template-item" onclick="applyTemplate(\'' + key + '\')">';
        html += '<div class="template-label">' + escHtml(tpl.label) + '</div>';
        html += '<div class="template-desc">' + escHtml(tpl.description) + '</div>';
        if (agentCounts.length > 0) {
            html += '<div class="template-tags">' + agentCounts.map(t => '<span class="template-tag">' + escHtml(t) + '</span>').join('') + '</div>';
        }
        html += '</div>';
    });
    menu.innerHTML = html;
}

// -- Agent Status + Pipeline Monitor --

async function fetchStatus() {
    try {
        const data = await api('/status');
        renderAgentStatus(data);
        renderStageFlow(data);
        renderLiveStageDetails(data);
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
    const completedStages = new Set((data.stage_log || []).map(e => e.stage));

    let html = '';
    PIPELINE_STAGES.forEach((stage, i) => {
        const cfg = stages[stage] || {};
        const enabled = cfg.enabled !== false;
        let cls = 'stage-node';

        // Find verdict for completed stages
        const logEntry = (data.stage_log || []).find(e => e.stage === stage);
        let verdictHtml = '';

        if (!enabled) {
            cls += ' disabled';
        } else if (completedStages.has(stage)) {
            cls += ' completed';
            if (logEntry) {
                const v = logEntry.verdict || '';
                const vCls = v === 'approve' ? 'approve' : v === 'revise' ? 'revise' :
                             v === 'blocked' ? 'revise' : 'direct';
                verdictHtml = '<div class="stage-status-icon">&#10003;</div>';
            }
        } else if (isRunning && stage === currentStage) {
            cls += ' active';
            verdictHtml = '<div class="stage-status-icon pulse-dot">&#9679;</div>';
        } else {
            cls += ' pending';
        }

        html += '<div class="' + cls + '">';
        html += verdictHtml;
        html += '<div class="stage-name">' + escHtml(stage) + '</div>';
        if (logEntry && logEntry.elapsed) {
            html += '<div class="stage-time">' + logEntry.elapsed + 's</div>';
        } else if (cfg.team) {
            html += '<div class="stage-time">' + escHtml(cfg.team.join(', ')) + '</div>';
        }
        html += '</div>';
        if (i < PIPELINE_STAGES.length - 1) {
            html += '<div class="stage-arrow">&#8594;</div>';
        }
    });

    // Pipeline elapsed timer
    if (isRunning && data.pipeline_started) {
        const started = new Date(data.pipeline_started);
        const elapsed = Math.round((Date.now() - started.getTime()) / 1000);
        const mins = Math.floor(elapsed / 60);
        const secs = elapsed % 60;
        html += '<div class="pipeline-elapsed">Running: ' + mins + 'm ' + secs + 's</div>';
    }

    flow.innerHTML = html;
}

function renderLiveStageDetails(data) {
    const el = document.getElementById('monitorDetails');
    if (!el) return;

    const stageLog = data.stage_log || [];
    const isRunning = data.state && data.state.startsWith('pipeline:');

    if (stageLog.length === 0 && !isRunning) {
        el.innerHTML = '';
        return;
    }

    let html = '';

    // Render completed stages
    stageLog.forEach(entry => {
        const v = entry.verdict || 'done';
        const vCls = v === 'approve' ? 'approve' : v === 'revise' ? 'revise' :
                     v === 'blocked' ? 'revise' : v === 'direct' ? 'direct' : 'done';

        html += '<div class="live-stage-detail completed">';
        html += '<div class="live-stage-header" onclick="toggleStageDetail(this)">';
        html += '<span class="stage-badge">' + escHtml(entry.stage) + '</span>';
        html += '<span class="verdict ' + vCls + '">' + escHtml(v.toUpperCase()) + '</span>';
        html += '<span class="elapsed">' + (entry.elapsed || 0) + 's</span>';
        if (entry.team) {
            html += '<span class="team-label">' + escHtml(entry.team.join(', ')) + '</span>';
        }
        html += '<span class="expand-toggle">&#9660;</span>';
        html += '</div>';

        // Issues
        if (entry.issues && entry.issues.length > 0) {
            html += '<div class="stage-issues">';
            entry.issues.forEach(iss => {
                html += '<div class="issue-item">&#9888; ' + escHtml(iss) + '</div>';
            });
            html += '</div>';
        }

        // Output (collapsible)
        html += '<div class="stage-output" style="display:none">';
        if (entry.output) {
            html += '<pre>' + escHtml(entry.output.substring(0, 2000)) + '</pre>';
        }

        // Comparison view for multi-agent stages
        if (entry.team_outputs && entry.team_outputs.length > 1) {
            html += renderComparisonView(entry);
        }

        html += '</div>';
        html += '</div>';
    });

    // Active stage indicator
    if (isRunning && data.current_stage) {
        const activeInLog = stageLog.some(e => e.stage === data.current_stage);
        if (!activeInLog) {
            html += '<div class="live-stage-detail active-stage">';
            html += '<div class="live-stage-header">';
            html += '<span class="stage-badge active">' + escHtml(data.current_stage) + '</span>';
            html += '<span class="verdict running">RUNNING</span>';
            html += '<span class="pulse-dot">&#9679;</span>';
            html += '</div>';
            html += '</div>';
        }
    }

    el.innerHTML = html;
}

function toggleStageDetail(headerEl) {
    const detail = headerEl.parentElement;
    const output = detail.querySelector('.stage-output');
    if (output) {
        const isHidden = output.style.display === 'none';
        output.style.display = isHidden ? 'block' : 'none';
        const toggle = headerEl.querySelector('.expand-toggle');
        if (toggle) toggle.textContent = isHidden ? '\u25B2' : '\u25BC';
    }
}

// -- Comparison View (Phase 7c) --

function renderComparisonView(stageEntry) {
    const outputs = stageEntry.team_outputs || [];
    if (outputs.length < 2) return '';

    let html = '<div class="comparison-section">';
    html += '<div class="comparison-toggle">';
    html += '<button class="btn btn-sm active" onclick="showCompView(this, \'side\')">Side-by-side</button>';
    html += '<button class="btn btn-sm" onclick="showCompView(this, \'unified\')">Unified</button>';
    html += '</div>';

    // Side-by-side view
    html += '<div class="comparison-view side-by-side">';
    outputs.forEach(([name, output]) => {
        html += '<div class="diff-pane">';
        html += '<div class="diff-pane-header">' + escHtml(name) + '</div>';
        html += '<pre class="diff-content">' + escHtml((output || '').substring(0, 3000)) + '</pre>';
        html += '</div>';
    });
    html += '</div>';

    // Cross-review summaries
    const reviews = stageEntry.cross_reviews || [];
    if (reviews.length > 0) {
        html += '<div class="cross-review-summary">';
        html += '<h5>Cross-Reviews</h5>';
        reviews.forEach(([reviewer, review]) => {
            html += '<div class="review-entry">';
            html += '<strong>' + escHtml(reviewer) + '</strong>';
            html += '<pre>' + escHtml((review || '').substring(0, 1000)) + '</pre>';
            html += '</div>';
        });
        html += '</div>';
    }

    // Comparison summary
    if (stageEntry.comparison_summary) {
        html += '<div class="comparison-summary">';
        html += '<h5>Comparison Summary</h5>';
        html += '<pre>' + escHtml(stageEntry.comparison_summary.substring(0, 2000)) + '</pre>';
        html += '</div>';
    }

    html += '</div>';
    return html;
}

function showCompView(btn, mode) {
    const section = btn.closest('.comparison-section');
    if (!section) return;
    section.querySelectorAll('.comparison-toggle .btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const view = section.querySelector('.comparison-view');
    if (view) {
        view.className = 'comparison-view ' + (mode === 'unified' ? 'unified' : 'side-by-side');
    }
}

function computeLineDiff(textA, textB) {
    const linesA = (textA || '').split('\n');
    const linesB = (textB || '').split('\n');
    const setA = new Set(linesA);
    const setB = new Set(linesB);

    const result = { a: [], b: [] };
    linesA.forEach(line => {
        result.a.push({ text: line, type: setB.has(line) ? 'common' : 'removed' });
    });
    linesB.forEach(line => {
        result.b.push({ text: line, type: setA.has(line) ? 'common' : 'added' });
    });
    return result;
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
        el.innerHTML = emptyState('pipeline', 'No pipeline data yet', 'Run a task to see stage stats');
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
        el.innerHTML = emptyState('history', 'No completed runs', 'Pipeline history will appear here');
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

let _pipelineDirty = false;

function markPipelineDirty() {
    _pipelineDirty = true;
    const bar = document.getElementById('pipelineSaveBar');
    if (bar) bar.style.display = 'flex';
}

function renderPipelineStagesConfig(cfg) {
    renderStagePalette(cfg);
    renderPipelineFlow(cfg);
    renderPmConfig(cfg);
    renderProvidersSidebar();
    // Update status line
    const statusEl = document.getElementById('peStatus');
    if (statusEl) {
        const enabled = Object.values(cfg.stages || {}).filter(s => s.enabled !== false).length;
        statusEl.textContent = 'idle \u00B7 PM: ' + ((cfg.program_manager || {}).backend || 'github_models') +
            ' \u00B7 ' + enabled + '/' + Object.keys(cfg.stages || {}).length + ' stages';
    }
    // Re-render properties if a stage is selected
    if (_selectedStage) renderStageProperties(_selectedStage);
}

function renderStagePalette(cfg) {
    const el = document.getElementById('peStagePalette');
    if (!el) return;
    const stages = cfg.stages || {};
    let html = '';
    PIPELINE_STAGES.forEach(function(name) {
        const scfg = stages[name] || {};
        const def = STAGE_DEFS[name] || {};
        const enabled = scfg.enabled !== false;
        const sel = _selectedStage === name ? ' selected' : '';
        const dis = enabled ? '' : ' disabled-stage';
        html += '<div class="pe-palette-item' + sel + dis + '" onclick="selectStage(\'' + name + '\')">';
        html += '<div class="pe-palette-icon" style="background:' + (def.color || '#666') + '20;color:' + (def.color || '#666') + '">' + (def.icon || '\u25CF') + '</div>';
        html += '<span class="pe-palette-name">' + escHtml(def.label || name) + '</span>';
        if (enabled) html += '<span class="pe-palette-check">\u2713</span>';
        html += '</div>';
    });
    el.innerHTML = html;
}

function renderPipelineFlow(cfg) {
    const el = document.getElementById('peFlow');
    if (!el) return;
    const stages = cfg.stages || {};
    let html = '';
    var stageNames = Object.keys(stages);
    stageNames.forEach(function(name, i) {
        var scfg = stages[name] || {};
        var def = STAGE_DEFS[name] || {};
        var enabled = scfg.enabled !== false;
        var team = (scfg.team || ['claude']).join(', ');
        var timeout = scfg.timeout || 300;
        var mins = Math.round(timeout / 60);
        var sel = _selectedStage === name ? ' selected' : '';
        var dis = enabled ? '' : ' disabled-node';

        html += '<div class="pe-node' + sel + dis + '" style="--node-color:' + (def.color || '#666') + ';border-color:' + (enabled ? (def.color || '#666') + '80' : 'var(--border)') + '" ';
        html += 'onclick="selectStage(\'' + name + '\')" data-stage="' + name + '">';
        html += '<div class="pe-node-icon" style="background:' + (def.color || '#666') + '20;color:' + (def.color || '#666') + '">' + (def.icon || '\u25CF') + '</div>';
        html += '<div class="pe-node-info">';
        html += '<div class="pe-node-name">' + escHtml(def.label || name) + '</div>';
        html += '<div class="pe-node-meta">' + escHtml(team) + ' \u00B7 ' + mins + 'm</div>';
        html += '</div>';
        html += '<button class="pe-node-close" onclick="event.stopPropagation();toggleStageEnabled(\'' + name + '\')" title="' + (enabled ? 'Disable' : 'Enable') + '">';
        html += enabled ? '\u00D7' : '+';
        html += '</button>';
        html += '</div>';

        if (i < stageNames.length - 1) {
            html += '<div class="pe-connector"><div class="pe-connector-line"></div><div class="pe-connector-dot"></div></div>';
        }
    });
    el.innerHTML = html;
}

function selectStage(name) {
    _selectedStage = _selectedStage === name ? null : name;
    if (pipelineConfig) {
        renderStagePalette(pipelineConfig);
        renderPipelineFlow(pipelineConfig);
    }
    if (_selectedStage) {
        renderStageProperties(_selectedStage);
    } else {
        var el = document.getElementById('pePropsContent');
        if (el) el.innerHTML = '<div class="pe-props-hint"><svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="1.5" style="width:32px;height:32px;color:var(--border);margin-bottom:8px"><circle cx="24" cy="24" r="16"/><path d="M24 16v8M24 28v2"/></svg><p class="text-muted text-sm">Select a stage to edit</p></div>';
    }
}

function renderStageProperties(name) {
    var el = document.getElementById('pePropsContent');
    if (!el || !pipelineConfig || !pipelineConfig.stages[name]) return;
    var scfg = pipelineConfig.stages[name];
    var def = STAGE_DEFS[name] || {};
    var enabled = scfg.enabled !== false;
    var team = (scfg.team || ['claude']).join(', ');
    var timeout = scfg.timeout || 300;

    var html = '<div class="pe-props-header">';
    html += '<div class="pe-props-icon" style="background:' + (def.color || '#666') + '20;color:' + (def.color || '#666') + '">' + (def.icon || '\u25CF') + '</div>';
    html += '<span class="pe-props-name">' + escHtml(def.label || name) + '</span>';
    html += '<button class="pe-props-delete" onclick="toggleStageEnabled(\'' + name + '\')">' + (enabled ? 'Disable' : 'Enable') + '</button>';
    html += '</div>';

    // Enabled toggle
    html += '<div class="pe-props-field"><div class="toggle-row"><span class="toggle-label">Enabled</span>';
    html += '<label class="toggle-switch"><input type="checkbox" ' + (enabled ? 'checked' : '') + ' onchange="toggleStage(\'' + name + '\',this.checked);renderStageProperties(\'' + name + '\')"><span class="toggle-slider"></span></label>';
    html += '</div></div>';

    // Provider / Team
    html += '<div class="pe-props-field"><label>PROVIDER</label>';
    html += '<input type="text" class="inline-input" value="' + escHtml(team) + '" ';
    html += 'onchange="updateStageField(\'' + name + '\',\'team\',this.value)" placeholder="claude, copilot"></div>';

    // Timeout
    html += '<div class="pe-props-field"><label>TIMEOUT (SECONDS)</label>';
    html += '<input type="number" class="inline-input" value="' + timeout + '" min="30" max="3600" ';
    html += 'onchange="updateStageField(\'' + name + '\',\'timeout\',parseInt(this.value))"></div>';

    // Team size info
    var teamArr = (scfg.team || ['claude']);
    html += '<div class="pe-props-field"><label>TEAM SIZE</label>';
    html += '<div style="font-size:0.85rem;color:var(--text)">' + teamArr.length + ' agent' + (teamArr.length !== 1 ? 's' : '') + '</div></div>';

    // Stage-specific notes
    if (name === 'code') {
        html += '<div class="pe-props-field"><label>NOTES</label>';
        html += '<div style="font-size:0.78rem;color:var(--text-muted)">Multi-agent: when 2+ agents are assigned, they implement independently and cross-review.</div></div>';
    } else if (name === 'review') {
        html += '<div class="pe-props-field"><label>NOTES</label>';
        html += '<div style="font-size:0.78rem;color:var(--text-muted)">Security audit. HIGH severity findings block publish.</div></div>';
    } else if (name === 'publish') {
        html += '<div class="pe-props-field"><label>NOTES</label>';
        html += '<div style="font-size:0.78rem;color:var(--text-muted)">Git commit + push. Blocked if tests fail or security review finds HIGH severity.</div></div>';
    }

    el.innerHTML = html;
}

function toggleStageEnabled(name) {
    if (!pipelineConfig || !pipelineConfig.stages[name]) return;
    var enabled = pipelineConfig.stages[name].enabled !== false;
    pipelineConfig.stages[name].enabled = !enabled;
    markPipelineDirty();
    renderPipelineStagesConfig(pipelineConfig);
}

function triggerPipelineRun() {
    api('/trigger', { method: 'POST', body: {} }).then(function() {
        showToast('Pipeline triggered!', 'success');
    }).catch(function(e) {
        showToast('Trigger failed: ' + e.message, 'error');
    });
}

function renderProvidersSidebar() {
    var el = document.getElementById('providerList');
    if (!el || !providersConfig) return;
    var providers = (providersConfig || {}).providers || {};
    var keys = Object.keys(providers);
    if (keys.length === 0) {
        el.innerHTML = '<div class="text-muted text-sm">None configured</div>';
        return;
    }
    var colors = ['#3b82f6', '#f59e0b', '#22c55e', '#a855f7', '#ef4444', '#06b6d4'];
    var html = '';
    keys.forEach(function(key, i) {
        var p = providers[key];
        html += '<div class="pe-provider-item">';
        html += '<div class="pe-provider-dot" style="background:' + colors[i % colors.length] + '"></div>';
        html += '<span class="pe-provider-name">' + escHtml(p.name || key) + '</span>';
        html += '<span class="pe-provider-binary">' + escHtml(p.binary || '?') + '</span>';
        html += '</div>';
    });
    el.innerHTML = html;
}

// Backend → model presets and required API key
var BACKEND_PRESETS = {
    github_models: {
        label: 'GitHub Models',
        models: ['gpt-4o', 'gpt-4o-mini', 'gpt-4.1', 'o3-mini'],
        default_model: 'gpt-4o',
        key_name: 'GITHUB_TOKEN',
        key_label: 'GitHub Token',
        key_placeholder: 'ghp_...'
    },
    anthropic: {
        label: 'Anthropic',
        models: ['claude-sonnet-4-20250514', 'claude-haiku-4-5-20251001', 'claude-opus-4-20250514'],
        default_model: 'claude-sonnet-4-20250514',
        key_name: 'ANTHROPIC_API_KEY',
        key_label: 'Anthropic Key',
        key_placeholder: 'sk-ant-...'
    },
    openai_compatible: {
        label: 'OpenAI Compatible',
        models: ['gpt-4o', 'gpt-4.1', 'llama3', 'mistral'],
        default_model: 'gpt-4o',
        key_name: 'PIPELINE_PM_KEY',
        key_label: 'API Key',
        key_placeholder: 'sk-...',
        extra_keys: [
            { name: 'PIPELINE_PM_URL', label: 'Endpoint URL', placeholder: 'http://localhost:11434/v1/chat/completions' }
        ]
    }
};

var _secretsStatus = {};

function loadSecretsStatus() {
    api('/api/secrets').then(function(data) {
        _secretsStatus = data.keys || {};
        if (pipelineConfig) renderPmConfig(pipelineConfig);
    }).catch(function() {});
}

function renderPmConfig(cfg) {
    const el = document.getElementById('pmConfig');
    if (!el || !cfg.program_manager) return;
    const pm = cfg.program_manager;
    const backend = pm.backend || 'github_models';
    const preset = BACKEND_PRESETS[backend] || BACKEND_PRESETS.github_models;

    let html = '';

    // Backend selector
    html += '<div class="stage-config-field">';
    html += '<label>Backend</label>';
    html += '<select class="inline-input" onchange="switchPmBackend(this.value)">';
    Object.keys(BACKEND_PRESETS).forEach(function(b) {
        html += '<option value="' + b + '"' + (backend === b ? ' selected' : '') + '>' + escHtml(BACKEND_PRESETS[b].label) + '</option>';
    });
    html += '</select>';
    html += '</div>';

    // Model — dropdown with presets + custom option
    html += '<div class="stage-config-field">';
    html += '<label>Model</label>';
    var currentModel = pm.model || preset.default_model;
    var isCustom = preset.models.indexOf(currentModel) === -1;
    html += '<select class="inline-input" onchange="handleModelSelect(this.value)">';
    preset.models.forEach(function(m) {
        html += '<option value="' + m + '"' + (currentModel === m ? ' selected' : '') + '>' + escHtml(m) + '</option>';
    });
    html += '<option value="__custom__"' + (isCustom ? ' selected' : '') + '>Custom...</option>';
    html += '</select>';
    if (isCustom) {
        html += '<input type="text" class="inline-input mt-4" value="' + escHtml(currentModel) + '" ';
        html += 'placeholder="Model name" onchange="updatePmField(\'model\', this.value)">';
    }
    html += '</div>';

    // Temperature
    html += '<div class="stage-config-field">';
    html += '<label>Temp</label>';
    html += '<input type="number" class="inline-input" value="' + (pm.temperature || 0.3) + '" step="0.1" min="0" max="2" ';
    html += 'onchange="updatePmField(\'temperature\', parseFloat(this.value))">';
    html += '</div>';

    // API Key section
    html += '<div class="pe-divider"></div>';
    html += '<div class="pe-group-label">API KEYS</div>';

    // Primary key for this backend
    var keyStatus = _secretsStatus[preset.key_name] || {};
    html += _renderKeyField(preset.key_name, preset.key_label, preset.key_placeholder, keyStatus);

    // Extra keys (e.g., URL for openai_compatible)
    if (preset.extra_keys) {
        preset.extra_keys.forEach(function(ek) {
            var ekStatus = _secretsStatus[ek.name] || {};
            html += _renderKeyField(ek.name, ek.label, ek.placeholder, ekStatus);
        });
    }

    el.innerHTML = html;
}

function _renderKeyField(keyName, label, placeholder, status) {
    var isSet = status.set;
    var source = status.source || 'none';
    var dotClass = isSet ? 'key-dot-set' : 'key-dot-unset';
    var sourceLabel = source === 'ui' ? 'saved' : (source === 'env' ? '.env' : 'not set');

    var html = '<div class="stage-config-field key-field">';
    html += '<label><span class="key-dot ' + dotClass + '"></span>' + escHtml(label) + ' <span class="text-muted text-sm">(' + sourceLabel + ')</span></label>';
    html += '<div class="key-input-row">';
    html += '<input type="password" class="inline-input key-input" id="key_' + keyName + '" ';
    html += 'placeholder="' + (isSet ? '••••••••' : placeholder) + '">';
    html += '<button class="btn btn-sm btn-key-save" onclick="saveApiKey(\'' + keyName + '\')">Set</button>';
    html += '</div>';
    if (isSet && source === 'ui') {
        html += '<button class="btn-link text-sm text-muted" onclick="clearApiKey(\'' + keyName + '\')">Clear saved key</button>';
    }
    html += '</div>';
    return html;
}

function switchPmBackend(backend) {
    if (!pipelineConfig || !pipelineConfig.program_manager) return;
    var preset = BACKEND_PRESETS[backend];
    if (!preset) return;
    pipelineConfig.program_manager.backend = backend;
    pipelineConfig.program_manager.model = preset.default_model;
    markPipelineDirty();
    renderPmConfig(pipelineConfig);
    // Update toolbar status text
    renderPipelineStagesConfig(pipelineConfig);
}

function handleModelSelect(value) {
    if (value === '__custom__') {
        // Re-render to show custom input — set model to empty so user types it
        if (pipelineConfig && pipelineConfig.program_manager) {
            pipelineConfig.program_manager.model = '';
            markPipelineDirty();
            renderPmConfig(pipelineConfig);
            // Focus the custom input
            var inp = document.querySelector('#pmConfig .key-input, #pmConfig input[type="text"]');
            if (inp) inp.focus();
        }
    } else {
        updatePmField('model', value);
    }
}

async function saveApiKey(keyName) {
    var input = document.getElementById('key_' + keyName);
    if (!input || !input.value.trim()) {
        showToast('Enter a key value first', 'warning');
        return;
    }
    try {
        var body = {};
        body[keyName] = input.value.trim();
        await api('/api/secrets', { method: 'PUT', body: body });
        input.value = '';
        showToast(keyName + ' saved', 'success');
        loadSecretsStatus();
    } catch (e) {
        showToast('Failed to save key: ' + e.message, 'error');
    }
}

async function clearApiKey(keyName) {
    try {
        var body = {};
        body[keyName] = '';
        await api('/api/secrets', { method: 'PUT', body: body });
        showToast(keyName + ' cleared', 'success');
        loadSecretsStatus();
    } catch (e) {
        showToast('Failed to clear key: ' + e.message, 'error');
    }
}

function toggleStage(name, enabled) {
    if (!pipelineConfig || !pipelineConfig.stages[name]) return;
    pipelineConfig.stages[name].enabled = enabled;
    markPipelineDirty();
    renderPipelineStagesConfig(pipelineConfig);
}

function updateStageField(name, field, value) {
    if (!pipelineConfig || !pipelineConfig.stages[name]) return;
    if (field === 'team') {
        pipelineConfig.stages[name].team = value.split(',').map(s => s.trim()).filter(Boolean);
    } else {
        pipelineConfig.stages[name][field] = value;
    }
    markPipelineDirty();
    // Refresh flow nodes to show updated meta
    renderPipelineFlow(pipelineConfig);
}

function updatePmField(field, value) {
    if (!pipelineConfig || !pipelineConfig.program_manager) return;
    pipelineConfig.program_manager[field] = value;
    markPipelineDirty();
    if (field === 'backend') renderPmConfig(pipelineConfig);
}

async function savePipelineConfig() {
    try {
        await api('/api/config/pipeline', { method: 'PUT', body: pipelineConfig });
        _pipelineDirty = false;
        document.getElementById('pipelineSaveBar').style.display = 'none';
        showToast('Pipeline config saved!', 'success');
        // Refresh the raw editor too
        if (currentConfigTab === 'pipeline') {
            document.getElementById('configEditor').value = JSON.stringify(pipelineConfig, null, 2);
        }
    } catch (e) {
        showToast('Save failed: ' + e.message, 'error');
    }
}

function resetPipelineConfig() {
    _pipelineDirty = false;
    document.getElementById('pipelineSaveBar').style.display = 'none';
    loadPipelineConfig();
}

function renderProviderList(cfg) {
    // Providers are now rendered in the sidebar via renderProvidersSidebar
    providersConfig = cfg;
    renderProvidersSidebar();
}

function showConfigTab(tab, evt) {
    currentConfigTab = tab;
    document.querySelectorAll('.config-editor .tab').forEach(t => t.classList.remove('active'));
    if (evt && evt.target) evt.target.classList.add('active');
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
        showToast('Invalid JSON: ' + e.message, 'error');
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
        showToast('Configuration saved!', 'success');
    } catch (e) {
        showToast('Save failed: ' + e.message, 'error');
    }
}

// -- Polling --

function startPolling() {
    fetchStatus();
    fetchPipelineStats();
    fetchPipelineHistory();
    loadPipelineConfig();
    loadProvidersConfig();
    loadSecretsStatus();
    renderTemplateMenu();

    // Close template menu when clicking outside
    document.addEventListener('click', function(e) {
        const picker = document.getElementById('templatePicker');
        if (picker && !picker.contains(e.target)) closeTemplateMenu();
    });

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

// -- Skills --

let skillsData = [];
let _skillRunId = null;

async function fetchSkills() {
    try {
        const data = await api('/api/skills');
        skillsData = data.skills || [];
        renderSkillsList(skillsData);
    } catch (e) {
        console.warn('Skills fetch failed:', e);
        const el = document.getElementById('skillsList');
        if (el) el.innerHTML = emptyState('skills', 'Failed to load skills', 'Server may need a restart — ' + e.message);
    }
}

function renderSkillsList(skills) {
    const el = document.getElementById('skillsList');
    if (!el) return;
    if (skills.length === 0) {
        el.innerHTML = emptyState('skills', 'No skills defined', 'Click "+ New Skill" to create one');
        return;
    }

    let html = '';
    skills.forEach(skill => {
        const isEnv = skill.source === 'environment';
        const tags = (skill.tags || []).map(t => '<span class="skill-tag">' + escHtml(t) + '</span>').join('');
        html += '<div class="card skill-card' + (isEnv ? ' env-skill' : '') + '">';
        html += '<div class="skill-card-header">';
        html += '<h4>' + escHtml(skill.name || skill.id) + '</h4>';
        if (isEnv) {
            html += '<span class="skill-source-badge">env</span>';
        }
        html += '</div>';
        if (skill.description) {
            html += '<p class="text-muted text-sm">' + escHtml(skill.description) + '</p>';
        }
        html += '<div class="skill-meta">';
        html += '<span class="skill-phase">' + escHtml(skill.phase || 'implement') + '</span>';
        if (skill.provider) {
            html += '<span class="skill-provider">' + escHtml(skill.provider) + '</span>';
        }
        if (tags) html += '<div class="skill-tags">' + tags + '</div>';
        html += '</div>';
        html += '<div class="skill-actions mt-8">';
        html += '<button class="btn btn-primary btn-sm" onclick="showSkillRunModal(\'' + escHtml(skill.id) + '\')">Run</button>';
        if (!isEnv) {
            html += '<button class="btn btn-secondary btn-sm" onclick="editSkill(\'' + escHtml(skill.id) + '\')">Edit</button>';
            html += '<button class="btn btn-danger btn-sm" onclick="deleteSkill(\'' + escHtml(skill.id) + '\')">Delete</button>';
        }
        html += '</div>';
        html += '</div>';
    });
    el.innerHTML = html;
}

function showSkillForm(editId) {
    document.getElementById('skillForm').style.display = 'block';
    document.getElementById('skillFormTitle').textContent = editId ? 'Edit Skill' : 'New Skill';
    document.getElementById('skillEditId').value = editId || '';
    if (!editId) {
        document.getElementById('skillName').value = '';
        document.getElementById('skillDesc').value = '';
        document.getElementById('skillPrompt').value = '';
        document.getElementById('skillPhase').value = 'implement';
        document.getElementById('skillProvider').value = '';
        document.getElementById('skillTags').value = '';
    }
}

function hideSkillForm() {
    document.getElementById('skillForm').style.display = 'none';
}

function editSkill(skillId) {
    const skill = skillsData.find(s => s.id === skillId);
    if (!skill) return;
    showSkillForm(skillId);
    document.getElementById('skillName').value = skill.name || '';
    document.getElementById('skillDesc').value = skill.description || '';
    document.getElementById('skillPrompt').value = skill.prompt || '';
    document.getElementById('skillPhase').value = skill.phase || 'implement';
    document.getElementById('skillProvider').value = skill.provider || '';
    document.getElementById('skillTags').value = (skill.tags || []).join(', ');
}

async function saveSkill() {
    const editId = document.getElementById('skillEditId').value;
    const body = {
        name: document.getElementById('skillName').value.trim(),
        description: document.getElementById('skillDesc').value.trim(),
        prompt: document.getElementById('skillPrompt').value.trim(),
        phase: document.getElementById('skillPhase').value,
        provider: document.getElementById('skillProvider').value.trim() || null,
        tags: document.getElementById('skillTags').value.split(',').map(t => t.trim()).filter(Boolean),
    };

    if (!body.name || !body.prompt) {
        showToast('Name and prompt are required.', 'warning');
        return;
    }

    try {
        if (editId) {
            await api('/api/skills/' + editId, { method: 'PUT', body });
        } else {
            body.id = body.name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
            await api('/api/skills', { method: 'POST', body });
        }
        hideSkillForm();
        fetchSkills();
    } catch (e) {
        showToast('Failed to save skill: ' + e.message, 'error');
    }
}

async function deleteSkill(skillId) {
    const confirmed = await showConfirm('Delete skill "' + skillId + '"?');
    if (!confirmed) return;
    try {
        await api('/api/skills/' + skillId, { method: 'DELETE' });
        fetchSkills();
        showToast('Skill deleted', 'success');
    } catch (e) {
        showToast('Failed to delete: ' + e.message, 'error');
    }
}

function showSkillRunModal(skillId) {
    const skill = skillsData.find(s => s.id === skillId);
    if (!skill) return;
    _skillRunId = skillId;
    document.getElementById('skillRunModal').style.display = 'block';
    document.getElementById('skillRunName').textContent = skill.name || skillId;
    document.getElementById('skillRunInput').value = '';
    document.getElementById('skillRunProvider').value = '';
    document.getElementById('skillRunOutput').style.display = 'none';
    document.getElementById('skillRunOutputText').textContent = '';
}

function hideSkillRunModal() {
    document.getElementById('skillRunModal').style.display = 'none';
    _skillRunId = null;
}

async function executeSkill() {
    if (!_skillRunId) return;
    const input = document.getElementById('skillRunInput').value.trim();
    const provider = document.getElementById('skillRunProvider').value.trim() || undefined;
    const outputEl = document.getElementById('skillRunOutput');
    const textEl = document.getElementById('skillRunOutputText');

    outputEl.style.display = 'block';
    textEl.textContent = 'Running skill...';

    try {
        await api('/api/skills/' + _skillRunId + '/run', {
            method: 'POST',
            body: { input, provider },
        });
        textEl.textContent = 'Skill started. Output will appear when complete.\nPolling for results...';
        pollSkillOutput(_skillRunId);
    } catch (e) {
        textEl.textContent = 'Error: ' + e.message;
    }
}

async function pollSkillOutput(skillId) {
    const textEl = document.getElementById('skillRunOutputText');
    let attempts = 0;
    const maxAttempts = 120; // 10 minutes at 5s intervals

    async function check() {
        attempts++;
        try {
            const data = await api('/api/skills/' + skillId + '/runs');
            const runs = (data.runs || []).filter(r => r.status !== 'running');
            if (runs.length > 0) {
                const latest = runs[runs.length - 1];
                const runId = latest.run_id;
                try {
                    const full = await api('/skill-output/' + runId);
                    textEl.textContent = full.output || latest.output || 'No output';
                } catch (_) {
                    textEl.textContent = latest.output || 'Completed (no output captured)';
                }
                return;
            }
        } catch (e) {
            // keep polling
        }
        if (attempts < maxAttempts) {
            setTimeout(check, 5000);
            textEl.textContent = 'Running skill... (' + (attempts * 5) + 's)';
        } else {
            textEl.textContent = 'Timed out waiting for skill output.';
        }
    }
    setTimeout(check, 3000);
}

// -- Init --
startPolling();
fetchSkills();
