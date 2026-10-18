(() => {
  'use strict';

  const body = document.body;
  const authenticated = body.dataset.authenticated === 'true';
  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || '';
  const feed = document.getElementById('messageFeed');
  const emptyState = document.getElementById('emptyState');
  const historyLoader = document.getElementById('loadingHistory');
  const olderRow = document.getElementById('historyLoaderRow');
  const olderButton = document.getElementById('loadEarlier');
  const form = document.getElementById('chatForm');
  const input = document.getElementById('messageInput');
  const sendButton = document.getElementById('sendButton');
  const logoutForm = document.getElementById('logoutForm');
  const sidebar = document.getElementById('sidebar');
  const mobileMenu = document.getElementById('mobileMenu');
  const mobileScrim = document.getElementById('mobileScrim');

  let historyCursor = null;
  let hasOlder = false;
  let busy = false;

  function showToast(message, isError = false) {
    const toast = document.createElement('div');
    toast.className = `toast${isError ? ' error' : ''}`;
    toast.setAttribute('role', isError ? 'alert' : 'status');
    toast.textContent = message;
    document.body.appendChild(toast);
    window.setTimeout(() => toast.remove(), 5200);
  }

  function timestamp(value) {
    if (!value) return '';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '';
    return new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' }).format(date);
  }

  function makeMessage(role, text, timeValue) {
    const assistant = role === 'assistant';
    const row = document.createElement('article');
    row.className = `message-row ${assistant ? 'assistant' : 'user'}`;

    if (assistant) {
      const avatar = document.createElement('div');
      avatar.className = 'message-avatar';
      avatar.setAttribute('aria-hidden', 'true');
      const image = document.createElement('img');
      image.src = '/static/aurrexa-mark.jpeg';
      image.alt = '';
      avatar.appendChild(image);
      row.appendChild(avatar);
    }

    const content = document.createElement('div');
    content.className = 'message-content';
    const meta = document.createElement('div');
    meta.className = 'message-meta';
    const roleLabel = document.createElement('span');
    roleLabel.className = 'message-role';
    roleLabel.textContent = assistant ? 'AURREXA' : 'YOU';
    const time = document.createElement('time');
    time.className = 'message-time';
    time.textContent = timestamp(timeValue);
    meta.append(roleLabel, time);

    const bubble = document.createElement('div');
    bubble.className = 'message-bubble';
    bubble.textContent = text;
    content.append(meta, bubble);
    row.appendChild(content);

    if (!assistant) {
      const avatar = document.createElement('div');
      avatar.className = 'message-avatar user-avatar';
      avatar.textContent = 'YOU';
      avatar.setAttribute('aria-hidden', 'true');
      row.appendChild(avatar);
    }
    return row;
  }

  function makeTyping() {
    const row = document.createElement('article');
    row.className = 'message-row assistant pending';
    row.id = 'typingIndicator';
    row.setAttribute('aria-label', 'AURREXA is preparing a response');
    const avatar = document.createElement('div');
    avatar.className = 'message-avatar';
    avatar.setAttribute('aria-hidden', 'true');
    const image = document.createElement('img');
    image.src = '/static/aurrexa-mark.jpeg';
    image.alt = '';
    avatar.appendChild(image);
    const content = document.createElement('div');
    content.className = 'message-content';
    const bubble = document.createElement('div');
    bubble.className = 'message-bubble';
    for (let index = 0; index < 3; index += 1) {
      const dot = document.createElement('span');
      dot.className = 'typing-dot';
      bubble.appendChild(dot);
    }
    content.appendChild(bubble);
    row.append(avatar, content);
    return row;
  }

  function syncEmptyState() {
    if (!emptyState || !feed) return;
    const hasMessages = feed.querySelector('.message-row') !== null;
    emptyState.hidden = hasMessages;
  }

  function appendPair(item) {
    if (!feed) return;
    feed.append(makeMessage('user', item.user_message, item.created_at));
    feed.append(makeMessage('assistant', item.ai_response, item.created_at));
  }

  async function loadHistory(beforeId = null, prepend = false) {
    if (!feed) return;
    const query = new URLSearchParams({ limit: '100' });
    if (beforeId) query.set('before_id', String(beforeId));
    try {
      const response = await fetch(`/api/history?${query.toString()}`, {
        headers: { Accept: 'application/json' },
        credentials: 'same-origin',
      });
      if (response.status === 401) {
        window.location.replace('/');
        return;
      }
      if (!response.ok) throw new Error('History could not be loaded.');
      const data = await response.json();
      const oldHeight = feed.scrollHeight;
      const oldTop = feed.scrollTop;
      const fragment = document.createDocumentFragment();
      for (const item of data.items || []) {
        fragment.append(makeMessage('user', item.user_message, item.created_at));
        fragment.append(makeMessage('assistant', item.ai_response, item.created_at));
      }
      if (prepend) feed.prepend(fragment);
      else feed.append(fragment);
      historyCursor = data.next_before_id || null;
      hasOlder = Boolean(data.has_more && historyCursor);
      if (olderRow) olderRow.hidden = !hasOlder;
      if (prepend) feed.scrollTop = feed.scrollHeight - oldHeight + oldTop;
      else feed.scrollTop = feed.scrollHeight;
      syncEmptyState();
    } catch (error) {
      showToast(error.message || 'History could not be loaded.', true);
      syncEmptyState();
    } finally {
      if (historyLoader) historyLoader.remove();
      if (feed && !feed.querySelector('.message-row')) syncEmptyState();
    }
  }

  if (authenticated && feed) loadHistory();

  if (olderButton) {
    olderButton.addEventListener('click', async () => {
      if (hasOlder && historyCursor) await loadHistory(historyCursor, true);
    });
  }

  if (form && input && sendButton) {
    const resizeInput = () => {
      input.style.height = 'auto';
      input.style.height = `${Math.min(input.scrollHeight, 180)}px`;
    };
    input.addEventListener('input', resizeInput);
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        form.requestSubmit();
      }
    });

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (busy) return;
      const message = input.value.trim();
      if (!message) {
        input.focus();
        return;
      }
      if (message.length > 8000) {
        showToast('Messages are limited to 8,000 characters.', true);
        return;
      }

      busy = true;
      sendButton.disabled = true;
      input.disabled = true;
      if (emptyState) emptyState.hidden = true;
      const optimistic = makeMessage('user', message, new Date().toISOString());
      const typing = makeTyping();
      feed.append(optimistic, typing);
      feed.scrollTop = feed.scrollHeight;
      input.value = '';
      input.style.height = 'auto';

      try {
        const response = await fetch('/api/chat', {
          method: 'POST',
          credentials: 'same-origin',
          headers: {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'X-CSRFToken': csrfToken,
          },
          body: JSON.stringify({ message }),
        });
        const data = await response.json().catch(() => ({}));
        typing.remove();
        if (response.status === 401) {
          window.location.replace('/');
          return;
        }
        if (!response.ok) {
          optimistic.remove();
          showToast(data.error || 'AURREXA could not complete that response.', true);
          syncEmptyState();
          return;
        }
        feed.append(makeMessage('assistant', data.ai_response, data.created_at));
        feed.scrollTop = feed.scrollHeight;
      } catch (_error) {
        typing.remove();
        optimistic.remove();
        showToast('AURREXA could not reach the server. Check your connection and try again.', true);
        syncEmptyState();
      } finally {
        busy = false;
        sendButton.disabled = false;
        input.disabled = false;
        input.focus();
      }
    });
  }

  document.querySelectorAll('[data-prompt]').forEach((button) => {
    button.addEventListener('click', () => {
      if (!input) return;
      input.value = button.dataset.prompt || '';
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.focus();
    });
  });

  if (logoutForm) {
    logoutForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      const button = document.getElementById('logoutButton');
      if (button) button.disabled = true;
      try {
        const response = await fetch('/logout', {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'X-CSRFToken': csrfToken, 'Accept': 'text/html' },
        });
        if (!response.ok) throw new Error('Logout could not be completed.');
        window.location.replace('/');
      } catch (_error) {
        if (button) button.disabled = false;
        showToast('Logout could not be completed. Please try again.', true);
      }
    });
  }

  function setSidebarOpen(open) {
    if (!sidebar || !mobileMenu) return;
    sidebar.classList.toggle('is-open', open);
    mobileMenu.setAttribute('aria-expanded', String(open));
    mobileMenu.setAttribute('aria-label', open ? 'Close navigation' : 'Open navigation');
    if (mobileScrim) mobileScrim.hidden = !open;
  }

  if (mobileMenu) mobileMenu.addEventListener('click', () => setSidebarOpen(!sidebar?.classList.contains('is-open')));
  if (mobileScrim) mobileScrim.addEventListener('click', () => setSidebarOpen(false));
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') setSidebarOpen(false);
  });
})();
