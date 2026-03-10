// Task-Claw Tasks + Ideas UI

let tasks = [];
let ideas = [];
let editingTaskId = null;
let editingIsIdea = false;
let activeTypeFilter = 'all';
let activeStatusFilter = 'all';
let activeIdeaFilter = 'all';
let pendingPhotos = [];
let currentView = 'tasks'; // 'tasks' or 'ideas'
let pollTimer = null;
const PIPELINE_STAGES = ['rewrite', 'plan', 'code', 'simplify', 'test', 'review', 'publish'];

// ========================
// DATA
// ========================

async function loadTasks() {
    try {
        tasks = await api('/api/tasks');
        if (!Array.isArray(tasks)) tasks = [];
    } catch (e) {
        console.warn('Failed to load tasks:', e);
    }
}

async function loadIdeas() {
    try {
        ideas = await api('/api/ideas');
        if (!Array.isArray(ideas)) ideas = [];
    } catch (e) {
        console.warn('Failed to load ideas:', e);
    }
}

// ========================
// TABS
// ========================

function switchView(view) {
    currentView = view;
    document.getElementById('tabTasks').classList.toggle('active', view === 'tasks');
    document.getElementById('tabIdeas').classList.toggle('active', view === 'ideas');
    document.getElementById('taskFilters').style.display = view === 'tasks' ? '' : 'none';
    document.getElementById('taskList').style.display = view === 'tasks' ? '' : 'none';
    document.getElementById('ideaSection').style.display = view === 'ideas' ? '' : 'none';
    const btn = document.getElementById('btnNewTask');
    btn.textContent = view === 'tasks' ? '+ New Task' : '+ New Idea';
    renderAll();
}

document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('tabTasks').addEventListener('click', e => { e.preventDefault(); switchView('tasks'); });
    document.getElementById('tabIdeas').addEventListener('click', e => { e.preventDefault(); switchView('ideas'); });
});

// ========================
// FILTERS
// ========================

function getTypeIcon(type) {
    const icons = { bug: '[BUG]', feature: '[FEAT]', improvement: '[IMP]', task: '[TASK]' };
    return icons[type] || '[TASK]';
}

function getStatusLabel(status) {
    const labels = {
        open: 'Open', grabbed: 'Grabbed', 'in-progress': 'In Progress',
        done: 'Done', 'security-blocked': 'Blocked', 'pushed-to-production': 'Deployed',
        planned: 'Planned', planning: 'Planning'
    };
    return labels[status] || status || 'Open';
}

function getStatusClass(status) {
    const map = {
        open: 'badge-info', grabbed: 'badge-warning', 'in-progress': 'badge-warning',
        done: 'badge-success', 'security-blocked': 'badge-danger', 'pushed-to-production': 'badge-success',
        planned: 'badge-info', planning: 'badge-warning'
    };
    return map[status] || 'badge-info';
}

function getPriorityClass(priority) {
    return 'priority-' + (priority || 'medium');
}

function renderFilters() {
    const items = currentView === 'tasks' ? tasks : ideas;
    const container = currentView === 'tasks' ? document.getElementById('taskFilters') : document.getElementById('ideaFilters');
    if (!container) return;

    // Type filters (tasks only)
    let html = '';
    if (currentView === 'tasks') {
        const types = ['all', 'bug', 'feature', 'improvement', 'task'];
        types.forEach(t => {
            const count = t === 'all' ? items.length : items.filter(i => i.type === t).length;
            const active = activeTypeFilter === t ? ' active' : '';
            html += '<button class="filter-btn' + active + '" onclick="setTypeFilter(\'' + t + '\')">';
            html += (t === 'all' ? 'All' : t.charAt(0).toUpperCase() + t.slice(1));
            html += ' <span class="filter-count">' + count + '</span></button>';
        });
        html += '<span class="filter-separator">|</span>';
    }

    // Status filters
    const statuses = ['all', 'open', 'in-progress', 'done', 'planned'];
    const activeFilter = currentView === 'tasks' ? activeStatusFilter : activeIdeaFilter;
    statuses.forEach(s => {
        const count = s === 'all' ? items.length : items.filter(i => (i.status || 'open') === s).length;
        const active = activeFilter === s ? ' active' : '';
        html += '<button class="filter-btn' + active + '" onclick="setStatusFilter(\'' + s + '\')">';
        html += (s === 'all' ? 'All' : getStatusLabel(s));
        html += ' <span class="filter-count">' + count + '</span></button>';
    });

    container.innerHTML = html;
}

