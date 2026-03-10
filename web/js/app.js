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
    // Headers
    html = html.replace(/^### (.+)$/gm, '<h4>$1</h4>');
    html = html.replace(/^## (.+)$/gm, '<h3>$1</h3>');
    html = html.replace(/^# (.+)$/gm, '<h2>$1</h2>');
    // Bold / italic
    html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
    // Code
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
    // Code blocks
    html = html.replace(/```[\s\S]*?```/g, m => {
        const code = m.replace(/```\w*\n?/, '').replace(/\n?```$/, '');
        return '<pre><code>' + code + '</code></pre>';
    });
    // Lists
    html = html.replace(/^[-*] (.+)$/gm, '<li>$1</li>');
    html = html.replace(/(<li>.*<\/li>\n?)+/g, '<ul>$&</ul>');
    // Line breaks
    html = html.replace(/\n/g, '<br>');
    // Clean up
    html = html.replace(/<br><\/ul>/g, '</ul>');
    html = html.replace(/<ul><br>/g, '<ul>');
    // Remove <br> inside <pre> blocks (newlines are preserved by CSS white-space)
    html = html.replace(/<pre><code>([\s\S]*?)<\/code><\/pre>/g, (m, code) =>
        '<pre><code>' + code.replace(/<br>/g, '\n') + '</code></pre>');
    return html;
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

