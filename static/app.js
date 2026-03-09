const state = {
  users: [],
  currentUserId: null,
  data: null,
};

const app = document.getElementById('app');
const userSelect = document.getElementById('userSelect');
const userMeta = document.getElementById('userMeta');

const metricLabels = [
  ['open_tasks', '열린 업무'],
  ['overdue_tasks', '지연 업무'],
  ['review_tasks', '검토 대기'],
  ['today_log_count', '오늘 업무일지'],
  ['visible_user_count', '보이는 인원'],
];

init();

async function init() {
  try {
    state.users = await api('/api/org/users');
    if (!state.users.length) {
      renderEmpty('사용자 데이터가 없습니다.');
      return;
    }
    state.currentUserId = state.users[0].id;
    renderUserOptions();
    await loadState();
  } catch (error) {
    renderEmpty(error.message, true);
  }
}

function renderUserOptions() {
  userSelect.innerHTML = state.users.map((user) => (
    `<option value="${user.id}">${user.name} · ${user.role_label} · ${user.team || '무소속'}</option>`
  )).join('');
  userSelect.value = String(state.currentUserId);
  userSelect.addEventListener('change', async (event) => {
    state.currentUserId = Number(event.target.value);
    await loadState();
  });
}

async function loadState() {
  state.data = await api(`/api/org/state?user_id=${state.currentUserId}`);
  render();
}

function render() {
  const { user, summary, highlights, tasks, reminders, my_weekly_focus, teams, reportees, missing_logs, review_queue, assignee_options, logs_today } = state.data;
  userMeta.innerHTML = [
    `<span class="badge role-${user.role}">${user.role_label}</span>`,
    `<span>${user.team || '무소속'} 조직</span>`,
    `<span>${state.data.today}</span>`,
  ].join(' ');

  const myTasks = tasks.filter((task) => task.assignee_id === user.id);
  const openMyTasks = myTasks.filter((task) => task.status !== 'done');

  app.innerHTML = `
    <section class="panel panel-wide">
      <div class="panel-head">
        <div>
          <h2>${user.name}님의 오늘 운영판</h2>
          <p>${roleDescription(user.role)}</p>
        </div>
        <div class="badges">${highlights.map((item, index) => `<span class="highlight-chip ${index === 1 && item.includes('미작성') ? 'warn' : ''}">${escapeHtml(item)}</span>`).join('')}</div>
      </div>
      <div class="metric-grid">
        ${metricLabels.map(([key, label]) => metricCard(label, summary[key] ?? 0)).join('')}
      </div>
    </section>

    ${user.role !== 'member' ? renderAssignmentPanel(user, assignee_options, tasks) : ''}
    ${renderWeeklyFocusPanel(user, my_weekly_focus)}
    ${renderReminderPanel(user, reminders, openMyTasks)}
    ${user.role === 'ceo' ? renderTeamOverview(teams, missing_logs) : ''}
    ${user.role === 'manager' ? renderManagerOverview(reportees, missing_logs) : ''}
    ${renderTasksPanel(user, tasks)}
    ${renderWorklogPanel(user, openMyTasks, logs_today)}
    ${user.role !== 'member' ? renderReviewPanel(review_queue) : ''}
  `;

  bindForms();
}

function renderAssignmentPanel(user, assigneeOptions, tasks) {
  return `
    <section class="panel panel-half">
      <div class="panel-head">
        <div>
          <h2>${user.role === 'ceo' ? '전사 업무 지시' : '팀 업무 배정'}</h2>
          <p>${user.role === 'ceo' ? '대표 시점에서 중간관리자와 팀원에게 직접 업무를 내릴 수 있습니다.' : '팀장이 하위 팀원에게 바로 업무를 나눠줄 수 있습니다.'}</p>
        </div>
      </div>
      <form id="taskForm" class="form-grid">
        <div class="field full">
          <label for="taskTitle">업무 제목</label>
          <input id="taskTitle" name="title" placeholder="예: 고객사 제안서 1차 초안 작성" required>
        </div>
        <div class="field">
          <label for="taskAssignee">담당자</label>
          <select id="taskAssignee" name="assignee_id">${assigneeOptions.map((option) => `<option value="${option.id}">${option.name} · ${option.role_label}</option>`).join('')}</select>
        </div>
        <div class="field">
          <label for="taskDueDate">마감일</label>
          <input id="taskDueDate" name="due_date" type="date" value="${state.data.today}">
        </div>
        <div class="field">
          <label for="taskPriority">우선순위</label>
          <select id="taskPriority" name="priority">
            <option value="1">1 - 긴급</option>
            <option value="2" selected>2 - 중요</option>
            <option value="3">3 - 일반</option>
          </select>
        </div>
        <div class="field">
          <label for="taskParent">상위 업무</label>
          <select id="taskParent" name="parent_task_id">
            <option value="">없음</option>
            ${tasks.map((task) => `<option value="${task.id}">${escapeHtml(task.title)}</option>`).join('')}
          </select>
        </div>
        <div class="field full">
          <label for="taskDescription">지시 내용</label>
          <textarea id="taskDescription" name="description" placeholder="완료 기준, 전달 방식, 체크포인트를 적어두면 팀원이 덜 헷갈립니다."></textarea>
        </div>
        <div class="form-actions">
          <button type="submit">업무 배정하기</button>
          <span id="taskFormNotice" class="notice"></span>
        </div>
      </form>
    </section>
  `;
}