function setTypeFilter(type) {
    activeTypeFilter = type;
    renderAll();
}

function setStatusFilter(status) {
    if (currentView === 'tasks') activeStatusFilter = status;
    else activeIdeaFilter = status;
    renderAll();
}

function getFilteredItems() {
    let items = currentView === 'tasks' ? tasks : ideas;
    const statusFilter = currentView === 'tasks' ? activeStatusFilter : activeIdeaFilter;
    if (currentView === 'tasks' && activeTypeFilter !== 'all') {
        items = items.filter(i => i.type === activeTypeFilter);
    }
    if (statusFilter !== 'all') {
        items = items.filter(i => (i.status || 'open') === statusFilter);
    }
    return items;
}

// ========================
// RENDERING
// ========================

function renderTasks() {
    const container = document.getElementById('taskList');
    if (!container || currentView !== 'tasks') return;
    const items = getFilteredItems();

    if (items.length === 0) {
        container.innerHTML = '<div class="empty-state">No tasks found</div>';
        return;
    }

    let html = '';
    items.forEach(t => {
        const isDone = t.status === 'done' || t.status === 'pushed-to-production';
        html += '<div class="task-card ' + getPriorityClass(t.priority) + (isDone ? ' done' : '') + '" onclick="openTaskDetail(\'' + escHtml(t.id) + '\')">';
        html += '<div class="task-checkbox' + (isDone ? ' checked' : '') + '" onclick="event.stopPropagation();toggleTaskDone(\'' + escHtml(t.id) + '\')"></div>';
        html += '<div class="task-info">';
        html += '<div class="task-title">' + escHtml(t.title) + '</div>';
        html += '<div class="task-meta">' + timeAgo(t.created || t.updated) + '</div>';
        html += '<div class="task-badges">';
        html += '<span class="badge type-' + (t.type || 'task') + '">' + getTypeIcon(t.type) + '</span>';
        html += '<span class="badge ' + getStatusClass(t.status) + '">' + getStatusLabel(t.status) + '</span>';
        if (t.priority) html += '<span class="badge badge-' + (t.priority === 'high' ? 'danger' : t.priority === 'low' ? 'success' : 'warning') + '">' + escHtml(t.priority) + '</span>';
        if (t.cli_provider) html += '<span class="badge">' + escHtml(t.cli_provider) + '</span>';
        html += '</div>';
        if (t.description) html += '<div class="task-desc text-sm text-muted">' + escHtml((t.description || '').substring(0, 120)) + '</div>';
        if (t.photos && t.photos.length) {
            html += '<div class="task-photos">';
            t.photos.slice(0, 3).forEach(p => {
                html += '<img class="photo-thumb-sm" src="/photos/' + escHtml(p) + '" alt="" loading="lazy">';
            });
            if (t.photos.length > 3) html += '<span class="text-muted text-sm">+' + (t.photos.length - 3) + '</span>';
            html += '</div>';
        }
        html += '</div></div>';
    });
    container.innerHTML = html;
}

function renderIdeas() {
    const container = document.getElementById('ideaList');
    if (!container || currentView !== 'ideas') return;
    const items = getFilteredItems();

    if (items.length === 0) {
        container.innerHTML = '<div class="empty-state">No ideas found</div>';
        return;
    }

    let html = '';
    items.forEach(idea => {
        const isDone = idea.status === 'done' || idea.status === 'pushed-to-production';
        html += '<div class="task-card idea-card' + (isDone ? ' done' : '') + '" onclick="openIdeaDetail(\'' + escHtml(idea.id) + '\')">';
        html += '<div class="task-info">';
        html += '<div class="task-title">' + escHtml(idea.title) + '</div>';
        html += '<div class="task-meta">' + timeAgo(idea.created || idea.updated) + '</div>';
        html += '<div class="task-badges">';
        html += '<span class="badge ' + getStatusClass(idea.status) + '">' + getStatusLabel(idea.status) + '</span>';
        if (idea.research_status) html += '<span class="badge research-status badge-info">' + escHtml(idea.research_status) + '</span>';
        html += '</div>';
        if (idea.description) html += '<div class="task-desc text-sm text-muted">' + escHtml((idea.description || '').substring(0, 120)) + '</div>';
        html += '</div></div>';
    });
    container.innerHTML = html;
}

