const STATUS_OPTIONS = [
  ['progress', 'Progress'],
  ['task_done', 'Task done'],
  ['blocked', 'Blocked'],
  ['interrupted', 'Interrupted'],
  ['off_task', 'Off task'],
];

const STATUS_LABEL = Object.fromEntries(STATUS_OPTIONS);
const NEXT_LABEL = {
  same_task: 'Keep this task',
  choose_task: 'Pick another',
  finish: 'Stop for today',
};
const PHASE_LABEL = {
  idle: 'Idle',
  work: 'Working',
  work_paused: 'Paused',
  feedback: 'Waiting for feedback',
  break: 'On a break',
  break_paused: 'Break paused',
};

const el = (id) => document.getElementById(id);
const errorNode = el('error');
let state = null;
let lastPhase = null;
let draftKey = null;
let historyDay = new Date().toLocaleDateString('sv-SE');

// ---------------------------------------------------------------- utilities

function two(n) { return String(n).padStart(2, '0'); }

function mmss(total) {
  total = Math.max(0, Math.round(total));
  return two(Math.floor(total / 60)) + ':' + two(total % 60);
}

function humanDuration(total) {
  total = Math.max(0, Math.round(total));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h) return h + 'h ' + m + 'm';
  if (m) return m + 'm' + (s ? ' ' + s + 's' : '');
  return s + 's';
}

function clockTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return two(d.getHours()) + ':' + two(d.getMinutes());
}

function showError(message) {
  errorNode.textContent = message || '';
}

async function api(path, options) {
  const response = await fetch(path, options);
  let body = {};
  try { body = await response.json(); } catch (e) { body = {}; }
  if (!response.ok) throw new Error(body.error || 'Request failed (' + response.status + ')');
  return body;
}

function post(path, payload) {
  return api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Intent-Log': '1' },
    body: JSON.stringify(payload || {}),
  });
}

// -------------------------------------------------------------- feedback UI

function buildStatuses() {
  const box = el('statuses');
  for (const [value, label] of STATUS_OPTIONS) {
    const wrapper = document.createElement('label');
    const input = document.createElement('input');
    input.type = 'radio';
    input.name = 'status';
    input.value = value;
    input.addEventListener('change', saveDraft);
    wrapper.append(input, document.createTextNode(label));
    box.append(wrapper);
  }
}

function saveDraft() {
  if (!draftKey) return;
  const picked = document.querySelector('input[name="status"]:checked');
  localStorage.setItem(draftKey, JSON.stringify({
    status: picked ? picked.value : '',
    content: el('feedback-content').value,
    next: el('feedback-next').value,
  }));
}

function loadDraft() {
  const raw = draftKey && localStorage.getItem(draftKey);
  if (!raw) return;
  try {
    const draft = JSON.parse(raw);
    if (draft.status) {
      const input = document.querySelector('input[name="status"][value="' + draft.status + '"]');
      if (input) input.checked = true;
    }
    el('feedback-content').value = draft.content || '';
    if (draft.next) el('feedback-next').value = draft.next;
  } catch (e) { /* ignore a broken draft */ }
}

function resetFeedbackForm() {
  document.querySelectorAll('input[name="status"]').forEach((input) => { input.checked = false; });
  el('feedback-content').value = '';
  el('feedback-next').value = 'same_task';
}

// ------------------------------------------------------------------- render

function phaseClass(phase) {
  if (phase === 'work' || phase === 'work_paused') return 'work';
  if (phase === 'feedback') return 'feedback';
  if (phase === 'break' || phase === 'break_paused') return 'break';
  return '';
}