function renderWeeklyFocusPanel(user, weeklyFocus) {
  return `
    <section class="panel panel-half">
      <div class="panel-head">
        <div>
          <h2>이번 주 포커스</h2>
          <p>${user.role === 'member' ? '팀원이 주간 업무를 잊지 않도록 이번 주 핵심 목표를 고정합니다.' : '관리자도 이번 주에 반드시 챙길 우선순위를 남길 수 있습니다.'}</p>
        </div>
      </div>
      <form id="weeklyFocusForm" class="form-grid">
        <div class="field full">
          <label for="weeklyFocus">이번 주 핵심 목표</label>
          <textarea id="weeklyFocus" name="focus" placeholder="예: 제안서 템플릿 마무리, 광고 리포트 검토 완료">${escapeHtml(weeklyFocus?.focus || '')}</textarea>
        </div>
        <div class="field full">
          <label for="weeklySupport">지원이 필요한 점</label>
          <textarea id="weeklySupport" name="support_needed" placeholder="예: 대표 최종 피드백 필요, 회계 데이터 요청">${escapeHtml(weeklyFocus?.support_needed || '')}</textarea>
        </div>
        <div class="form-actions">
          <button type="submit">주간 포커스 저장</button>
          <span id="weeklyFocusNotice" class="notice"></span>
        </div>
      </form>
    </section>
  `;
}

function renderReminderPanel(user, reminders, openMyTasks) {
  if (user.role !== 'member') {
    return `
      <section class="panel panel-third">
        <div class="panel-head">
          <div>
            <h2>오늘의 컨트롤 포인트</h2>
            <p>역할에 맞춰 꼭 챙겨야 할 흐름만 빠르게 볼 수 있게 정리했습니다.</p>
          </div>
        </div>
        <div class="list-grid">
          <article class="list-card">
            <h3>대표라면</h3>
            <p>지연 업무, 업무일지 미작성자, 검토 대기 업무를 먼저 확인하세요.</p>
          </article>
          <article class="list-card">
            <h3>팀장이라면</h3>
            <p>오늘 팀원 배정, 진행 막힘, 일지 검토를 끝내면 팀이 덜 흔들립니다.</p>
          </article>
        </div>
      </section>
    `;
  }

  return `
    <section class="panel panel-third">
      <div class="panel-head">
        <div>
          <h2>오늘 잊지 말 것</h2>
          <p>팀원 화면에서는 오늘과 지연 업무를 먼저 보여줍니다.</p>
        </div>
      </div>
      ${reminders.length ? `
        <div class="list-grid">
          ${reminders.map((item) => `
            <article class="list-card">
              <div class="badges"><span class="badge ${item.kind === '지연' ? 'status-blocked' : ''}">${item.kind}</span></div>
              <h3>${escapeHtml(item.title)}</h3>
              <p class="list-meta">마감일 ${item.due_date}</p>
            </article>
          `).join('')}
        </div>
      ` : `<div class="empty">열린 업무가 없습니다. 새 업무가 배정되면 여기에 보입니다.</div>`}
      ${openMyTasks.length ? '' : '<p class="notice">오늘은 현재 열린 업무가 없습니다.</p>'}
    </section>
  `;
}