function renderAll() {
    renderFilters();
    if (currentView === 'tasks') renderTasks();
    else renderIdeas();
    renderPipelineOverview();
}

// ========================
// PIPELINE OVERVIEW STRIP
// ========================

function renderPipelineOverview() {
    const el = document.getElementById('pipelineOverview');
    if (!el) return;
    const activeTasks = tasks.filter(t => t.status === 'in-progress' || t.status === 'grabbed');
    if (activeTasks.length === 0) {
        el.style.display = 'none';
        return;
    }
    el.style.display = '';
    let html = '<div class="pipeline-strip">';
    PIPELINE_STAGES.forEach((s, i) => {
        html += '<span class="pipeline-dot" id="dot-' + s + '">' + s + '</span>';
        if (i < PIPELINE_STAGES.length - 1) html += '<span class="pipeline-arrow">&rarr;</span>';
    });
    html += '</div>';
    el.innerHTML = html;
}

// ========================
// TASK FORM
// ========================

function openTaskForm(id) {
    editingTaskId = id || null;
    editingIsIdea = currentView === 'ideas';
    pendingPhotos = [];
    document.getElementById('photoPreviews').innerHTML = '';

    const title = document.getElementById('formTitle');
    const form = document.getElementById('taskForm');
    form.reset();

    if (editingIsIdea) {
        title.textContent = id ? 'Edit Idea' : 'New Idea';
        // Hide task-specific fields
        document.getElementById('taskType').closest('.form-group').style.display = 'none';
        document.getElementById('taskPriority').closest('.form-group').parentElement.style.display = 'none';
        document.getElementById('taskAutoImplement').closest('.form-group').style.display = 'none';
        document.getElementById('photoUpload').closest('.form-group').style.display = 'none';
    } else {
        title.textContent = id ? 'Edit Task' : 'New Task';
        document.getElementById('taskType').closest('.form-group').style.display = '';
        document.getElementById('taskPriority').closest('.form-group').parentElement.style.display = '';
        document.getElementById('taskAutoImplement').closest('.form-group').style.display = '';
        document.getElementById('photoUpload').closest('.form-group').style.display = '';
    }

    if (id) {
        const item = editingIsIdea ? ideas.find(i => i.id === id) : tasks.find(t => t.id === id);
        if (item) {
            document.getElementById('taskTitle').value = item.title || '';
            document.getElementById('taskDesc').value = item.description || '';
            if (!editingIsIdea) {
                document.getElementById('taskType').value = item.type || 'task';
                document.getElementById('taskPriority').value = item.priority || 'medium';
                document.getElementById('taskProvider').value = item.cli_provider || '';
                document.getElementById('taskAutoImplement').checked = item.auto_implement !== false;
            }
        }
    }

    document.getElementById('taskFormOverlay').style.display = 'flex';
    document.getElementById('taskTitle').focus();
    loadProviders();
}

function closeTaskForm() {
    document.getElementById('taskFormOverlay').style.display = 'none';
    editingTaskId = null;
    pendingPhotos = [];
}

async function submitTask(e) {
    e.preventDefault();
    const now = Date.now();
    const title = document.getElementById('taskTitle').value.trim();
    const desc = document.getElementById('taskDesc').value.trim();

    if (!title) return;

    if (editingIsIdea) {
        const idea = {
            title,
            description: desc,
            updated: now,
        };
        try {
            if (editingTaskId) {
                await api('/api/ideas/' + editingTaskId, { method: 'PUT', body: idea });
            } else {
                idea.status = 'open';
                idea.created = now;
                await api('/api/ideas', { method: 'POST', body: idea });
            }
            closeTaskForm();
            await loadIdeas();
            renderAll();
        } catch (e) {
            alert('Failed to save idea: ' + e.message);
        }
        return;
    }

    const task = {
        title,
        description: desc,
        type: document.getElementById('taskType').value,
        priority: document.getElementById('taskPriority').value,
        cli_provider: document.getElementById('taskProvider').value || undefined,
        auto_implement: document.getElementById('taskAutoImplement').checked,
        updated: now,
    };

    // Upload photos first
    let photoNames = [];
    if (pendingPhotos.length > 0) {
        for (const file of pendingPhotos) {
            try {
                const formData = new FormData();
                formData.append('photo', file);
                const result = await api('/api/photos/upload', { method: 'POST', body: formData });
                photoNames.push(result.filename);
            } catch (err) {
                console.warn('Photo upload failed:', err);
            }
        }
        if (photoNames.length) task.photos = photoNames;
    }

    try {
        if (editingTaskId) {
            // Preserve existing photos
            const existing = tasks.find(t => t.id === editingTaskId);
            if (existing && existing.photos) {
                task.photos = [...(existing.photos || []), ...photoNames];
            }
            await api('/api/tasks/' + editingTaskId, { method: 'PUT', body: task });
        } else {
            task.status = 'open';
            task.created = now;
            await api('/api/tasks', { method: 'POST', body: task });
        }
        closeTaskForm();
        await loadTasks();
        renderAll();
    } catch (e) {
        alert('Failed to save task: ' + e.message);
    }
}