function render(s) {
  state = s;
  const c = s.cycle;
  const phase = c.phase;

  el('phase-dot').className = 'dot ' + phaseClass(phase);
  el('phase-label').textContent = PHASE_LABEL[phase] || phase;
  el('version').textContent = s.version ? 'v' + s.version : '';

  el('today').textContent = s.today.sessions
    ? s.today.sessions + (s.today.sessions === 1 ? ' session' : ' sessions') +
      ' today · ' + humanDuration(s.today.work_seconds)
    : 'No sessions yet today';

  el('current').hidden = !(phase === 'work' || phase === 'work_paused');
  el('break').hidden = !(phase === 'break' || phase === 'break_paused');
  el('feedback').hidden = phase !== 'feedback';
  el('picker').hidden = phase !== 'idle';

  el('current-task').textContent = c.task
    ? c.task.title + (c.task.project ? ' · ' + c.task.project : '')
    : '(no task selected, that is fine)';
  el('pause-btn').textContent = phase === 'work_paused' ? 'Resume' : 'Pause';
  el('current-sub').textContent = 'Worked ' + humanDuration(c.work_seconds) +
    ' of ' + humanDuration(c.planned_seconds) +
    (phase === 'work_paused' ? ' · paused, not counting' : '');
  el('warn').textContent = (c.warnings || []).join(' ');

  tick();

  if (phase === 'feedback') {
    if (lastPhase !== 'feedback') {
      draftKey = 'intent-log-draft:' + (c.started_at || 'unknown');
      resetFeedbackForm();
      loadDraft();
      el('feedback-sub').textContent = (c.task ? c.task.title + ' · ' : '') +
        'worked ' + humanDuration(c.work_seconds);
      el('feedback-content').focus();
    }
  } else {
    draftKey = null;
  }

  if (phase === 'idle') {
    const hint = el('last-hint');
    const last = c.last_task;
    if (last && last.title) {
      hint.hidden = false;
      hint.replaceChildren();
      const button = document.createElement('button');
      button.className = 'ghost small';
      button.textContent = 'Continue "' + last.title + '"';
      button.addEventListener('click', () => startCycle(last.uuid));
      hint.append('Last segment: ', button);
    } else {
      hint.hidden = true;
    }
  }

  lastPhase = phase;
}

function tick() {
  if (!state) return;
  const c = state.cycle;
  if (c.phase === 'work' || c.phase === 'work_paused') {
    el('clock').textContent = mmss(c.remaining_seconds);
  }
  if (c.phase === 'break' || c.phase === 'break_paused') {
    el('break-clock').textContent = mmss(c.remaining_seconds);
  }
}

// -------------------------------------------------------------------- data

async function refresh() {
  try {
    const s = await api('/api/state');
    showError('');
    render(s);
  } catch (error) {
    showError(error.message);
  }
}

async function loadTasks() {
  try {
    const data = await api('/api/tasks');
    const list = el('tasks');
    list.replaceChildren();
    if (!data.tasks.length) {
      const li = document.createElement('li');
      li.className = 'empty';
      li.textContent = 'No open tasks';
      list.append(li);
      return;
    }
    for (const task of data.tasks) {
      const li = document.createElement('li');
      const main = document.createElement('div');
      main.className = 'task-main';
      const title = document.createElement('div');
      title.className = 'task-title';
      title.textContent = task.summary;
      const meta = document.createElement('div');
      meta.className = 'task-meta';
      meta.textContent = task.project || 'Inbox';
      main.append(title, meta);
      const button = document.createElement('button');
      button.className = 'start';
      button.textContent = 'Start 20 min';
      button.addEventListener('click', () => startCycle(task.uuid));
      li.append(main, button);
      list.append(li);
    }
  } catch (error) {
    showError(error.message);
  }
}

async function loadHistory() {
  try {
    const data = await api('/api/records?date=' + encodeURIComponent(historyDay));
    const body = el('history').querySelector('tbody');
    body.replaceChildren();
    el('history-summary').textContent = data.records.length
      ? data.records.length + (data.records.length === 1 ? ' session · ' : ' sessions · ') +
        humanDuration(data.summary.work_seconds)
      : 'No records for this day';
    for (const record of data.records) {
      const row = document.createElement('tr');
      [clockTime(record.started_at), clockTime(record.ended_at),
       humanDuration(record.work_seconds)].forEach((text) => {
        const td = document.createElement('td');
        td.className = 'num';
        td.textContent = text;
        row.append(td);
      });
      const taskCell = document.createElement('td');
      taskCell.textContent = record.task_title || '—';
      if (record.task_project) {
        const meta = document.createElement('div');
        meta.className = 'task-meta';
        meta.textContent = record.task_project;
        taskCell.append(meta);
      }
      row.append(taskCell);

      const feedbackCell = document.createElement('td');
      const tag = document.createElement('span');
      tag.className = 'tag ' + record.status;
      tag.textContent = STATUS_LABEL[record.status] || record.status;
      feedbackCell.append(tag);
      if (record.content) {
        const text = document.createElement('div');
        text.textContent = record.content;
        feedbackCell.append(text);
      }
      row.append(feedbackCell);

      const nextCell = document.createElement('td');
      nextCell.className = 'muted';
      nextCell.textContent = NEXT_LABEL[record.next_action] || record.next_action || '';
      row.append(nextCell);
      body.append(row);
    }
  } catch (error) {
    showError(error.message);
  }
}

async function startCycle(taskUuid) {
  showError('');
  try {
    render(await post('/api/cycle/start', { task_id: taskUuid || null }));
  } catch (error) {
    showError(error.message);
  }
}

// -------------------------------------------------------------------- sync