function renderTeamOverview(teams, missingLogs) {
  return `
    <section class="panel panel-third">
      <div class="panel-head">
        <div>
          <h2>팀별 현황</h2>
          <p>대표가 한 번에 보는 팀 상태입니다.</p>
        </div>
      </div>
      <div class="card-grid">
        ${teams.map((team) => `
          <article class="list-card">
            <h3>${team.manager_name} · ${team.team}</h3>
            <p class="list-meta">열린 업무 ${team.open_tasks}건 · 지연 ${team.overdue_tasks}건 · 오늘 일지 ${team.today_logs}건</p>
          </article>
        `).join('') || '<div class="empty">등록된 팀 현황이 없습니다.</div>'}
      </div>
      <div class="panel-head" style="margin-top:18px;">
        <div>
          <h3>업무일지 미작성</h3>
        </div>
      </div>
      ${missingLogs.length ? `
        <div class="card-grid">
          ${missingLogs.map((item) => `<article class="list-card"><h3>${item.name}</h3><p class="list-meta">${item.team} · 열린 업무 ${item.open_task_count}건</p></article>`).join('')}
        </div>
      ` : '<div class="empty">오늘 기준 미작성자는 없습니다.</div>'}
    </section>
  `;
}

function renderManagerOverview(reportees, missingLogs) {
  return `
    <section class="panel panel-third">
      <div class="panel-head">
        <div>
          <h2>팀원 상태</h2>
          <p>팀장이 바로 분담과 점검에 들어갈 수 있는 카드입니다.</p>
        </div>
      </div>
      <div class="card-grid">
        ${reportees.map((person) => `
          <article class="list-card">
            <h3>${person.name}</h3>
            <p class="list-meta">열린 업무 ${person.open_tasks}건 · 지연 ${person.overdue_tasks}건 · 오늘 일지 ${person.has_today_log ? '작성' : '미작성'}</p>
          </article>
        `).join('') || '<div class="empty">직속 팀원이 없습니다.</div>'}
      </div>
      <div class="panel-head" style="margin-top:18px;">
        <div>
          <h3>즉시 체크 필요</h3>
        </div>
      </div>
      ${missingLogs.length ? `
        <div class="card-grid">
          ${missingLogs.map((item) => `<article class="list-card"><h3>${item.name}</h3><p class="list-meta">열린 업무 ${item.open_task_count}건인데 오늘 일지가 없습니다.</p></article>`).join('')}
        </div>
      ` : '<div class="empty">오늘 기준 놓친 팀원은 없습니다.</div>'}
    </section>
  `;
}

function renderTasksPanel(user, tasks) {
  const visibleTasks = user.role === 'member' ? tasks.filter((task) => task.assignee_id === user.id) : tasks;
  return `
    <section class="panel panel-wide">
      <div class="panel-head">
        <div>
          <h2>${user.role === 'member' ? '내 업무 보드' : '가시 범위 업무 보드'}</h2>
          <p>${user.role === 'member' ? '내가 지금 해야 하는 일만 집중해서 볼 수 있습니다.' : '상위 권한은 하위 조직 업무까지 한 번에 확인할 수 있습니다.'}</p>
        </div>
      </div>
      ${visibleTasks.length ? `
        <div class="task-grid">
          ${visibleTasks.map((task) => renderTaskCard(task, user)).join('')}
        </div>
      ` : '<div class="empty">표시할 업무가 없습니다.</div>'}
    </section>
  `;
}

function renderTaskCard(task, user) {
  const canEdit = user.role !== 'member' || task.assignee_id === user.id;
  return `
    <article class="task-card">
      <div class="badges">
        <span class="badge role-${task.assignee_role}">${escapeHtml(task.assignee_name)}</span>
        <span class="badge status-${task.status}">${task.status_label}</span>
        <span class="badge">${task.priority_label}</span>
      </div>
      <h3>${escapeHtml(task.title)}</h3>
      <p>${escapeHtml(task.description || '지시 내용이 아직 없습니다.')}</p>
      <p class="task-meta">지시자 ${escapeHtml(task.created_by_name)} · 마감 ${task.due_date}${task.parent_task_title ? ` · 상위 업무 ${escapeHtml(task.parent_task_title)}` : ''}</p>
      ${canEdit ? `
        <div class="task-footer">
          <select data-task-status="${task.id}">
            ${['planned','in_progress','blocked','review','done'].map((status) => `<option value="${status}" ${task.status === status ? 'selected' : ''}>${statusLabel(status)}</option>`).join('')}
          </select>
          <button class="secondary" data-task-save="${task.id}">상태 반영</button>
        </div>
      ` : ''}
    </article>
  `;
}

