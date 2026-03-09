// ── State ─────────────────────────────────────────────────────
const state = {
  currentUserId: null,
  data: null,
};

// ── DOM refs ──────────────────────────────────────────────────
const elApp         = document.getElementById('app');
const elAccessGate  = document.getElementById('accessGate');
const elSessionBadge= document.getElementById('sessionBadge');
const elUserMeta    = document.getElementById('userMeta');
const elSessionCtrl = document.getElementById('sessionControls');

// ── Boot ──────────────────────────────────────────────────────
init();

async function init() {
  try {
    const accounts = await api('/api/org/demo-accounts');
    renderAccessGate(accounts);
  } catch (e) {
    elAccessGate.innerHTML = `<div class="access-card"><p style="color:var(--red)">서버 연결 실패: ${escapeHtml(e.message)}</p></div>`;
  }
}

// ── Access Gate ───────────────────────────────────────────────
function renderAccessGate(accounts) {
  const roleOrder = { ceo: 0, manager: 1, member: 2 };
  const sorted = [...accounts].sort((a, b) => (roleOrder[a.role] ?? 9) - (roleOrder[b.role] ?? 9));

  elAccessGate.innerHTML = `
    <div class="access-grid">
      <div class="access-card">
        <h3>비로그인 체험</h3>
        <p>계정 없이 전체 흐름을 훑어봅니다. 대표 시점으로 바로 진입합니다.</p>
        <div style="margin-top:18px">
          <button onclick="enterAs(${sorted[0]?.id})">대표 시점으로 둘러보기</button>
        </div>
      </div>
      <div class="access-card">
        <h3>테스트 계정 로그인</h3>
        <p>역할에 맞는 화면을 직접 체험합니다. 아이디·비밀번호는 아래에 표시됩니다.</p>
        <div class="account-list">
          ${sorted.map(u => `
            <div class="account-card">
              <h4>${escapeHtml(u.name)}</h4>
              <div class="account-meta">${escapeHtml(u.title || u.role_label)} · ${escapeHtml(u.team || '무소속')}</div>
              <div class="account-creds">
                ID: <b>${escapeHtml(u.demo_login_id)}</b><br>
                PW: <b>${escapeHtml(u.demo_password)}</b>
              </div>
              <div style="margin-top:12px">
                <button class="secondary" onclick="demoLogin('${escapeHtml(u.demo_login_id)}','${escapeHtml(u.demo_password)}')">이 계정으로 로그인</button>
              </div>
            </div>
          `).join('')}
        </div>
      </div>
    </div>
  `;

  elSessionBadge.textContent = '체험 준비';
  elUserMeta.textContent = '계정을 선택하거나 비로그인으로 둘러보세요.';
  elSessionCtrl.innerHTML = '';
}

async function enterAs(userId) {
  if (!userId) return;
  state.currentUserId = userId;
  await loadAndRender();
}

async function demoLogin(loginId, password) {
  try {
    const res = await api('/api/org/demo-login', {
      method: 'POST',
      body: JSON.stringify({ login_id: loginId, password }),
    });
    state.currentUserId = res.user.id;
    await loadAndRender();
  } catch (e) {
    alert('로그인 실패: ' + e.message);
  }
}

// ── Main Dashboard ────────────────────────────────────────────
async function loadAndRender() {
  elAccessGate.innerHTML = '<div class="access-card" style="text-align:center;padding:32px">불러오는 중...</div>';
  state.data = await api(`/api/org/state?user_id=${state.currentUserId}`);
  elAccessGate.classList.add('hidden');
  elApp.classList.remove('hidden');
  render();
}

