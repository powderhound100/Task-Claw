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
    }
}

function renderSkillsList(skills) {
    const el = document.getElementById('skillsList');
    if (!el) return;
    if (skills.length === 0) {
        el.innerHTML = '<div class="empty-state">No skills defined. Click "+ New Skill" to create one.</div>';
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
        alert('Name and prompt are required.');
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
        alert('Failed to save skill: ' + e.message);
    }
}

async function deleteSkill(skillId) {
    if (!confirm('Delete skill "' + skillId + '"?')) return;
    try {
        await api('/api/skills/' + skillId, { method: 'DELETE' });
        fetchSkills();
    } catch (e) {
        alert('Failed to delete: ' + e.message);
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