function renderWorklogPanel(user, openMyTasks, logsToday) {
  const myLogs = logsToday.filter((log) => log.user_id === user.id);
  return `
    <section class="panel panel-half">
      <div class="panel-head">
        <div>
          <h2>오늘 업무일지</h2>
          <p>${user.role === 'member' ? '오늘 한 일과 다음 행동을 남기면 팀장이 바로 확인할 수 있습니다.' : '관리자도 본인 업무 로그를 남길 수 있습니다.'}</p>
        </div>
      </div>
      ${openMyTasks.length ? `
        <form id="worklogForm" class="form-grid">
          <div class="field full">
            <label for="worklogTask">업무 선택</label>
            <select id="worklogTask" name="task_id">${openMyTasks.map((task) => `<option value="${task.id}">${escapeHtml(task.title)}</option>`).join('')}</select>
          </div>
          <div class="field full">
            <label for="todayDone">오늘 한 일</label>
            <textarea id="todayDone" name="today_done" placeholder="오늘 실제로 끝낸 일, 확인한 내용, 전달한 내용"></textarea>
          </div>
          <div class="field full">
            <label for="nextPlan">다음 액션</label>
            <textarea id="nextPlan" name="next_plan" placeholder="다음으로 할 일, 내일 이어갈 일"></textarea>
          </div>
          <div class="field full">
            <label for="blockers">막힌 점</label>
            <textarea id="blockers" name="blockers" placeholder="지원이 필요하면 여기 적습니다."></textarea>
          </div>
          <div class="field">
            <label for="progress">진행률</label>
            <input id="progress" name="progress" type="number" min="0" max="100" value="50">
          </div>
          <div class="form-actions">
            <button type="submit">업무일지 저장</button>
            <span id="worklogNotice" class="notice"></span>
          </div>
        </form>
      ` : '<div class="empty">내 이름으로 배정된 열린 업무가 있어야 업무일지를 쓸 수 있습니다.</div>'}
      <div class="panel-head" style="margin-top:18px;">
        <div>
          <h3>오늘 작성한 기록</h3>
        </div>
      </div>
      ${myLogs.length ? `
        <div class="card-grid">
          ${myLogs.map((log) => `
            <article class="list-card">
              <div class="badges"><span class="badge ${log.review_status === 'approved' ? 'status-done' : log.review_status === 'needs_update' ? 'status-blocked' : 'status-review'}">${reviewLabel(log.review_status)}</span></div>
              <h3>${escapeHtml(log.task_title)}</h3>
              <p>${escapeHtml(log.today_done)}</p>
              <p class="list-meta">다음 액션: ${escapeHtml(log.next_plan)} · 진행률 ${log.progress}%</p>
            </article>
          `).join('')}
        </div>
      ` : '<div class="empty">아직 오늘 작성한 업무일지가 없습니다.</div>'}
    </section>
  `;
}

function renderReviewPanel(reviewQueue) {
  return `
    <section class="panel panel-half">
      <div class="panel-head">
        <div>
          <h2>검토 큐</h2>
          <p>팀장과 대표는 제출된 업무일지를 승인하거나 보완 요청할 수 있습니다.</p>
        </div>
      </div>
      ${reviewQueue.length ? `
        <div class="card-grid">
          ${reviewQueue.map((log) => `
            <article class="review-card">
              <div class="badges"><span class="badge status-review">${reviewLabel(log.review_status)}</span><span class="badge">${escapeHtml(log.user_name)}</span></div>
              <h3>${escapeHtml(log.task_title)}</h3>
              <p>${escapeHtml(log.today_done)}</p>
              <p class="review-meta">다음 액션: ${escapeHtml(log.next_plan)} · 막힌 점: ${escapeHtml(log.blockers || '없음')}</p>
              <div class="review-footer">
                <button class="secondary" data-review-id="${log.id}" data-review-status="approved">승인</button>
                <button class="ghost" data-review-id="${log.id}" data-review-status="needs_update">보완 요청</button>
              </div>
            </article>
          `).join('')}
        </div>
      ` : '<div class="empty">오늘 검토 대기 중인 업무일지가 없습니다.</div>'}
    </section>
  `;
}

