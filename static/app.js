const STATUS_OPTIONS = [
  ['progress', '有推进'],
  ['task_done', '任务完成'],
  ['blocked', '卡住了'],
  ['interrupted', '被打断'],
  ['off_task', '跑偏了'],
];

const STATUS_LABEL = Object.fromEntries(STATUS_OPTIONS);
const NEXT_LABEL = {
  same_task: '继续这个任务',
  choose_task: '换个任务',
  finish: '今天先到这',
};
const PHASE_LABEL = {
  idle: '空闲',
  work: '工作进行中',
  work_paused: '已暂停',
  feedback: '等待反馈',
  break: '休息中',
  break_paused: '休息暂停',
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
  const m = Math.floor(total / 60);
  const s = total % 60;
  return two(m) + ':' + two(s);
}

function humanDuration(total) {
  total = Math.max(0, Math.round(total));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h) return h + ' 小时 ' + m + ' 分';
  if (m) return m + ' 分' + (s ? ' ' + s + ' 秒' : '');
  return s + ' 秒';
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
  if (!response.ok) throw new Error(body.error || '请求失败 (' + response.status + ')');
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

  const dot = el('phase-dot');
  dot.className = 'dot ' + phaseClass(phase);
  el('phase-label').textContent = PHASE_LABEL[phase] || phase;

  el('today').textContent = s.today.sessions
    ? '今天 ' + s.today.sessions + ' 段 · ' + humanDuration(s.today.work_seconds)
    : '今天还没有记录';

  // current / break / feedback visibility
  el('current').hidden = !(phase === 'work' || phase === 'work_paused');
  el('break').hidden = !(phase === 'break' || phase === 'break_paused');
  el('feedback').hidden = phase !== 'feedback';
  el('picker').hidden = phase !== 'idle';

  if (c.task) {
    const project = c.task.project ? ' · ' + c.task.project : '';
    el('current-task').textContent = c.task.title + project;
  } else {
    el('current-task').textContent = '（没选任务，自己知道在做什么也行）';
  }
  el('pause-btn').textContent = phase === 'work_paused' ? '继续' : '暂停';
  const planned = c.planned_seconds;
  el('current-sub').textContent = '已工作 ' + humanDuration(c.work_seconds) +
    ' · 计划 ' + humanDuration(planned) +
    (phase === 'work_paused' ? ' · 暂停中，不计时' : '');
  el('warn').textContent = (c.warnings || []).join(' ');

  tick();

  if (phase === 'feedback') {
    if (lastPhase !== 'feedback') {
      draftKey = 'intent-log-draft:' + (c.started_at || 'unknown');
      resetFeedbackForm();
      loadDraft();
      el('feedback-sub').textContent = (c.task ? c.task.title + ' · ' : '') +
        '实际工作 ' + humanDuration(c.work_seconds);
      el('feedback-content').focus();
    }
  } else {
    draftKey = null;
  }

  if (phase === 'idle') {
    const hint = el('last-hint');
    if (c.last_task && c.last_task.title) {
      hint.hidden = false;
      hint.replaceChildren();
      const button = document.createElement('button');
      button.className = 'ghost small';
      button.textContent = '继续「' + c.last_task.title + '」';
      button.addEventListener('click', () => startCycle(c.last_task.uuid));
      hint.append('上一段：', button);
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
      li.textContent = '没有待办任务';
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
      button.textContent = '开始 20 分钟';
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
      ? data.records.length + ' 段 · ' + humanDuration(data.summary.work_seconds)
      : '这天没有记录';
    for (const record of data.records) {
      const row = document.createElement('tr');
      const cells = [
        clockTime(record.started_at),
        clockTime(record.ended_at),
        humanDuration(record.work_seconds),
      ];
      cells.forEach((text, index) => {
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
    const s = await post('/api/cycle/start', { task_id: taskUuid || null });
    render(s);
  } catch (error) {
    showError(error.message);
  }
}

// ------------------------------------------------------------------- events

document.querySelectorAll('#current-actions button').forEach((button) => {
  button.addEventListener('click', async () => {
    const action = button.dataset.action;
    if (action === 'cancel' && !window.confirm('放弃这一段？不会写入记录。')) return;
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
    showError('先选一个状态');
    return;
  }
  const content = el('feedback-content').value.trim();
  if (!content) {
    showError('写一句话，哪怕只有几个字');
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
el('sync-tasks').addEventListener('click', async () => {
  showError('');
  try {
    await post('/api/sync');
    loadTasks();
  } catch (error) {
    showError(error.message);
  }
});

el('history-date').value = historyDay;
el('history-date').addEventListener('change', () => {
  historyDay = el('history-date').value || historyDay;
  loadHistory();
});

// --------------------------------------------------------------------- boot

buildStatuses();
refresh().then(() => {
  if (state && state.cycle.phase === 'idle') loadTasks();
});
loadHistory();
setInterval(tick, 250);
setInterval(refresh, 1000);
setInterval(() => {
  if (state && state.cycle.phase === 'idle') loadTasks();
}, 60000);