// ========================
// PHOTOS
// ========================

function handlePhotoDrop(e) {
    e.preventDefault();
    e.currentTarget.classList.remove('dragover');
    const files = Array.from(e.dataTransfer.files).filter(f => f.type.startsWith('image/'));
    pendingPhotos.push(...files);
    renderPhotoPreviews();
}

function handlePhotoSelect(e) {
    const files = Array.from(e.target.files);
    pendingPhotos.push(...files);
    renderPhotoPreviews();
}

let _previewUrls = [];
function renderPhotoPreviews() {
    const container = document.getElementById('photoPreviews');
    if (!container) return;
    // Revoke previous ObjectURLs to prevent memory leaks
    _previewUrls.forEach(u => URL.revokeObjectURL(u));
    _previewUrls = [];
    let html = '';
    pendingPhotos.forEach((f, i) => {
        const url = URL.createObjectURL(f);
        _previewUrls.push(url);
        html += '<div class="photo-thumb">';
        html += '<img src="' + url + '" alt="">';
        html += '<button class="remove-btn" onclick="removePendingPhoto(' + i + ')">&times;</button>';
        html += '</div>';
    });
    container.innerHTML = html;
}

function removePendingPhoto(idx) {
    pendingPhotos.splice(idx, 1);
    renderPhotoPreviews();
}

function openLightbox(src) {
    document.getElementById('lightboxImg').src = src;
    document.getElementById('lightbox').style.display = 'flex';
}

function closeLightbox() {
    document.getElementById('lightbox').style.display = 'none';
}

// ========================
// TASK ACTIONS
// ========================

async function toggleTaskDone(id) {
    const task = tasks.find(t => t.id === id);
    if (!task) return;
    const newStatus = (task.status === 'done' || task.status === 'pushed-to-production') ? 'open' : 'done';
    try {
        await api('/api/tasks/' + id, { method: 'PUT', body: { status: newStatus, updated: Date.now() } });
        await loadTasks();
        renderAll();
    } catch (e) {
        console.warn('Toggle failed:', e);
    }
}

async function deleteTask(id, isIdea) {
    if (!confirm('Delete this ' + (isIdea ? 'idea' : 'task') + '?')) return;
    try {
        const path = isIdea ? '/api/ideas/' : '/api/tasks/';
        await api(path + id, { method: 'DELETE' });
        if (isIdea) await loadIdeas();
        else await loadTasks();
        closeTaskDetail();
        renderAll();
    } catch (e) {
        alert('Delete failed: ' + e.message);
    }
}

async function implementTask(id) {
    try {
        await api('/implement/' + id, { method: 'POST' });
        alert('Implementation started!');
    } catch (e) {
        // Try via trigger
        const task = tasks.find(t => t.id === id);
        if (task) {
            try {
                await api('/trigger', { method: 'POST', body: { prompt: task.title + ': ' + (task.description || '') } });
                alert('Pipeline triggered!');
            } catch (e2) {
                alert('Failed: ' + e2.message);
            }
        }
    }
}

async function startResearch(id) {
    const idea = ideas.find(i => i.id === id);
    if (!idea) return;
    try {
        await api('/research', { method: 'POST', body: { id, title: idea.title, description: idea.description || '' } });
        alert('Research started!');
    } catch (e) {
        alert('Research failed: ' + e.message);
    }
}

// ========================
// TASK DETAIL MODAL
// ========================