function bindForms() {
  document.getElementById('taskForm')?.addEventListener('submit', handleTaskCreate);
  document.getElementById('weeklyFocusForm')?.addEventListener('submit', handleWeeklyFocusSave);
  document.getElementById('worklogForm')?.addEventListener('submit', handleWorklogSave);

  document.querySelectorAll('[data-task-save]').forEach((button) => {
    button.addEventListener('click', async () => {
      const taskId = Number(button.dataset.taskSave);
      const status = document.querySelector(`[data-task-status="${taskId}"]`).value;
      await api(`/api/org/tasks/${taskId}`, {
        method: 'PATCH',
        body: JSON.stringify({ actor_id: state.currentUserId, status }),
      });
      await loadState();
    });
  });

  document.querySelectorAll('[data-review-id]').forEach((button) => {
    button.addEventListener('click', async () => {
      const reviewId = Number(button.dataset.reviewId);
      const reviewStatus = button.dataset.reviewStatus;
      const reviewNote = reviewStatus === 'needs_update' ? '보완이 필요합니다. 다음 액션을 더 구체적으로 적어주세요.' : '확인했습니다.';
      await api(`/api/org/worklogs/${reviewId}/review`, {
        method: 'POST',
        body: JSON.stringify({ actor_id: state.currentUserId, review_status: reviewStatus, review_note: reviewNote }),
      });
      await loadState();
    });
  });
}

async function handleTaskCreate(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = {
    actor_id: state.currentUserId,
    title: form.title.value,
    assignee_id: Number(form.assignee_id.value),
    due_date: form.due_date.value,
    priority: Number(form.priority.value),
    description: form.description.value,
    parent_task_id: form.parent_task_id.value ? Number(form.parent_task_id.value) : null,
  };
  await api('/api/org/tasks', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
  document.getElementById('taskFormNotice').textContent = '업무를 배정했습니다.';
  form.reset();
  form.due_date.value = state.data.today;
  await loadState();
}

async function handleWeeklyFocusSave(event) {
  event.preventDefault();
  const form = event.currentTarget;
  await api('/api/org/weekly-focus', {
    method: 'POST',
    body: JSON.stringify({
      actor_id: state.currentUserId,
      focus: form.focus.value,
      support_needed: form.support_needed.value,
    }),
  });
  document.getElementById('weeklyFocusNotice').textContent = '주간 포커스를 저장했습니다.';
  await loadState();
}

async function handleWorklogSave(event) {
  event.preventDefault();
  const form = event.currentTarget;
  await api('/api/org/worklogs', {
    method: 'POST',
    body: JSON.stringify({
      actor_id: state.currentUserId,
      task_id: Number(form.task_id.value),
      today_done: form.today_done.value,
      next_plan: form.next_plan.value,
      blockers: form.blockers.value,
      progress: Number(form.progress.value),
    }),
  });
  document.getElementById('worklogNotice').textContent = '업무일지를 저장했습니다.';
  form.reset();
  form.progress.value = 50;
  await loadState();
}

async function api(url, options = {}) {
  const response = await fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!response.ok) {
    const errorText = await response.text();
    throw new Error(errorText || '요청에 실패했습니다.');
  }
  return response.json();
}

function metricCard(label, value) {
  return `
    <article class="metric-card">
      <div class="metric-label">${label}</div>
      <div class="metric-value">${value}</div>
    </article>
  `;
}

function roleDescription(role) {
  if (role === 'ceo') return '대표 화면에서는 전사 진행률, 지연 업무, 미작성 업무일지를 가장 먼저 보여줍니다.';
  if (role === 'manager') return '팀장 화면에서는 분담, 점검, 검토 요청 처리가 가장 먼저 보입니다.';
  return '팀원 화면에서는 오늘 해야 할 일, 주간 포커스, 업무일지를 놓치지 않게 설계했습니다.';
}

function statusLabel(status) {
  return {
    planned: '예정',
    in_progress: '진행중',
    blocked: '막힘',
    review: '검토요청',
    done: '완료',
  }[status] || status;
}

function reviewLabel(status) {
  return {
    submitted: '제출됨',
    approved: '승인됨',
    needs_update: '보완요청',
  }[status] || status;
}

function renderEmpty(message, isError = false) {
  app.innerHTML = `<section class="panel panel-wide"><div class="empty ${isError ? 'warning' : ''}">${escapeHtml(message)}</div></section>`;
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}
