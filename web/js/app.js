// Task-Claw shared utilities

const API_BASE = '';

async function api(path, opts = {}) {
    const url = API_BASE + path;
    const defaults = { headers: { 'Content-Type': 'application/json' } };
    if (opts.body && typeof opts.body === 'object' && !(opts.body instanceof FormData)) {
        opts.body = JSON.stringify(opts.body);
    }
    if (opts.body instanceof FormData) {
        delete defaults.headers['Content-Type'];
    }
    const res = await fetch(url, { ...defaults, ...opts });
    if (!res.ok) {
        const err = await res.json().catch(() => ({ error: res.statusText }));
        throw new Error(err.error || res.statusText);
    }
    return res.json();
}

function escHtml(s) {
    if (!s) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function renderMarkdown(text) {
    if (!text) return '';
    let html = escHtml(text);
    // Extract code blocks first to protect them from inline transforms
    const codeBlocks = [];
    html = html.replace(/```[\s\S]*?```/g, m => {
        const code = m.replace(/```\w*\n?/, '').replace(/\n?```$/, '');
        codeBlocks.push('<pre><code>' + code + '</code></pre>');
        return '\x00CB' + (codeBlocks.length - 1) + '\x00';
    });
    // Extract inline code
    html = html.replace(/`([^`]+)`/g, (m, code) => {
        codeBlocks.push('<code>' + code + '</code>');
        return '\x00CB' + (codeBlocks.length - 1) + '\x00';
    });
    // Headers
    html = html.replace(/^### (.+)$/gm, '<h4>$1</h4>');
    html = html.replace(/^## (.+)$/gm, '<h3>$1</h3>');
    html = html.replace(/^# (.+)$/gm, '<h2>$1</h2>');
    // Bold / italic
    html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
    // Lists
    html = html.replace(/^[-*] (.+)$/gm, '<li>$1</li>');
    html = html.replace(/(<li>.*<\/li>\n?)+/g, '<ul>$&</ul>');
    // Line breaks
    html = html.replace(/\n/g, '<br>');
    // Clean up
    html = html.replace(/<br><\/ul>/g, '</ul>');
    html = html.replace(/<ul><br>/g, '<ul>');
    // Restore code blocks (they keep original newlines, no <br> injection)
    html = html.replace(/\x00CB(\d+)\x00/g, (m, idx) => codeBlocks[parseInt(idx)]);
    return html;
}

// ========================
// TOAST NOTIFICATIONS
// ========================

const TOAST_ICONS = {
    success: '<svg class="toast-icon" viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd"/></svg>',
    error: '<svg class="toast-icon" viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clip-rule="evenodd"/></svg>',
    warning: '<svg class="toast-icon" viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clip-rule="evenodd"/></svg>',
    info: '<svg class="toast-icon" viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7-4a1 1 0 11-2 0 1 1 0 012 0zM9 9a1 1 0 000 2v3a1 1 0 001 1h1a1 1 0 100-2v-3a1 1 0 00-1-1H9z" clip-rule="evenodd"/></svg>'
};

function _getToastContainer() {
    let c = document.getElementById('toastContainer');
    if (!c) {
        c = document.createElement('div');
        c.id = 'toastContainer';
        c.className = 'toast-container';
        document.body.appendChild(c);
    }
    return c;
}

function showToast(message, type) {
    type = type || 'info';
    const container = _getToastContainer();
    const toast = document.createElement('div');
    toast.className = 'toast toast-' + type;
    toast.innerHTML = (TOAST_ICONS[type] || '') + '<span class="toast-message">' + escHtml(message) + '</span>';
    toast.addEventListener('click', function() { dismissToast(toast); });
    container.appendChild(toast);
    const duration = type === 'error' ? 5000 : 3500;
    setTimeout(function() { dismissToast(toast); }, duration);
}

function dismissToast(toast) {
    if (toast.classList.contains('removing')) return;
    toast.classList.add('removing');
    setTimeout(function() { toast.remove(); }, 200);
}

// ========================
// CONFIRM DIALOG
// ========================

function showConfirm(message) {
    return new Promise(function(resolve) {
        const overlay = document.createElement('div');
        overlay.className = 'confirm-overlay';
        const dialog = document.createElement('div');
        dialog.className = 'confirm-dialog';
        dialog.innerHTML = '<p>' + escHtml(message) + '</p>' +
            '<div class="confirm-actions">' +
            '<button class="btn btn-secondary" id="_confirmNo">Cancel</button>' +
            '<button class="btn btn-danger" id="_confirmYes">Confirm</button>' +
            '</div>';
        overlay.appendChild(dialog);
        document.body.appendChild(overlay);

        function cleanup(result) {
            overlay.remove();
            resolve(result);
        }

        dialog.querySelector('#_confirmYes').addEventListener('click', function() { cleanup(true); });
        dialog.querySelector('#_confirmNo').addEventListener('click', function() { cleanup(false); });
        overlay.addEventListener('click', function(e) { if (e.target === overlay) cleanup(false); });
    });
}

// ========================
// EMPTY STATE HELPERS
// ========================

const EMPTY_ICONS = {
    tasks: '<svg class="empty-state-icon" viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="8" y="6" width="32" height="36" rx="3"/><path d="M16 16h16M16 24h12M16 32h8"/><path d="M19 6V3h10v3"/></svg>',
    ideas: '<svg class="empty-state-icon" viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="24" cy="20" r="12"/><path d="M19 32v4h10v-4"/><path d="M22 36v3h4v-3"/><path d="M24 8v-4M36 20h4M8 20h-4M32.5 11.5l2.8-2.8M15.5 11.5l-2.8-2.8"/></svg>',
    pipeline: '<svg class="empty-state-icon" viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="10" cy="24" r="4"/><circle cx="24" cy="24" r="4"/><circle cx="38" cy="24" r="4"/><path d="M14 24h6M28 24h6"/></svg>',
    history: '<svg class="empty-state-icon" viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="24" cy="24" r="16"/><path d="M24 14v10l7 7"/><path d="M8 24H4M44 24h-4"/></svg>',
    skills: '<svg class="empty-state-icon" viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M28 6l-8 42M16 14L6 24l10 10M32 14l10 10-10 10"/></svg>',
    providers: '<svg class="empty-state-icon" viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="6" y="10" width="36" height="28" rx="3"/><circle cx="16" cy="24" r="3"/><circle cx="32" cy="24" r="3"/><path d="M24 18v12"/></svg>'
};

function emptyState(icon, title, subtitle) {
    return '<div class="empty-state">' +
        (EMPTY_ICONS[icon] || '') +
        '<h4>' + escHtml(title) + '</h4>' +
        (subtitle ? '<p>' + escHtml(subtitle) + '</p>' : '') +
        '</div>';
}

function timeAgo(dateStr) {
    if (!dateStr) return '--';
    const date = typeof dateStr === 'number' ? new Date(dateStr) : new Date(dateStr);
    const now = new Date();
    const diffMs = now - date;
    const diffS = Math.floor(diffMs / 1000);
    if (diffS < 60) return 'just now';
    const diffM = Math.floor(diffS / 60);
    if (diffM < 60) return diffM + 'm ago';
    const diffH = Math.floor(diffM / 60);
    if (diffH < 24) return diffH + 'h ago';
    const diffD = Math.floor(diffH / 24);
    return diffD + 'd ago';
}