function openTaskDetail(id) {
    const task = tasks.find(t => t.id === id);
    if (!task) return;
    renderTaskDetail(task, false);
    document.getElementById('taskDetailOverlay').style.display = 'flex';
}

function openIdeaDetail(id) {
    const idea = ideas.find(i => i.id === id);
    if (!idea) return;
    renderTaskDetail(idea, true);
    document.getElementById('taskDetailOverlay').style.display = 'flex';
}

function renderTaskDetail(item, isIdea) {
    const modal = document.getElementById('taskDetailModal');
    let html = '<div class="modal-header">';
    html += '<h2>' + escHtml(item.title) + '</h2>';
    html += '<button class="btn btn-icon modal-close" onclick="closeTaskDetail()">&times;</button>';
    html += '</div>';

    // Badges
    html += '<div class="task-badges mb-8">';
    if (!isIdea && item.type) html += '<span class="badge type-' + item.type + '">' + getTypeIcon(item.type) + '</span>';
    html += '<span class="badge ' + getStatusClass(item.status) + '">' + getStatusLabel(item.status) + '</span>';
    if (item.priority) html += '<span class="badge badge-' + (item.priority === 'high' ? 'danger' : item.priority === 'low' ? 'success' : 'warning') + '">' + escHtml(item.priority) + '</span>';
    html += '</div>';

    // Description
    if (item.description) {
        html += '<div class="detail-section"><h3>Description</h3>';
        html += '<div class="plan-content">' + renderMarkdown(item.description) + '</div></div>';
    }

    // Plan
    if (item.plan) {
        html += '<div class="detail-section"><h3>Plan</h3>';
        html += '<div class="plan-content">' + renderMarkdown(item.plan) + '</div></div>';
    }

    // Pipeline Summary
    if (item.pipeline_summary) {
        html += renderPipelineSummary(item.pipeline_summary);
    }

    // Photos (tasks only)
    if (!isIdea && item.photos && item.photos.length) {
        html += '<div class="detail-section"><h3>Photos</h3><div class="photo-previews">';
        item.photos.forEach(p => {
            html += '<div class="photo-thumb" onclick="openLightbox(\'/photos/' + escHtml(p) + '\')">';
            html += '<img src="/photos/' + escHtml(p) + '" alt="" loading="lazy">';
            html += '</div>';
        });
        html += '</div></div>';
    }

    // Research (ideas only)
    if (isIdea && item.research) {
        html += '<div class="detail-section"><h3>Research</h3>';
        html += '<div class="plan-content">' + renderMarkdown(item.research) + '</div></div>';
    }

    // AI Analysis
    if (item.ai_analysis) {
        html += '<div class="detail-section"><h3>AI Analysis</h3>';
        html += '<div class="plan-content">' + renderMarkdown(item.ai_analysis) + '</div></div>';
    }

    // Actions
    html += '<div class="form-actions mt-16">';
    html += '<button class="btn btn-secondary" onclick="openTaskForm(\'' + escHtml(item.id) + '\')">Edit</button>';
    if (isIdea) {
        if (item.status === 'open') html += '<button class="btn btn-primary" onclick="startResearch(\'' + escHtml(item.id) + '\')">Research</button>';
        if (item.status === 'planned') html += '<button class="btn btn-primary" onclick="implementTask(\'' + escHtml(item.id) + '\')">Implement</button>';
    } else {
        if (item.status === 'open') html += '<button class="btn btn-primary" onclick="implementTask(\'' + escHtml(item.id) + '\')">Run Pipeline</button>';
        if (item.status === 'planned') html += '<button class="btn btn-primary" onclick="implementTask(\'' + escHtml(item.id) + '\')">Implement</button>';
    }
    html += '<button class="btn btn-danger" onclick="deleteTask(\'' + escHtml(item.id) + '\',' + isIdea + ')">Delete</button>';
    html += '</div>';

    // Timestamps
    html += '<div class="text-sm text-muted mt-8">';
    html += 'Created: ' + (item.created ? new Date(item.created).toLocaleString() : '--');
    html += ' | Updated: ' + (item.updated ? new Date(item.updated).toLocaleString() : '--');
    if (item.id) html += ' | ID: ' + escHtml(item.id);
    html += '</div>';

    modal.innerHTML = html;
}