const SYNC_PANELS = [
  { key: 'task', endpoint: '/api/sync', prefix: 'sync' },
  { key: 'log', endpoint: '/api/log-sync', prefix: 'log-sync' },
];

function renderSync(panel, data) {
  const config = data.config || {};
  el(panel.prefix + '-remote').value = config.remote || '';
  const bits = [];
  if (!config.git) {
    bits.push('No git repository at ' + (config.path || 'the data directory'));
  } else {
    if (config.branch) bits.push('branch ' + config.branch);
    if (config.behind !== null && config.behind !== undefined) bits.push(config.behind + ' behind');
    if (config.ahead !== null && config.ahead !== undefined) bits.push(config.ahead + ' ahead');
    if (config.dirty) bits.push('uncommitted changes');
    if (!config.remote) bits.push('no origin remote');
  }
  el(panel.prefix + '-status').textContent = bits.join(' · ');

  const output = el(panel.prefix + '-output');
  if (data.last) {
    output.hidden = false;
    output.textContent = (data.last.ok ? 'Last sync ' : 'Last sync failed ') +
      clockTime(data.last.at) + '\n' + (data.last.message || '');
  }
}

async function loadSync(panel) {
  try {
    renderSync(panel, await api(panel.endpoint));
  } catch (error) {
    el(panel.prefix + '-status').textContent = 'Could not read the sync config';
  }
}

// ------------------------------------------------------------------- events

document.querySelectorAll('#current-actions button').forEach((button) => {
  button.addEventListener('click', async () => {
    const action = button.dataset.action;
    if (action === 'cancel' && !window.confirm('Discard this segment? Nothing will be recorded.')) return;
    showError('');
    try {
      render(await post('/api/cycle/' + action));
    } catch (error) {
      showError(error.message);
    }
  });
});

document.querySelectorAll('[data-action="skip-break"]').forEach((button) => {
  button.addEventListener('click', async () => {
    try {
      render(await post('/api/cycle/skip-break'));
    } catch (error) {
      showError(error.message);
    }
  });
});

el('feedback-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const picked = document.querySelector('input[name="status"]:checked');
  if (!picked) {
    showError('Pick a status first');
    return;
  }
  const content = el('feedback-content').value.trim();
  if (!content) {
    showError('Write one line, even a few words is enough');
    el('feedback-content').focus();
    return;
  }
  const button = el('feedback-submit');
  button.disabled = true;
  showError('');
  try {
    const result = await post('/api/cycle/feedback', {
      status: picked.value,
      content: content,
      next_action: el('feedback-next').value,
    });
    if (draftKey) localStorage.removeItem(draftKey);
    render(result);
    loadHistory();
  } catch (error) {
    showError(error.message);
  } finally {
    button.disabled = false;
  }
});

el('feedback-content').addEventListener('input', saveDraft);
el('feedback-next').addEventListener('change', saveDraft);

el('add-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  showError('');
  try {
    await post('/api/tasks', {
      description: el('new-desc').value,
      project: el('new-project').value,
    });
    el('new-desc').value = '';
    el('new-project').value = '';
    loadTasks();
  } catch (error) {
    showError(error.message);
  }
});

el('refresh-tasks').addEventListener('click', loadTasks);

el('history-date').value = historyDay;
el('history-date').addEventListener('change', () => {
  historyDay = el('history-date').value || historyDay;
  loadHistory();
});

SYNC_PANELS.forEach((panel) => {
  el(panel.prefix + '-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const url = el(panel.prefix + '-remote').value.trim();
    if (!url) {
      showError('Enter a git URL for the ' + panel.key + ' repository');
      return;
    }
    if (!window.confirm('Point the ' + panel.key + ' repository origin at\n' + url +
        '\n\nThis only changes the remote URL; no data is rewritten.')) {
      return;
    }
    showError('');
    try {
      renderSync(panel, await post(panel.endpoint + '/remote', { url: url }));
    } catch (error) {
      showError(error.message);
    }
  });

  el(panel.prefix + '-now').addEventListener('click', async () => {
    const button = el(panel.prefix + '-now');
    button.disabled = true;
    showError('');
    try {
      renderSync(panel, await post(panel.endpoint));
      if (panel.key === 'task') loadTasks();
    } catch (error) {
      showError(error.message);
      loadSync(panel);
    } finally {
      button.disabled = false;
    }
  });
});

// --------------------------------------------------------------------- boot

buildStatuses();
refresh().then(() => {
  if (state && state.cycle.phase === 'idle') loadTasks();
});
loadHistory();
SYNC_PANELS.forEach(loadSync);
setInterval(tick, 250);
setInterval(refresh, 1000);
setInterval(() => {
  if (state && state.cycle.phase === 'idle') loadTasks();
}, 60000);