function render() {
  const { user, summary, highlights, tasks, reminders, my_weekly_focus,
          teams, reportees, missing_logs, review_queue, assignee_options, logs_today } = state.data;

  // 헤더 업데이트
  elSessionBadge.innerHTML = `<span class="badge role-${user.role}">${escapeHtml(user.role_label)}</span> ${escapeHtml(user.name)}`;
  elUserMeta.innerHTML = [
    `<span>${escapeHtml(user.team || '무소속')} 조직</span>`,
    `<span>${state.data.today}</span>`,
  ].join(' · ');
  elSessionCtrl.innerHTML = `
    <div class="control-stack">
      <button class="secondary" onclick="showAccessGate()">계정 전환</button>
    </div>
  `;

  const myTasks     = tasks.filter(t => t.assignee_id === user.id);
  const openMyTasks = myTasks.filter(t => t.status !== 'done');

  elApp.innerHTML = `
    <section class="panel panel-wide">
      <div class="panel-head">
        <div>
          <h2>${escapeHtml(user.name)}님의 오늘 운영판</h2>
          <p>${roleDescription(user.role)}</p>
        </div>
        <div class="badges">
          ${highlights.map((item, i) => `<span class="highlight-chip ${i === 1 && item.includes('미작성') ? 'warn' : ''}">${escapeHtml(item)}</span>`).join('')}
        </div>
      </div>
      <div class="metric-grid">
        ${metricLabels.map(([key, label]) => metricCard(label, summary[key] ?? 0)).join('')}
      </div>
    </section>

    ${user.role !== 'member' ? renderAssignmentPanel(user, assignee_options, tasks) : ''}
    ${renderWeeklyFocusPanel(user, my_weekly_focus)}
    ${renderReminderPanel(user, reminders, openMyTasks)}
    ${user.role === 'ceo'     ? renderTeamOverview(teams, missing_logs) : ''}
    ${user.role === 'manager' ? renderManagerOverview(reportees, missing_logs) : ''}
    ${renderTasksPanel(user, tasks)}
    ${renderWorklogPanel(user, openMyTasks, logs_today)}
    ${user.role !== 'member'  ? renderReviewPanel(review_queue) : ''}
  `;

  bindForms();
}

function showAccessGate() {
  elApp.classList.add('hidden');
  elAccessGate.classList.remove('hidden');
  init();
}

