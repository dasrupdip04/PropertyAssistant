// PropertyAssistant - Client-Side Chat Engine
// Vanilla JavaScript, Mobile-First, Strict DOM-based XSS prevention

(function () {
  'use strict';

  // --- State & DOM References ---
  const STORAGE_KEY = 'pa_session_id';
  let sessionId = localStorage.getItem(STORAGE_KEY);
  if (!sessionId) {
    sessionId = (typeof crypto.randomUUID === 'function')
      ? crypto.randomUUID()
      : 'sess-' + Math.random().toString(36).substring(2, 15) + Math.random().toString(36).substring(2, 15);
    localStorage.setItem(STORAGE_KEY, sessionId);
  }

  let isSubmitting = false;
  let lastFailedMessage = null;

  const chatStream = document.getElementById('chat-stream');
  const chatForm = document.getElementById('chat-form');
  const userInput = document.getElementById('user-input');
  const btnSend = document.getElementById('btn-send');
  const charCounter = document.getElementById('char-counter');
  const btnNewSearch = document.getElementById('btn-new-search');
  const filterBar = document.getElementById('filter-bar');
  const filterChips = document.getElementById('filter-chips');
  const errorToast = document.getElementById('error-toast');
  const errorMessage = document.getElementById('error-message');
  const btnRetry = document.getElementById('btn-retry');
  const btnDismissError = document.getElementById('btn-dismiss-error');

  // --- Utility Functions ---
  function sanitizeText(str) {
    return str || '';
  }

  function scrollChatToBottom() {
    chatStream.scrollTop = chatStream.scrollHeight;
  }

  function updateCharCount() {
    const len = userInput.value.length;
    charCounter.textContent = `${len} / 1000`;
    if (len > 1000) {
      charCounter.classList.add('limit-warn');
      btnSend.disabled = true;
    } else {
      charCounter.classList.remove('limit-warn');
      btnSend.disabled = isSubmitting || len === 0 || !userInput.value.trim();
    }
  }

  function autoResizeInput() {
    userInput.style.height = 'auto';
    const newHeight = Math.min(userInput.scrollHeight, 120);
    userInput.style.height = newHeight + 'px';
  }

  function showError(msg, canRetry = true) {
    errorMessage.textContent = msg;
    errorToast.classList.remove('hidden');
    btnRetry.style.display = canRetry ? 'inline-flex' : 'none';
  }

  function hideError() {
    errorToast.classList.add('hidden');
    errorMessage.textContent = '';
  }

  // --- Message Element Builders (Strict DOM methods, NO innerHTML) ---

  function appendUserBubble(text) {
    const row = document.createElement('div');
    row.className = 'message-row user';

    const bubble = document.createElement('div');
    bubble.className = 'user-bubble';
    bubble.textContent = sanitizeText(text);

    row.appendChild(bubble);
    chatStream.appendChild(row);
    scrollChatToBottom();
    return row;
  }

  function createTypingIndicator() {
    const row = document.createElement('div');
    row.className = 'message-row assistant';
    row.id = 'typing-row';

    const container = document.createElement('div');
    container.className = 'assistant-container';

    const indicator = document.createElement('div');
    indicator.className = 'typing-indicator';

    for (let i = 0; i < 3; i++) {
      const dot = document.createElement('span');
      dot.className = 'typing-dot';
      indicator.appendChild(dot);
    }

    container.appendChild(indicator);
    row.appendChild(container);
    return row;
  }

  function removeTypingIndicator() {
    const typing = document.getElementById('typing-row');
    if (typing) {
      typing.remove();
    }
  }

  function buildPropertyCard(card, index) {
    const cardEl = document.createElement('div');
    cardEl.className = 'property-card';

    // Header: index, project name, availability badge
    const headerEl = document.createElement('div');
    headerEl.className = 'card-header';

    const indexBadge = document.createElement('span');
    indexBadge.className = 'card-index';
    indexBadge.textContent = String(index);
    headerEl.appendChild(indexBadge);

    const projectTitle = document.createElement('div');
    projectTitle.className = 'card-project';
    projectTitle.textContent = card.project_name || 'Property Listing';
    headerEl.appendChild(projectTitle);

    const availBadge = document.createElement('span');
    const avail = (card.availability || 'AVAILABLE').toLowerCase();
    availBadge.className = `card-badge badge-${avail}`;
    availBadge.textContent = card.availability || 'AVAILABLE';
    headerEl.appendChild(availBadge);

    cardEl.appendChild(headerEl);

    // Location
    const locEl = document.createElement('div');
    locEl.className = 'card-location';
    locEl.textContent = `${card.sector ? 'Sector ' + card.sector + ', ' : ''}${card.city || ''}`;
    cardEl.appendChild(locEl);

    // Meta Specs: BHK, Floor, Area
    const metaRow = document.createElement('div');
    metaRow.className = 'card-meta-row';

    const bhkSpan = document.createElement('span');
    bhkSpan.textContent = card.configuration_bhk ? `${card.configuration_bhk} BHK` : 'Config N/A';
    metaRow.appendChild(bhkSpan);

    const floorSpan = document.createElement('span');
    floorSpan.textContent = card.floor !== null && card.floor !== undefined ? `Floor ${card.floor}` : 'Floor N/A';
    metaRow.appendChild(floorSpan);

    const areaSpan = document.createElement('span');
    areaSpan.textContent = card.area_sqft ? `${Number(card.area_sqft).toLocaleString()} sqft` : 'Area N/A';
    metaRow.appendChild(areaSpan);

    cardEl.appendChild(metaRow);

    // Price Row
    const priceRow = document.createElement('div');
    priceRow.className = 'card-price-row';

    const priceLabel = document.createElement('span');
    priceLabel.className = 'card-price-label';
    priceLabel.textContent = 'Price';
    priceRow.appendChild(priceLabel);

    const priceValue = document.createElement('span');
    priceValue.className = 'card-price-value';
    priceValue.textContent = card.display_price || (card.price_lakh ? `₹${card.price_lakh} L` : 'Price on request');
    priceRow.appendChild(priceValue);

    cardEl.appendChild(priceRow);

    return cardEl;
  }

  function buildNotesContainer(notes) {
    if (!notes || notes.length === 0) return null;
    const container = document.createElement('div');
    container.className = 'notes-container';

    notes.forEach(noteText => {
      const item = document.createElement('div');
      item.className = 'note-item';

      const bullet = document.createElement('span');
      bullet.className = 'note-bullet';
      bullet.textContent = '•';
      item.appendChild(bullet);

      const text = document.createElement('span');
      text.textContent = noteText;
      item.appendChild(text);

      container.appendChild(item);
    });

    return container;
  }

  function buildQuickReplies(replies, hasMore) {
    const list = Array.isArray(replies) ? [...replies] : [];
    if (hasMore && !list.includes('Show more')) {
      list.push('Show more');
    }
    if (list.length === 0) return null;

    const container = document.createElement('div');
    container.className = 'quick-replies';

    list.forEach(label => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = label === 'Show more' ? 'pill-btn pill-show-more' : 'pill-btn';
      btn.textContent = label;
      btn.addEventListener('click', () => {
        handleQuickReply(label);
      });
      container.appendChild(btn);
    });

    return container;
  }

  function buildDebugAccordion(debugData) {
    if (!debugData) return null;
    const details = document.createElement('details');
    details.className = 'debug-accordion';

    const summary = document.createElement('summary');
    summary.className = 'debug-summary';
    summary.textContent = `🔍 How I got this (${debugData.latency_ms || 0}ms)`;
    details.appendChild(summary);

    const content = document.createElement('div');
    content.className = 'debug-content';
    content.textContent = JSON.stringify(debugData, null, 2);
    details.appendChild(content);

    return details;
  }

  function appendAssistantBubble(payload) {
    const row = document.createElement('div');
    row.className = 'message-row assistant';

    const container = document.createElement('div');
    container.className = 'assistant-container';

    // 1. Assistant Text Message
    if (payload.reply) {
      const bubble = document.createElement('div');
      bubble.className = 'assistant-bubble';
      bubble.textContent = sanitizeText(payload.reply);
      container.appendChild(bubble);
    }

    // 2. Deterministic Notes
    if (payload.notes && payload.notes.length > 0) {
      const notesEl = buildNotesContainer(payload.notes);
      if (notesEl) container.appendChild(notesEl);
    }

    // 3. Property Cards
    if (payload.cards && payload.cards.length > 0) {
      const cardsGrid = document.createElement('div');
      cardsGrid.className = 'cards-container';
      payload.cards.forEach((card, idx) => {
        const cardIndex = (payload.offset || 0) + idx + 1;
        cardsGrid.appendChild(buildPropertyCard(card, cardIndex));
      });
      container.appendChild(cardsGrid);
    }

    // 4. Quick-reply buttons & Show More
    const qrEl = buildQuickReplies(payload.quick_replies, payload.has_more);
    if (qrEl) container.appendChild(qrEl);

    // 5. Collapsible Debug Accordion
    if (payload.client_debug) {
      const debugEl = buildDebugAccordion(payload.client_debug);
      if (debugEl) container.appendChild(debugEl);
    }

    row.appendChild(container);
    chatStream.appendChild(row);
    scrollChatToBottom();
    return row;
  }

  // --- Active Filters Management ---

  function renderFilterChips(filters) {
    filterChips.innerHTML = '';
    if (!filters) {
      filterBar.classList.add('hidden');
      return;
    }

    const slots = [];
    if (filters.city) {
      slots.push({ slot: 'city', label: `City: ${filters.city}` });
    }
    if (filters.sector) {
      slots.push({ slot: 'sector', label: `Sector: ${filters.sector}` });
    }
    if (filters.bhk_in && filters.bhk_in.length > 0) {
      slots.push({ slot: 'bhk', label: `BHK: ${filters.bhk_in.join(', ')}` });
    }
    if (filters.price_min_lakh || filters.price_max_lakh) {
      let bText = 'Budget: ';
      if (filters.price_min_lakh && filters.price_max_lakh) {
        bText += `₹${filters.price_min_lakh}L - ₹${filters.price_max_lakh}L`;
      } else if (filters.price_max_lakh) {
        bText += `Under ₹${filters.price_max_lakh}L`;
      } else {
        bText += `Above ₹${filters.price_min_lakh}L`;
      }
      slots.push({ slot: 'budget', label: bText });
    }

    if (slots.length === 0) {
      filterBar.classList.add('hidden');
      return;
    }

    filterBar.classList.remove('hidden');
    slots.forEach(item => {
      const chip = document.createElement('span');
      chip.className = 'chip';

      const label = document.createElement('span');
      label.textContent = item.label;
      chip.appendChild(label);

      const removeBtn = document.createElement('button');
      removeBtn.type = 'button';
      removeBtn.className = 'chip-remove';
      removeBtn.setAttribute('aria-label', `Remove ${item.label}`);
      removeBtn.textContent = '×';
      removeBtn.addEventListener('click', () => {
        handleRemoveFilter(item.slot);
      });
      chip.appendChild(removeBtn);

      filterChips.appendChild(chip);
    });
  }

  // --- API Handlers ---

  async function sendMessage(messageText) {
    if (!messageText || isSubmitting) return;

    hideError();
    isSubmitting = true;
    lastFailedMessage = messageText;
    userInput.value = '';
    updateCharCount();
    autoResizeInput();

    appendUserBubble(messageText);

    const typingEl = createTypingIndicator();
    chatStream.appendChild(typingEl);
    scrollChatToBottom();

    try {
      const response = await fetch('/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: sessionId,
          message: messageText,
        }),
      });

      removeTypingIndicator();

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.detail || `Server returned error (${response.status})`);
      }

      const payload = await response.json();
      lastFailedMessage = null;
      appendAssistantBubble(payload);

      // Update active filters if session state returned
      if (payload.client_debug && payload.client_debug.resolved_filters) {
        renderFilterChips(payload.client_debug.resolved_filters);
      }
    } catch (err) {
      removeTypingIndicator();
      showError(err.message || 'Failed to connect. Please check your connection.', true);
      // Put message back in input if not already typing
      if (!userInput.value) {
        userInput.value = lastFailedMessage || '';
        updateCharCount();
        autoResizeInput();
      }
    } finally {
      isSubmitting = false;
      updateCharCount();
      userInput.focus();
    }
  }

  async function handleQuickReply(text) {
    await sendMessage(text);
  }

  async function handleRemoveFilter(slot) {
    if (isSubmitting) return;
    hideError();
    isSubmitting = true;

    const typingEl = createTypingIndicator();
    chatStream.appendChild(typingEl);
    scrollChatToBottom();

    try {
      const response = await fetch('/chat/remove-filter', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: sessionId,
          slot: slot,
        }),
      });

      removeTypingIndicator();

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.detail || 'Failed to remove filter');
      }

      const payload = await response.json();
      appendAssistantBubble(payload);

      if (payload.client_debug && payload.client_debug.resolved_filters) {
        renderFilterChips(payload.client_debug.resolved_filters);
      }
    } catch (err) {
      removeTypingIndicator();
      showError(err.message || 'Failed to remove filter', false);
    } finally {
      isSubmitting = false;
    }
  }

  async function resetSearch() {
    sessionId = (typeof crypto.randomUUID === 'function')
      ? crypto.randomUUID()
      : 'sess-' + Math.random().toString(36).substring(2, 15) + Math.random().toString(36).substring(2, 15);
    localStorage.setItem(STORAGE_KEY, sessionId);

    chatStream.innerHTML = '';
    renderFilterChips(null);
    hideError();

    appendAssistantBubble({
      reply: "Hi! I'm your grounded real-estate assistant for Noida, Greater Noida West, and Gurugram. What kind of home are you looking for today?",
      quick_replies: ['3 BHK in Noida', 'Flats in Gurugram under 1.5 Cr', 'Greater Noida West 2 BHK'],
    });

    userInput.focus();
  }

  async function restoreSessionHistory() {
    if (!sessionId) {
      await resetSearch();
      return;
    }

    try {
      const res = await fetch(`/chat/${sessionId}`);
      if (!res.ok) {
        await resetSearch();
        return;
      }

      const data = await res.json();
      if (!data.history || data.history.length === 0) {
        await resetSearch();
        return;
      }

      chatStream.innerHTML = '';
      data.history.forEach(turn => {
        if (turn.role === 'customer' || turn.role === 'user') {
          appendUserBubble(turn.content);
        } else {
          // Assistant turn
          const payload = {
            reply: turn.content,
            cards: [],
            notes: [],
            quick_replies: [],
          };
          if (turn.debug && typeof turn.debug === 'object') {
            payload.client_debug = turn.debug;
          }
          appendAssistantBubble(payload);
        }
      });

      if (data.state && data.state.filters) {
        renderFilterChips(data.state.filters);
      }
    } catch {
      await resetSearch();
    }
  }

  // --- Event Listeners ---

  userInput.addEventListener('input', () => {
    updateCharCount();
    autoResizeInput();
  });

  userInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      if (!btnSend.disabled) {
        const text = userInput.value.trim();
        if (text) sendMessage(text);
      }
    }
  });

  chatForm.addEventListener('submit', (e) => {
    e.preventDefault();
    const text = userInput.value.trim();
    if (text) sendMessage(text);
  });

  btnNewSearch.addEventListener('click', () => {
    resetSearch();
  });

  btnRetry.addEventListener('click', () => {
    if (lastFailedMessage) {
      sendMessage(lastFailedMessage);
    }
  });

  btnDismissError.addEventListener('click', () => {
    hideError();
  });

  // --- Initial Mount ---
  updateCharCount();
  restoreSessionHistory();
})();
