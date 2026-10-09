const $ = selector => document.querySelector(selector);
const token = $('meta[name="app-token"]').content;

let state = null;
let refreshing = false;
let lastRenderedListingsSignature = '';

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, char => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]));
}

function date(value) {
  if (!value) return '';
  return new Date(value).toLocaleString('ru-RU', {day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'});
}

async function api(path, body) {
  const response = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: {
      'X-App-Token': token,
      ...(body === undefined ? {} : {'Content-Type': 'application/json'})
    },
    ...(body === undefined ? {} : {body: JSON.stringify(body)})
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Не удалось выполнить действие.');
  return result;
}

function notice(text) {
  const el = $('#notice');
  el.textContent = text;
  el.hidden = !text;
  if (text) {
    setTimeout(() => {
      if (el.textContent === text) el.hidden = true;
    }, 4000);
  }
}

function render() {
  if (!state) return;

  // Runtime Waiting Status
  const runtime = state.runtime || {};
  const statusEl = $('#runtime-status');
  if (statusEl) {
    const postingEnabled = Boolean(state.settings?.telegram_posting_enabled);
    const workerStopped = runtime.workers && Object.values(runtime.workers).some(alive => !alive);
    if (workerStopped) {
      statusEl.textContent = `${runtime.label || 'Фоновая обработка остановлена'} · Постинг TG: ${postingEnabled ? 'включён' : 'выключен'}`;
      statusEl.parentElement.style.background = '#fde8e7';
      statusEl.parentElement.style.color = '#8a2420';
    } else if (runtime.waiting_photos && runtime.waiting_photos.count) {
      statusEl.textContent = `📸 Получено ${runtime.waiting_photos.count} фото · Ожидание текста описания...`;
      statusEl.parentElement.style.background = '#fff8c5';
      statusEl.parentElement.style.color = '#734f00';
    } else {
      statusEl.textContent = `${runtime.label || 'Сбор GINSENG'} · Постинг TG: ${postingEnabled ? 'включён' : 'выключен'}`;
      statusEl.parentElement.style.background = '#e8f3ee';
      statusEl.parentElement.style.color = '#2b5e4c';
    }
  }

  const postingEnabled = Boolean(state.settings?.telegram_posting_enabled);
  const postingToggle = $('#telegram-posting-toggle');
  if (postingToggle) {
    postingToggle.textContent = postingEnabled ? 'Выключить публикации' : 'Включить публикации';
    postingToggle.setAttribute('aria-pressed', String(postingEnabled));
    postingToggle.classList.toggle('primary', postingEnabled);
    postingToggle.classList.toggle('posting-enabled', postingEnabled);
  }
  const postingState = $('#telegram-posting-state');
  if (postingState) {
    postingState.textContent = postingEnabled
      ? 'Постинг включён. Очередь Telegram обрабатывается автоматически.'
      : 'Постинг выключен. Исходящие публикации и изменения постов приостановлены.';
    postingState.classList.toggle('enabled', postingEnabled);
  }

  // 1. Zalo Status
  const zaloStatus = state.runtime?.zalo || {};
  const zaloReady = Boolean(zaloStatus.ready);
  const zaloBadge = $('#zalo-badge');
  const zaloText = $('#zalo-status-text');
  const zaloSource = (state.sources || []).find(source => source.enabled && source.kind === 'group');
  const zaloJob = (state.jobs || []).find(job => job.source_id === zaloSource?.id &&
    ['bind_source', 'collect_archive', 'collect_initial_scan', 'collect_incremental'].includes(job.kind));
  if (zaloReady) {
    if (state.runtime?.workers?.collector === false) {
      zaloBadge.textContent = 'Сбор остановлен';
      zaloBadge.className = 'badge danger';
      zaloText.textContent = state.runtime.label || 'Перезапустите приложение, чтобы продолжить сбор';
    } else if (state.runtime?.workers?.interpreter === false) {
      zaloBadge.textContent = 'Обработка остановлена';
      zaloBadge.className = 'badge danger';
      zaloText.textContent = state.runtime.label || 'Перезапустите приложение, чтобы продолжить обработку';
    } else if (zaloJob?.state === 'failed') {
      zaloBadge.textContent = 'Ошибка сбора';
      zaloBadge.className = 'badge danger';
      zaloText.textContent = zaloJob.detail || 'Не удалось прочитать группу GINSENG';
    } else if (zaloSource && zaloSource.binding_state !== 'bound') {
      zaloBadge.textContent = 'Группа не открыта';
      zaloBadge.className = 'badge warning';
      zaloText.textContent = zaloSource.binding_detail || zaloJob?.detail || 'Привязка чата GINSENG ещё не завершена';
    } else {
      zaloBadge.textContent = 'Подключено';
      zaloBadge.className = 'badge success';
      zaloText.textContent = 'Группа GINSENG открыта · сканирование каждую секунду';
    }
  } else {
    zaloBadge.textContent = 'Требуется вход';
    zaloBadge.className = 'badge warning';
    zaloText.textContent = 'Откройте Chrome для разовой авторизации';
  }

  // 2. OmniRoute Status
  const omni = state.omniroute || {};
  const aiBadge = $('#ai-badge');
  const aiText = $('#ai-status-text');
  const aiModel = $('#ai-model-name');
  if (omni.connected || (omni.has_key && !omni.error)) {
    aiBadge.textContent = 'Активен';
    aiBadge.className = 'badge success';
    aiText.textContent = 'Связь с OmniRoute установлена';
  } else if (!omni.has_key) {
    aiBadge.textContent = 'Не настроен';
    aiBadge.className = 'badge warning';
    aiText.textContent = 'Укажите URL и API-ключ';
  } else {
    aiBadge.textContent = 'Ошибка связи';
    aiBadge.className = 'badge danger';
    aiText.textContent = omni.error || 'Проверьте API-ключ и URL';
  }
  aiModel.textContent = omni.model || 'gemini-3.7-flash-tiered';

  // 3. Telegram Status
  const tg = state.telegram || {};
  const tgBadge = $('#tg-badge');
  const tgText = $('#tg-status-text');
  const tgChannel = $('#tg-channel-name');
  if (tg.connected || (tg.has_token && !tg.error)) {
    tgBadge.textContent = 'Подключено';
    tgBadge.className = 'badge success';
    tgText.textContent = tg.bot_username ? `@${tg.bot_username}` : 'Бот активен';
    tgChannel.textContent = tg.channel_title || tg.channel_id || 'Подключен';
  } else if (!tg.has_token) {
    tgBadge.textContent = 'Не настроен';
    tgBadge.className = 'badge warning';
    tgText.textContent = 'Укажите ID канала и токен бота';
    tgChannel.textContent = 'Не указан';
  } else {
    tgBadge.textContent = 'Ошибка токена';
    tgBadge.className = 'badge danger';
    tgText.textContent = tg.error || 'Проверьте токен бота в настройках';
    tgChannel.textContent = tg.channel_id || 'Не указан';
  }

  // 4. Feed of Published Listings
  const listings = state.listings || [];
  $('#total-published').textContent = listings.length;
  const feedContainer = $('#feed-container');

  const currentSignature = listings.map(l => `${l.id}:${l.updated_at}:${l.publication_state}:${l.status}:${l.automation?.state || ''}:${l.automation?.reason || ''}:${(l.media || []).length}`).join('|');
  if (currentSignature === lastRenderedListingsSignature) {
    return;
  }
  lastRenderedListingsSignature = currentSignature;

  if (!listings.length) {
    feedContainer.innerHTML = `
      <div class="empty-feed">
        <span class="empty-icon">📂</span>
        <h3>Пока нет публикаций</h3>
        <p>Отправьте в группу GINSENG фотографии и текст объявления в любом порядке — приложение свяжет их и опубликует пост в Telegram по шаблону.</p>
      </div>
    `;
    return;
  }

  feedContainer.innerHTML = listings.map(item => {
    const photos = (item.media || []).filter(m => m.kind !== 'video');
    const galleryHtml = photos.length ? `
      <div class="post-gallery">
        ${photos.slice(0, 10).map((m, idx) => `
          <img src="/api/media/${m.id}?token=${encodeURIComponent(token)}" alt="Фото ${idx + 1}" loading="lazy">
        `).join('')}
      </div>
    ` : '<div class="post-gallery-empty">Фотографии не прикреплены</div>';

    let pubState;
    if (item.publication_state === 'sent') {
      pubState = '<span class="tag success">Опубликовано в TG</span>';
    } else if (item.status === 'rented') {
      pubState = '<span class="tag">Сдано</span>';
    } else if (item.automation?.state === 'review') {
      pubState = '<span class="tag warning">Нужна проверка</span>';
    } else if (item.automation?.last_error) {
      pubState = '<span class="tag danger">Требует внимания</span>';
    } else if (item.automation?.state === 'pending') {
      pubState = '<span class="tag">В очереди ИИ</span>';
    } else {
      pubState = '<span class="tag">В обработке</span>';
    }

    let bannerHtml = '';
    if (item.publication_state !== 'sent') {
      if (item.automation?.state === 'review' && item.automation?.reason) {
        bannerHtml = `<div class="listing-review-banner">⚠️ <strong>Причина задержки:</strong> ${escapeHtml(item.automation.reason)}</div>`;
      } else if (item.automation?.last_error) {
        bannerHtml = `<div class="listing-review-banner error">⚠️ <strong>Причина задержки:</strong> ${escapeHtml(item.automation.last_error)}</div>`;
      }
    }

    const actionButton = item.publication_state === 'sent'
      ? `<button class="button small" data-republish="${item.id}">Переопубликовать</button>`
      : `<button class="button small primary" data-republish="${item.id}">Опубликовать в TG</button>`;

    return `
      <article class="feed-card">
        <div class="feed-card-header">
          <div>
            <span class="listing-code">NT-${String(item.id).padStart(5, '0')}</span>
            <strong>${escapeHtml(item.title)}</strong>
          </div>
          <div class="feed-header-meta">
            ${pubState}
            <span class="muted">${date(item.updated_at)}</span>
          </div>
        </div>
        ${bannerHtml}
        ${galleryHtml}
        <div class="feed-card-body">
          <div class="price-tag">${escapeHtml(item.price || 'Цена по запросу')}</div>
          <pre class="post-text-preview">${escapeHtml(item.description)}</pre>
        </div>
        <div class="feed-card-footer">
          <span class="small-muted">Фото: ${photos.length} шт.${item.sources?.length ? ` · Группа: ${item.sources.map(source => escapeHtml(source.name)).join(', ')}` : ''}</span>
          <div class="feed-actions">
            ${actionButton}
            <button class="button small danger" data-delete="${item.id}">Удалить</button>
          </div>
        </div>
      </article>
    `;
  }).join('');
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const next = await api('/api/snapshot');
    state = next;
    render();
  } catch (err) {
    console.error('Refresh error:', err);
  } finally {
    refreshing = false;
  }
}

// Events & Dialog Handlers
document.addEventListener('DOMContentLoaded', () => {
  $('#telegram-posting-toggle')?.addEventListener('click', async event => {
    const button = event.currentTarget;
    const enabled = !Boolean(state?.settings?.telegram_posting_enabled);
    button.disabled = true;
    try {
      await api('/api/telegram-posting', {enabled});
      notice(enabled ? 'Публикации в Telegram включены.' : 'Публикации в Telegram выключены.');
      await refresh();
    } catch (err) {
      notice(err.message);
    } finally {
      button.disabled = false;
    }
  });

  // Open dialogs
  $('#ai-configure-btn')?.addEventListener('click', () => {
    const omni = state?.omniroute || {};
    const form = $('#omniroute-form');
    form.base_url.value = omni.base_url || 'http://127.0.0.1:20128';
    form.model.value = omni.model || 'agy/gemini-3.7-flash-tiered';
    form.api_key.value = '';
    const datalist = $('#omniroute-models');
    datalist.innerHTML = (omni.models || []).map(m => `<option value="${escapeHtml(m)}"></option>`).join('');
    $('#omniroute-dialog').showModal();
  });

  $('#tg-configure-btn')?.addEventListener('click', () => {
    const tg = state?.telegram || {};
    const form = $('#telegram-form');
    form.channel_id.value = tg.channel_id || '';
    form.token.value = '';
    $('#telegram-dialog').showModal();
  });

  // Close dialogs
  document.querySelectorAll('[data-close]').forEach(btn => {
    btn.addEventListener('click', () => {
      const dialogId = btn.getAttribute('data-close');
      const dialog = document.getElementById(dialogId);
      if (dialog) dialog.close();
    });
  });

  const photoDialog = $('#photo-preview-dialog');
  const photoPreview = $('#photo-preview-image');
  const photoScale = $('#photo-preview-scale');
  $('#feed-container')?.addEventListener('click', event => {
    const thumbnail = event.target.closest('.post-gallery img');
    if (!thumbnail || !photoDialog || !photoPreview) return;
    photoPreview.classList.remove('original-size');
    photoPreview.src = thumbnail.currentSrc || thumbnail.src;
    photoDialog.showModal();
    photoPreview.onload = () => {
      $('#photo-preview-dimensions').textContent = `${photoPreview.naturalWidth} × ${photoPreview.naturalHeight} px`;
    };
    if (photoPreview.complete && photoPreview.naturalWidth) {
      $('#photo-preview-dimensions').textContent = `${photoPreview.naturalWidth} × ${photoPreview.naturalHeight} px`;
    }
    photoScale?.setAttribute('aria-pressed', 'false');
  });
  photoScale?.addEventListener('click', () => {
    const original = photoPreview?.classList.toggle('original-size') || false;
    photoScale.setAttribute('aria-pressed', String(original));
  });
  photoDialog?.addEventListener('click', event => {
    if (event.target === photoDialog) photoDialog.close();
  });

  // Open Zalo in Chrome
  document.querySelectorAll('[data-open-zalo]').forEach(btn => {
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try {
        await api('/api/zalo', {action: 'open'});
        notice('Открыто управляемое окно Zalo, которое использует сборщик. Если требуется, войдите по QR-коду.');
      } catch (err) {
        notice(err.message);
      } finally {
        btn.disabled = false;
      }
    });
  });

  // Save OmniRoute Form
  $('#omniroute-form')?.addEventListener('submit', async e => {
    e.preventDefault();
    const form = e.target;
    const submitBtn = form.querySelector('button[type="submit"]');
    const errEl = form.querySelector('.form-error');
    errEl.textContent = '';
    submitBtn.disabled = true;
    try {
      await api('/api/omniroute', {
        base_url: form.base_url.value.trim(),
        api_key: form.api_key.value.trim(),
        model: form.model.value.trim(),
        archive_model: form.model.value.trim(),
        listing_model: form.model.value.trim(),
        decision_model: form.model.value.trim(),
        publisher_model: form.model.value.trim()
      });
      notice('Настройки нейросети сохранены.');
      $('#omniroute-dialog').close();
      refresh();
    } catch (err) {
      errEl.textContent = err.message;
    } finally {
      submitBtn.disabled = false;
    }
  });

  // Save Telegram Form
  $('#telegram-form')?.addEventListener('submit', async e => {
    e.preventDefault();
    const form = e.target;
    const submitBtn = form.querySelector('button[type="submit"]');
    const errEl = form.querySelector('.form-error');
    errEl.textContent = '';
    submitBtn.disabled = true;
    try {
      await api('/api/telegram', {
        channel_id: form.channel_id.value.trim(),
        token: form.token.value.trim()
      });
      notice('Настройки Telegram успешно сохранены и проверены.');
      $('#telegram-dialog').close();
      refresh();
    } catch (err) {
      errEl.textContent = err.message;
    } finally {
      submitBtn.disabled = false;
    }
  });

  // Clear all listings button
  $('#clear-all-listings-btn')?.addEventListener('click', async () => {
    if (!confirm('Удалить все объявления из базы?\n\nБот зафиксирует текущий момент как точку отсчёта и будет автоматически публиковать ТОЛЬКО новые сообщения, которые вы скинете в Zalo после этого.')) return;
    const btn = $('#clear-all-listings-btn');
    btn.disabled = true;
    try {
      await api('/api/clear-all-listings', {});
      notice('Все объявления удалены. Бот ожидает только новые сообщения.');
      refresh();
    } catch (err) {
      notice(err.message);
    } finally {
      btn.disabled = false;
    }
  });

  // Feed Actions (Delegation)
  $('#feed-container')?.addEventListener('click', async e => {
    const republishBtn = e.target.closest('[data-republish]');
    if (republishBtn) {
      const id = republishBtn.getAttribute('data-republish');
      republishBtn.disabled = true;
      try {
        await api('/api/republish', {listing_id: Number(id)});
        notice(`Объект NT-${String(id).padStart(5, '0')} отправлен в Telegram.`);
        refresh();
      } catch (err) {
        notice(err.message);
      } finally {
        republishBtn.disabled = false;
      }
      return;
    }

    const deleteBtn = e.target.closest('[data-delete]');
    if (deleteBtn) {
      const id = deleteBtn.getAttribute('data-delete');
      if (!confirm(`Удалить объект NT-${String(id).padStart(5, '0')} из базы?`)) return;
      deleteBtn.disabled = true;
      try {
        await api('/api/delete-listings', {ids: [Number(id)]});
        notice(`Объект NT-${String(id).padStart(5, '0')} удалён.`);
        refresh();
      } catch (err) {
        notice(err.message);
      } finally {
        deleteBtn.disabled = false;
      }
    }
  });

  // Start polling
  refresh();
  setInterval(refresh, 3000);
});