// ── Panel renderers ───────────────────────────────────────────
const metricLabels = [
  ['open_tasks',         '열린 업무'],
  ['overdue_tasks',      '지연 업무'],
  ['review_tasks',       '검토 대기'],
  ['today_log_count',    '오늘 업무일지'],
  ['visible_user_count', '보이는 인원'],
];

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
        <div class="field full"><label>업무 제목</label><input id="taskTitle" name="title" placeholder="예: 고객사 제안서 1차 초안 작성" required></div>
        <div class="field"><label>담당자</label><select id="taskAssignee" name="assignee_id">${assigneeOptions.map(o => `<option value="${o.id}">${escapeHtml(o.name)} · ${escapeHtml(o.role_label)}</option>`).join('')}</select></div>
        <div class="field"><label>마감일</label><input id="taskDueDate" name="due_date" type="date" value="${state.data.today}"></div>
        <div class="field"><label>우선순위</label><select id="taskPriority" name="priority"><option value="1">1 - 긴급</option><option value="2" selected>2 - 중요</option><option value="3">3 - 일반</option></select></div>
        <div class="field"><label>상위 업무</label><select id="taskParent" name="parent_task_id"><option value="">없음</option>${tasks.map(t => `<option value="${t.id}">${escapeHtml(t.title)}</option>`).join('')}</select></div>
        <div class="field full"><label>지시 내용</label><textarea id="taskDescription" name="description" placeholder="완료 기준, 전달 방식, 체크포인트를 적어두면 팀원이 덜 헷갈립니다."></textarea></div>
        <div class="form-actions"><button type="submit">업무 배정하기</button><span id="taskFormNotice" class="notice"></span></div>
      </form>
    </section>`;
}

function renderWeeklyFocusPanel(user, weeklyFocus) {
  return `
    <section class="panel panel-half">
      <div class="panel-head"><div><h2>이번 주 포커스</h2><p>${user.role === 'member' ? '팀원이 주간 업무를 잊지 않도록 이번 주 핵심 목표를 고정합니다.' : '관리자도 이번 주에 반드시 챙길 우선순위를 남길 수 있습니다.'}</p></div></div>
      <form id="weeklyFocusForm" class="form-grid">
        <div class="field full"><label>이번 주 핵심 목표</label><textarea id="weeklyFocus" name="focus" placeholder="예: 제안서 템플릿 마무리, 광고 리포트 검토 완료">${escapeHtml(weeklyFocus?.focus || '')}</textarea></div>
        <div class="field full"><label>지원이 필요한 점</label><textarea id="weeklySupport" name="support_needed" placeholder="예: 대표 최종 피드백 필요, 회계 데이터 요청">${escapeHtml(weeklyFocus?.support_needed || '')}</textarea></div>
        <div class="form-actions"><button type="submit">주간 포커스 저장</button><span id="weeklyFocusNotice" class="notice"></span></div>
      </form>
    </section>`;
}

function renderReminderPanel(user, reminders, openMyTasks) {
  if (user.role !== 'member') {
    return `
      <section class="panel panel-third">
        <div class="panel-head"><div><h2>오늘의 컨트롤 포인트</h2><p>역할에 맞춰 꼭 챙겨야 할 흐름만 빠르게 볼 수 있게 정리했습니다.</p></div></div>
        <div class="list-grid">
          <article class="list-card"><h3>대표라면</h3><p>지연 업무, 업무일지 미작성자, 검토 대기 업무를 먼저 확인하세요.</p></article>
          <article class="list-card"><h3>팀장이라면</h3><p>오늘 팀원 배정, 진행 막힘, 일지 검토를 끝내면 팀이 덜 흔들립니다.</p></article>
        </div>
      </section>`;
  }
  return `
    <section class="panel panel-third">
      <div class="panel-head"><div><h2>오늘 잊지 말 것</h2><p>팀원 화면에서는 오늘과 지연 업무를 먼저 보여줍니다.</p></div></div>
      ${reminders.length ? `<div class="list-grid">${reminders.map(item => `
        <article class="list-card">
          <div class="badges"><span class="badge ${item.kind === '지연' ? 'status-blocked' : ''}">${item.kind}</span></div>
          <h3>${escapeHtml(item.title)}</h3>
          <p class="list-meta">마감일 ${item.due_date}</p>
        </article>`).join('')}</div>` : `<div class="empty">열린 업무가 없습니다.</div>`}
    </section>`;
}

function renderTeamOverview(teams, missingLogs) {
  return `
    <section class="panel panel-third">
      <div class="panel-head"><div><h2>팀별 현황</h2><p>대표가 한 번에 보는 팀 상태입니다.</p></div></div>
      <div class="card-grid">
        ${teams.map(t => `<article class="list-card"><h3>${escapeHtml(t.manager_name)} · ${escapeHtml(t.team)}</h3><p class="list-meta">열린 ${t.open_tasks}건 · 지연 ${t.overdue_tasks}건 · 오늘 일지 ${t.today_logs}건</p></article>`).join('') || '<div class="empty">팀 현황 없음</div>'}
      </div>
      <div class="panel-head" style="margin-top:18px"><div><h3>업무일지 미작성</h3></div></div>
      ${missingLogs.length ? `<div class="card-grid">${missingLogs.map(m => `<article class="list-card"><h3>${escapeHtml(m.name)}</h3><p class="list-meta">${escapeHtml(m.team)} · 열린 업무 ${m.open_task_count}건</p></article>`).join('')}</div>` : '<div class="empty">오늘 기준 미작성자 없음</div>'}
    </section>`;
}

function renderManagerOverview(reportees, missingLogs) {
  return `
    <section class="panel panel-third">
      <div class="panel-head"><div><h2>팀원 상태</h2><p>팀장이 바로 분담과 점검에 들어갈 수 있는 카드입니다.</p></div></div>
      <div class="card-grid">
        ${reportees.map(p => `<article class="list-card"><h3>${escapeHtml(p.name)}</h3><p class="list-meta">열린 ${p.open_tasks}건 · 지연 ${p.overdue_tasks}건 · 오늘 일지 ${p.has_today_log ? '작성' : '미작성'}</p></article>`).join('') || '<div class="empty">직속 팀원 없음</div>'}
      </div>
      <div class="panel-head" style="margin-top:18px"><div><h3>즉시 체크 필요</h3></div></div>
      ${missingLogs.length ? `<div class="card-grid">${missingLogs.map(m => `<article class="list-card"><h3>${escapeHtml(m.name)}</h3><p class="list-meta">열린 업무 ${m.open_task_count}건인데 오늘 일지 없음</p></article>`).join('')}</div>` : '<div class="empty">오늘 기준 놓친 팀원 없음</div>'}
    </section>`;
}

function renderTasksPanel(user, tasks) {
  const visible = user.role === 'member' ? tasks.filter(t => t.assignee_id === user.id) : tasks;
  return `
    <section class="panel panel-wide">
      <div class="panel-head"><div><h2>${user.role === 'member' ? '내 업무 보드' : '가시 범위 업무 보드'}</h2><p>${user.role === 'member' ? '내가 지금 해야 하는 일만 집중해서 볼 수 있습니다.' : '상위 권한은 하위 조직 업무까지 한 번에 확인할 수 있습니다.'}</p></div></div>
      ${visible.length ? `<div class="task-grid">${visible.map(t => renderTaskCard(t, user)).join('')}</div>` : '<div class="empty">표시할 업무가 없습니다.</div>'}
    </section>`;
}

function renderTaskCard(task, user) {
  const canEdit = user.role !== 'member' || task.assignee_id === user.id;
  return `
    <article class="task-card">
      <div class="badges">
        <span class="badge role-${task.assignee_role}">${escapeHtml(task.assignee_name)}</span>
        <span class="badge status-${task.status}">${escapeHtml(task.status_label)}</span>
        <span class="badge">${escapeHtml(task.priority_label)}</span>
      </div>
      <h3>${escapeHtml(task.title)}</h3>
      <p>${escapeHtml(task.description || '지시 내용이 아직 없습니다.')}</p>
      <p class="task-meta">지시자 ${escapeHtml(task.created_by_name)} · 마감 ${task.due_date}${task.parent_task_title ? ` · 상위 ${escapeHtml(task.parent_task_title)}` : ''}</p>
      ${canEdit ? `
        <div class="task-footer">
          <select data-task-status="${task.id}">
            ${['planned','in_progress','blocked','review','done'].map(s => `<option value="${s}" ${task.status === s ? 'selected' : ''}>${statusLabel(s)}</option>`).join('')}
          </select>
          <button class="secondary" data-task-save="${task.id}">상태 반영</button>
        </div>` : ''}
    </article>`;
}

function renderWorklogPanel(user, openMyTasks, logsToday) {
  const myLogs = logsToday.filter(l => l.user_id === user.id);
  return `
    <section class="panel panel-half">
      <div class="panel-head"><div><h2>오늘 업무일지</h2><p>${user.role === 'member' ? '오늘 한 일과 다음 행동을 남기면 팀장이 바로 확인할 수 있습니다.' : '관리자도 본인 업무 로그를 남길 수 있습니다.'}</p></div></div>
      ${openMyTasks.length ? `
        <form id="worklogForm" class="form-grid">
          <div class="field full"><label>업무 선택</label><select id="worklogTask" name="task_id">${openMyTasks.map(t => `<option value="${t.id}">${escapeHtml(t.title)}</option>`).join('')}</select></div>
          <div class="field full"><label>오늘 한 일</label><textarea id="todayDone" name="today_done" placeholder="오늘 실제로 끝낸 일, 확인한 내용, 전달한 내용"></textarea></div>
          <div class="field full"><label>다음 액션</label><textarea id="nextPlan" name="next_plan" placeholder="다음으로 할 일, 내일 이어갈 일"></textarea></div>
          <div class="field full"><label>막힌 점</label><textarea id="blockers" name="blockers" placeholder="지원이 필요하면 여기 적습니다."></textarea></div>
          <div class="field"><label>진행률</label><input id="progress" name="progress" type="number" min="0" max="100" value="50"></div>
          <div class="form-actions"><button type="submit">업무일지 저장</button><span id="worklogNotice" class="notice"></span></div>
        </form>` : '<div class="empty">내 이름으로 배정된 열린 업무가 있어야 업무일지를 쓸 수 있습니다.</div>'}
      <div class="panel-head" style="margin-top:18px"><div><h3>오늘 작성한 기록</h3></div></div>
      ${myLogs.length ? `<div class="card-grid">${myLogs.map(log => `
        <article class="list-card">
          <div class="badges"><span class="badge ${log.review_status === 'approved' ? 'status-done' : log.review_status === 'needs_update' ? 'status-blocked' : 'status-review'}">${reviewLabel(log.review_status)}</span></div>
          <h3>${escapeHtml(log.task_title)}</h3>
          <p>${escapeHtml(log.today_done)}</p>
          <p class="list-meta">다음 액션: ${escapeHtml(log.next_plan)} · 진행률 ${log.progress}%</p>
        </article>`).join('')}</div>` : '<div class="empty">아직 오늘 작성한 업무일지가 없습니다.</div>'}
    </section>`;
}

function renderReviewPanel(reviewQueue) {
  return `
    <section class="panel panel-half">
      <div class="panel-head"><div><h2>검토 큐</h2><p>팀장과 대표는 제출된 업무일지를 승인하거나 보완 요청할 수 있습니다.</p></div></div>
      ${reviewQueue.length ? `<div class="card-grid">${reviewQueue.map(log => `
        <article class="review-card">
          <div class="badges"><span class="badge status-review">${reviewLabel(log.review_status)}</span><span class="badge">${escapeHtml(log.user_name)}</span></div>
          <h3>${escapeHtml(log.task_title)}</h3>
          <p>${escapeHtml(log.today_done)}</p>
          <p class="review-meta">다음 액션: ${escapeHtml(log.next_plan)} · 막힌 점: ${escapeHtml(log.blockers || '없음')}</p>
          <div class="review-footer">
            <button class="secondary" data-review-id="${log.id}" data-review-status="approved">승인</button>
            <button class="ghost" data-review-id="${log.id}" data-review-status="needs_update">보완 요청</button>
          </div>
        </article>`).join('')}</div>` : '<div class="empty">오늘 검토 대기 중인 업무일지가 없습니다.</div>'}
    </section>`;
}

// ── Form bindings ─────────────────────────────────────────────
function bindForms() {
  document.getElementById('taskForm')?.addEventListener('submit', handleTaskCreate);
  document.getElementById('weeklyFocusForm')?.addEventListener('submit', handleWeeklyFocusSave);
  document.getElementById('worklogForm')?.addEventListener('submit', handleWorklogSave);

  document.querySelectorAll('[data-task-save]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const taskId = Number(btn.dataset.taskSave);
      const status = document.querySelector(`[data-task-status="${taskId}"]`).value;
      await api(`/api/org/tasks/${taskId}`, { method: 'PATCH', body: JSON.stringify({ actor_id: state.currentUserId, status }) });
      await refreshDashboard();
    });
  });

  document.querySelectorAll('[data-review-id]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const note = btn.dataset.reviewStatus === 'needs_update' ? '보완이 필요합니다.' : '확인했습니다.';
      await api(`/api/org/worklogs/${btn.dataset.reviewId}/review`, {
        method: 'POST',
        body: JSON.stringify({ actor_id: state.currentUserId, review_status: btn.dataset.reviewStatus, review_note: note }),
      });
      await refreshDashboard();
    });
  });
}

async function handleTaskCreate(e) {
  e.preventDefault();
  const f = e.currentTarget;
  await api('/api/org/tasks', {
    method: 'POST',
    body: JSON.stringify({
      actor_id: state.currentUserId,
      title: f.title.value,
      assignee_id: Number(f.assignee_id.value),
      due_date: f.due_date.value,
      priority: Number(f.priority.value),
      description: f.description.value,
      parent_task_id: f.parent_task_id.value ? Number(f.parent_task_id.value) : null,
    }),
  });
  document.getElementById('taskFormNotice').textContent = '업무를 배정했습니다.';
  f.reset(); f.due_date.value = state.data.today;
  await refreshDashboard();
}

async function handleWeeklyFocusSave(e) {
  e.preventDefault();
  const f = e.currentTarget;
  await api('/api/org/weekly-focus', {
    method: 'POST',
    body: JSON.stringify({ actor_id: state.currentUserId, focus: f.focus.value, support_needed: f.support_needed.value }),
  });
  document.getElementById('weeklyFocusNotice').textContent = '주간 포커스를 저장했습니다.';
  await refreshDashboard();
}

async function handleWorklogSave(e) {
  e.preventDefault();
  const f = e.currentTarget;
  await api('/api/org/worklogs', {
    method: 'POST',
    body: JSON.stringify({
      actor_id: state.currentUserId,
      task_id: Number(f.task_id.value),
      today_done: f.today_done.value,
      next_plan: f.next_plan.value,
      blockers: f.blockers.value,
      progress: Number(f.progress.value),
    }),
  });
  document.getElementById('worklogNotice').textContent = '업무일지를 저장했습니다.';
  f.reset(); f.progress.value = 50;
  await refreshDashboard();
}

async function refreshDashboard() {
  state.data = await api(`/api/org/state?user_id=${state.currentUserId}`);
  render();
}

// ── Helpers ───────────────────────────────────────────────────
async function api(url, options = {}) {
  const res = await fetch(url, { headers: { 'Content-Type': 'application/json' }, ...options });
  if (!res.ok) throw new Error(await res.text() || '요청 실패');
  return res.json();
}

function metricCard(label, value) {
  return `<article class="metric-card"><div class="metric-label">${label}</div><div class="metric-value">${value}</div></article>`;
}

function roleDescription(role) {
  if (role === 'ceo')     return '대표 화면에서는 전사 진행률, 지연 업무, 미작성 업무일지를 가장 먼저 보여줍니다.';
  if (role === 'manager') return '팀장 화면에서는 분담, 점검, 검토 요청 처리가 가장 먼저 보입니다.';
  return '팀원 화면에서는 오늘 해야 할 일, 주간 포커스, 업무일지를 놓치지 않게 설계했습니다.';
}

function statusLabel(s) {
  return { planned:'예정', in_progress:'진행중', blocked:'막힘', review:'검토요청', done:'완료' }[s] || s;
}

function reviewLabel(s) {
  return { submitted:'제출됨', approved:'승인됨', needs_update:'보완요청' }[s] || s;
}

function escapeHtml(v) {
  return String(v ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'","&#39;");
}