function renderPipelineSummary(summary) {
    let html = '<div class="detail-section"><h3>Pipeline Summary</h3>';
    html += '<div class="pipeline-content">';

    if (summary.elapsed) html += '<div>Total time: <strong>' + summary.elapsed.toFixed(1) + 's</strong></div>';
    html += '<div>Published: ' + (summary.published ? 'Yes' : 'No') + '</div>';

    // Stage timeline
    if (summary.stages && summary.stages.length) {
        html += '<div class="pipeline-stages mt-8">';
        summary.stages.forEach(s => {
            let cls = 'pipeline-stage';
            if (s.verdict === 'approve' || s.verdict === 'done' || s.verdict === 'direct') cls += ' done';
            else if (s.verdict === 'revise') cls += ' failed';
            html += '<div class="' + cls + '">';
            html += '<div class="stage-name">' + escHtml(s.stage) + '</div>';
            html += '<div class="stage-time">' + (s.elapsed || 0).toFixed(1) + 's</div>';
            if (s.verdict) html += '<div class="stage-verdict badge badge-' + (s.verdict === 'approve' || s.verdict === 'done' || s.verdict === 'direct' ? 'success' : 'warning') + '">' + escHtml(s.verdict) + '</div>';
            if (s.issues && s.issues.length) {
                html += '<div class="text-sm text-muted mt-4">' + s.issues.length + ' issue(s)</div>';
            }
            html += '</div>';
        });
        html += '</div>';
    }

    html += '</div></div>';
    return html;
}

function closeTaskDetail() {
    document.getElementById('taskDetailOverlay').style.display = 'none';
}

// ========================
// AGENT STATUS
// ========================

async function fetchAgentStatus() {
    try {
        const data = await api('/status');
        renderAgentStatusBar(data);
        // Update pipeline overview dots
        if (data.current_stage) {
            const activeIdx = PIPELINE_STAGES.indexOf(data.current_stage);
            PIPELINE_STAGES.forEach((s, i) => {
                const dot = document.getElementById('dot-' + s);
                if (dot) {
                    dot.className = 'pipeline-dot' + (i < activeIdx ? ' completed' : i === activeIdx ? ' active' : '');
                }
            });
        }
        return data;
    } catch (e) {
        return null;
    }
}

function renderAgentStatusBar(data) {
    const el = document.getElementById('agentStatus');
    const apiEl = document.getElementById('apiCounter');
    if (el) {
        let text = 'Agent: ' + (data.state || 'unknown');
        if (data.current_task) text += ' - ' + data.current_task;
        if (data.current_stage) text += ' [' + data.current_stage + ']';
        el.textContent = text;
    }
    if (apiEl) {
        apiEl.textContent = 'API: ' + (data.api_calls_today || 0) + '/' + (data.api_limit || 10);
    }
}

async function triggerAgent() {
    try {
        await api('/trigger', { method: 'POST', body: {} });
    } catch (e) {
        console.warn('Trigger failed:', e);
    }
}

// ========================
// PROVIDERS DROPDOWN
// ========================

async function loadProviders() {
    try {
        const data = await api('/status');
        const select = document.getElementById('taskProvider');
        if (!select || !data.providers) return;
        // Keep the default option, clear the rest
        while (select.options.length > 1) select.remove(1);
        for (const [key, name] of Object.entries(data.providers)) {
            const opt = document.createElement('option');
            opt.value = key;
            opt.textContent = name;
            select.appendChild(opt);
        }
    } catch (e) {}
}

// ========================
// SECURITY REPORT
// ========================

async function viewSecurityReport(taskId) {
    try {
        const data = await api('/security-report/' + taskId);
        alert(data.report || 'No report available');
    } catch (e) {
        alert('No security report found');
    }
}

// ========================
// POLLING
// ========================

function hasActiveTasks() {
    return tasks.some(t => t.status === 'in-progress' || t.status === 'grabbed');
}

async function pollData() {
    await Promise.all([loadTasks(), loadIdeas()]);
    renderAll();
    const data = await fetchAgentStatus();
    const interval = hasActiveTasks() || (data && data.state && data.state.startsWith('pipeline:')) ? 5000 : 30000;
    pollTimer = setTimeout(pollData, interval);
}

// ========================
// INIT
// ========================

(async function init() {
    await Promise.all([loadTasks(), loadIdeas()]);
    renderAll();
    fetchAgentStatus();
    pollTimer = setTimeout(pollData, 5000);
})();
