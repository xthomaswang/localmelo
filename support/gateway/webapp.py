"""Minimal chat web UI served on the gateway."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>localmelo</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }

:root {
  --bg: #f9fafb;
  --surface: #ffffff;
  --border: #e5e7eb;
  --text: #111827;
  --text-secondary: #6b7280;
  --user-bg: #2563eb;
  --user-text: #ffffff;
  --assistant-bg: #f3f4f6;
  --accent: #2563eb;
  --code-bg: #1e1e2e;
  --code-text: #cdd6f4;
  --input-bg: #ffffff;
  --shadow: 0 1px 3px rgba(0,0,0,0.08);
}

body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
  background: var(--bg);
  color: var(--text);
  height: 100dvh;
  display: flex;
  flex-direction: column;
}

header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 20px;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
}
header h1 { font-size: 16px; font-weight: 600; letter-spacing: -0.01em; }
#new-chat-btn {
  padding: 6px 14px; font-size: 13px;
  border: 1px solid var(--border); border-radius: 6px;
  background: var(--surface); color: var(--text);
  cursor: pointer; transition: background 0.15s;
}
#new-chat-btn:hover { background: var(--bg); }

#messages {
  flex: 1; overflow-y: auto; padding: 24px 0; scroll-behavior: smooth;
}
.msg-row {
  max-width: 720px; margin: 0 auto 24px; padding: 0 20px;
  display: flex; animation: fadeIn 0.2s ease;
}
@keyframes fadeIn {
  from { opacity: 0; transform: translateY(6px); }
  to { opacity: 1; transform: translateY(0); }
}
.msg-row.user { justify-content: flex-end; }
.msg-row.assistant { justify-content: flex-start; }

.msg-bubble {
  max-width: 85%; padding: 10px 16px; border-radius: 16px;
  line-height: 1.55; font-size: 14.5px;
  word-wrap: break-word; overflow-wrap: break-word;
}
.user .msg-bubble {
  background: var(--user-bg); color: var(--user-text);
  border-bottom-right-radius: 4px;
}
.assistant .msg-bubble {
  background: var(--assistant-bg); color: var(--text);
  border-bottom-left-radius: 4px;
}
.assistant .msg-bubble p { margin-bottom: 8px; }
.assistant .msg-bubble p:last-child { margin-bottom: 0; }
.assistant .msg-bubble code {
  background: var(--code-bg); color: var(--code-text);
  padding: 1px 5px; border-radius: 4px; font-size: 13px;
  font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;
}
.assistant .msg-bubble pre {
  background: var(--code-bg); color: var(--code-text);
  padding: 14px 16px; border-radius: 8px; overflow-x: auto;
  margin: 8px 0; font-size: 13px; line-height: 1.5;
}
.assistant .msg-bubble pre code {
  background: none; padding: 0; font-size: inherit;
}
.assistant .msg-bubble ul, .assistant .msg-bubble ol {
  padding-left: 20px; margin: 6px 0;
}
.assistant .msg-bubble li { margin-bottom: 4px; }
.assistant .msg-bubble strong { font-weight: 600; }

.thinking { display: flex; gap: 4px; padding: 12px 16px; }
.thinking span {
  width: 7px; height: 7px; background: var(--text-secondary);
  border-radius: 50%; animation: bounce 1.2s infinite;
}
.thinking span:nth-child(2) { animation-delay: 0.15s; }
.thinking span:nth-child(3) { animation-delay: 0.3s; }
@keyframes bounce {
  0%, 60%, 100% { transform: translateY(0); opacity: 0.4; }
  30% { transform: translateY(-6px); opacity: 1; }
}

#welcome {
  flex: 1; display: flex; align-items: center; justify-content: center;
  text-align: center; padding: 40px 20px;
}
#welcome h2 { font-size: 22px; font-weight: 600; margin-bottom: 8px; }
#welcome p { color: var(--text-secondary); font-size: 14px; }

#input-area { padding: 16px 20px 24px; background: var(--bg); }
#input-wrap {
  max-width: 720px; margin: 0 auto; display: flex; align-items: flex-end; gap: 8px;
  background: var(--input-bg); border: 1px solid var(--border);
  border-radius: 14px; padding: 8px 8px 8px 16px;
  box-shadow: var(--shadow); transition: border-color 0.15s;
}
#input-wrap:focus-within { border-color: var(--accent); }
#user-input {
  flex: 1; border: none; outline: none; resize: none;
  font-size: 14.5px; font-family: inherit; line-height: 1.5;
  max-height: 160px; min-height: 24px;
  background: transparent; color: var(--text);
}
#user-input::placeholder { color: var(--text-secondary); }
#send-btn {
  width: 36px; height: 36px; border-radius: 10px; border: none;
  background: var(--accent); color: white; cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0; transition: opacity 0.15s;
}
#send-btn:disabled { opacity: 0.4; cursor: not-allowed; }
#send-btn svg { width: 18px; height: 18px; }
#footer-note {
  text-align: center; font-size: 11px; color: var(--text-secondary); margin-top: 8px;
}
</style>
</head>
<body>

<header>
  <h1>localmelo</h1>
  <button id="new-chat-btn">New Chat</button>
</header>

<div id="welcome">
  <div>
    <h2>localmelo</h2>
    <p>Your local AI agent. Ask anything to get started.</p>
  </div>
</div>

<div id="messages" style="display:none;"></div>

<div id="input-area">
  <div id="input-wrap">
    <textarea id="user-input" rows="1" placeholder="Send a message..." autocomplete="off"></textarea>
    <button id="send-btn" disabled>
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
           stroke-linecap="round" stroke-linejoin="round">
        <line x1="12" y1="19" x2="12" y2="5"></line>
        <polyline points="5 12 12 5 19 12"></polyline>
      </svg>
    </button>
  </div>
  <div id="footer-note">Responses are generated by a local model.</div>
</div>

<script>
const msgContainer = document.getElementById('messages');
const welcome = document.getElementById('welcome');
const userInput = document.getElementById('user-input');
const sendBtn = document.getElementById('send-btn');
const newChatBtn = document.getElementById('new-chat-btn');

let sessionId = null;
let sending = false;

userInput.addEventListener('input', function() {
  this.style.height = 'auto';
  this.style.height = Math.min(this.scrollHeight, 160) + 'px';
  sendBtn.disabled = !this.value.trim() || sending;
});

userInput.addEventListener('keydown', function(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    if (this.value.trim() && !sending) send();
  }
});

sendBtn.addEventListener('click', send);

newChatBtn.addEventListener('click', function() {
  sessionId = null;
  msgContainer.innerHTML = '';
  msgContainer.style.display = 'none';
  welcome.style.display = 'flex';
  userInput.focus();
});

function renderMarkdown(text) {
  var html = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');

  // Code blocks
  html = html.replace(/```(\w*)\n([\s\S]*?)```/g, function(m, lang, code) {
    return '<pre><code>' + code.trimEnd() + '</code></pre>';
  });

  // Inline code
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');

  // Bold
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');

  // Lists
  html = html.replace(/(^|\n)([-*] .+(?:\n[-*] .+)*)/g, function(m, pre, block) {
    var items = block.split('\n').map(function(l) {
      return '<li>' + l.replace(/^[-*] /, '') + '</li>';
    }).join('');
    return pre + '<ul>' + items + '</ul>';
  });

  html = html.replace(/(^|\n)(\d+\. .+(?:\n\d+\. .+)*)/g, function(m, pre, block) {
    var items = block.split('\n').map(function(l) {
      return '<li>' + l.replace(/^\d+\. /, '') + '</li>';
    }).join('');
    return pre + '<ol>' + items + '</ol>';
  });

  // Paragraphs
  html = html.replace(/\n{2,}/g, '</p><p>');
  html = '<p>' + html + '</p>';
  html = html.replace(/<p>\s*<(pre|ul|ol)/g, '<$1');
  html = html.replace(/<\/(pre|ul|ol)>\s*<\/p>/g, '</$1>');
  html = html.replace(/<p><\/p>/g, '');

  return html;
}

function appendMessage(role, content) {
  var row = document.createElement('div');
  row.className = 'msg-row ' + role;
  var bubble = document.createElement('div');
  bubble.className = 'msg-bubble';
  if (role === 'assistant') {
    bubble.innerHTML = renderMarkdown(content);
  } else {
    bubble.textContent = content;
  }
  row.appendChild(bubble);
  msgContainer.appendChild(row);
  msgContainer.scrollTop = msgContainer.scrollHeight;
}

function showThinking() {
  var row = document.createElement('div');
  row.className = 'msg-row assistant';
  row.id = 'thinking';
  row.innerHTML = '<div class="msg-bubble thinking"><span></span><span></span><span></span></div>';
  msgContainer.appendChild(row);
  msgContainer.scrollTop = msgContainer.scrollHeight;
}

function hideThinking() {
  var el = document.getElementById('thinking');
  if (el) el.remove();
}

function stripThink(text) {
  return text.replace(/<think>[\s\S]*?<\/think>\s*/gi, '').trim();
}

function send() {
  var query = userInput.value.trim();
  if (!query || sending) return;

  welcome.style.display = 'none';
  msgContainer.style.display = 'block';

  sending = true;
  sendBtn.disabled = true;
  userInput.value = '';
  userInput.style.height = 'auto';

  appendMessage('user', query);
  showThinking();

  var payload = { query: query };
  if (sessionId) payload.session_id = sessionId;

  fetch('/v1/agent/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  })
  .then(function(res) {
    if (!res.ok) {
      return res.json().catch(function() { return {}; }).then(function(err) {
        throw new Error(err.error || res.statusText);
      });
    }
    return res.json();
  })
  .then(function(data) {
    hideThinking();
    sessionId = data.session_id;
    var cleaned = stripThink(data.result || '');
    appendMessage('assistant', cleaned || '(empty response)');
  })
  .catch(function(e) {
    hideThinking();
    appendMessage('assistant', 'Error: ' + e.message);
  })
  .finally(function() {
    sending = false;
    sendBtn.disabled = !userInput.value.trim();
    userInput.focus();
  });
}

userInput.focus();
</script>
</body>
</html>"""


def mount(app: FastAPI) -> None:
    """Register the chat web UI route on the gateway app."""

    @app.get("/", response_class=HTMLResponse)
    async def chat_ui() -> str:
        return _HTML
