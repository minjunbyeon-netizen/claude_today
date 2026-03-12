const STORAGE_MODE_KEY = "org_focus_session_mode";
const STORAGE_USER_KEY = "org_focus_session_user";

const state = {
  users: [],
  demoAccounts: [],
  currentUserId: null,
  sessionMode: null,
  data: null,
};

const metricLabels = [
  ["open_tasks", "열린 업무", "현재 처리 중인 전체 업무"],
  ["overdue_tasks", "지연 업무", "마감일이 지난 업무"],
  ["review_tasks", "검토 대기", "승인 또는 보완이 필요한 업무"],
  ["today_log_count", "오늘 업무일지", "오늘 작성된 로그 수"],
  ["visible_user_count", "가시 인원", "현재 역할로 볼 수 있는 인원"],
];

const elAccessGate = document.getElementById("accessGate");
const elApp = document.getElementById("app");
const elSessionBadge = document.getElementById("sessionBadge");
const elUserMeta = document.getElementById("userMeta");
const elSessionControls = document.getElementById("sessionControls");

init();

async function init() {
  if (!elAccessGate || !elApp || !elSessionBadge || !elUserMeta || !elSessionControls) {
    document.body.innerHTML = `
      <div style="padding:24px;font:14px/1.5 'Segoe UI',sans-serif;color:#b42318;">
        Dashboard shell is incomplete. Reload after updating <code>static/index.html</code>.
      </div>
    `;
    return;
  }

  try {
    const [users, demoAccounts] = await Promise.all([
      api("/api/org/users"),
      api("/api/org/demo-accounts"),
    ]);
    state.users = users;
    state.demoAccounts = demoAccounts;

    const storedMode = localStorage.getItem(STORAGE_MODE_KEY);
    const storedUserId = Number(localStorage.getItem(STORAGE_USER_KEY));
    const storedUser = findUser(storedUserId);

    if (storedMode && storedUser) {
      await startSession(storedMode, storedUser.id);
      return;
    }

    renderAccessGate();
  } catch (error) {
    renderFatal(error.message);
  }
}

function findUser(userId) {
  return state.users.find((user) => user.id === Number(userId)) || null;
}

async function startSession(mode, userId) {
  const user = findUser(userId) || state.users[0];
  if (!user) {
    renderFatal("No available user was found for this preview.");
    return;
  }
  state.sessionMode = mode;
  state.currentUserId = user.id;
  localStorage.setItem(STORAGE_MODE_KEY, mode);
  localStorage.setItem(STORAGE_USER_KEY, String(user.id));
  renderSessionChrome(user);

  try {
    await loadState({ onError: "gate" });
  } catch (error) {
    state.sessionMode = null;
    state.currentUserId = null;
    state.data = null;
    localStorage.removeItem(STORAGE_MODE_KEY);
    localStorage.removeItem(STORAGE_USER_KEY);
    renderAccessGate(error.message);
  }
}

function endSession() {
  state.sessionMode = null;
  state.currentUserId = null;
  state.data = null;
  localStorage.removeItem(STORAGE_MODE_KEY);
  localStorage.removeItem(STORAGE_USER_KEY);
  renderAccessGate();
}

function renderSessionChrome(user) {
  if (!user || !state.sessionMode) {
    elSessionBadge.textContent = "준비 중";
    elUserMeta.textContent = "비로그인 체험 또는 테스트 로그인으로 시작하세요.";
    elSessionControls.innerHTML = `
      <div class="session-card">가상 계정 ${state.demoAccounts.length}개 · 빠른 체험 1개 모드</div>
    `;
    return;
  }

  if (state.sessionMode === "guest") {
    elSessionBadge.textContent = "비로그인 체험";
    elUserMeta.innerHTML = `
      <span class="badge role-${user.role}">${escapeHtml(user.role_label)}</span>
      <span>${escapeHtml(user.name)} · ${escapeHtml(user.title || user.role_label)}</span>
      <span>${escapeHtml(user.team || "무소속")}</span>
    `;
    elSessionControls.innerHTML = `
      <div class="control-stack">
        <label class="section-label" for="guestUserSelect">체험 사용자</label>
        <select id="guestUserSelect">
          ${state.users.map((option) => `
            <option value="${option.id}" ${option.id === user.id ? "selected" : ""}>
              ${escapeHtml(option.name)} · ${escapeHtml(option.title || option.role_label)}
            </option>
          `).join("")}
        </select>
        <div class="hero-actions">
          <button type="button" class="secondary" id="guestRefreshButton">새로고침</button>
          <button type="button" class="ghost" id="resetSessionButton">시작 화면</button>
        </div>
      </div>
    `;

    document.getElementById("guestUserSelect")?.addEventListener("change", async (event) => {
      await startSession("guest", Number(event.target.value));
    });
    document.getElementById("guestRefreshButton")?.addEventListener("click", async () => {
      await loadState();
    });
    document.getElementById("resetSessionButton")?.addEventListener("click", endSession);
    return;
  }

  elSessionBadge.textContent = "테스트 로그인";
  elUserMeta.innerHTML = `
    <span class="badge role-${user.role}">${escapeHtml(user.role_label)}</span>
    <span>${escapeHtml(user.name)} · ${escapeHtml(user.title || user.role_label)}</span>
    <span>${escapeHtml(user.team || "무소속")}</span>
  `;
  elSessionControls.innerHTML = `
    <div class="control-stack">
      <div class="session-card">로그인 계정: <strong>${escapeHtml(user.demo_login_id || user.name)}</strong></div>
      <div class="hero-actions">
        <button type="button" class="secondary" id="switchAccountButton">다른 계정</button>
        <button type="button" class="ghost" id="logoutButton">로그아웃</button>
      </div>
    </div>
  `;
  document.getElementById("switchAccountButton")?.addEventListener("click", endSession);
  document.getElementById("logoutButton")?.addEventListener("click", endSession);
}

function renderAccessGate(errorMessage = "") {
  renderSessionChrome(null);
  elAccessGate.classList.remove("hidden");
  elApp.classList.add("hidden");

  const accounts = [...state.demoAccounts].sort((a, b) => a.id - b.id);

  elAccessGate.innerHTML = `
    <div class="panel__head">
      <div>
        <p class="section-label">Access</p>
        <h3>Codex 앱처럼 바로 들어가 보고, 역할별로 전환해 점검하는 시작 화면</h3>
        <p class="panel__sub">복잡한 온보딩 없이 지금은 체험과 테스트 로그인만 제공합니다.</p>
      </div>
    </div>

    <div class="access-grid">
      <article class="access-card">
        <p class="eyebrow-inline">Quick preview</p>
        <h3>로그인 없이 구조 먼저 보기</h3>
        <p>대표, 팀장, 팀원 시점을 셀렉터로 바꿔가며 정보 구조와 UX 흐름을 빠르게 확인할 수 있습니다.</p>
        <div class="hero-actions" style="margin-top: 18px;">
          <button type="button" id="guestStartButton">비로그인으로 시작</button>
        </div>
        <p class="helper">비로그인 체험은 인증 없이 레이아웃과 동작을 둘러보는 모드입니다.</p>
      </article>

      <article class="access-card">
        <p class="eyebrow-inline">Demo login</p>
        <h3>가상 계정으로 실제처럼 들어가기</h3>
        <p>테스트용 아이디와 비밀번호를 바로 제공하므로 별도 가입 없이 역할별 화면을 확인할 수 있습니다.</p>
        <form id="demoLoginForm" class="form-grid" style="margin-top: 18px;">
          <div class="field">
            <label for="demoLoginId">테스트 아이디</label>
            <input id="demoLoginId" name="login_id" placeholder="예: ceo" required>
          </div>
          <div class="field">
            <label for="demoPassword">비밀번호</label>
            <input id="demoPassword" name="password" type="password" placeholder="예: 1111" required>
          </div>
          <div class="field field--full">
            <div class="form-actions">
              <button type="submit">테스트 로그인</button>
              <span class="${errorMessage ? "warning" : "helper"}">${escapeHtml(errorMessage)}</span>
            </div>
          </div>
        </form>

        <div class="account-list">
          ${accounts.map(renderDemoAccountCard).join("")}
        </div>
      </article>
    </div>
  `;

  document.getElementById("guestStartButton")?.addEventListener("click", async () => {
    await startSession("guest", state.users[0]?.id);
  });

  document.getElementById("demoLoginForm")?.addEventListener("submit", handleDemoLogin);

  document.querySelectorAll("[data-demo-account]").forEach((button) => {
    button.addEventListener("click", async () => {
      const loginId = button.dataset.loginId;
      const password = button.dataset.password;
      document.getElementById("demoLoginId").value = loginId;
      document.getElementById("demoPassword").value = password;
      await submitDemoLogin(loginId, password);
    });
  });
}

function renderDemoAccountCard(account) {
  return `
    <article class="account-card">
      <div class="task-badges">
        <span class="badge role-${account.role}">${escapeHtml(account.role_label)}</span>
        <span class="badge">${escapeHtml(account.title || account.role_label)}</span>
      </div>
      <h4>${escapeHtml(account.name)}</h4>
      <div class="account-meta">${escapeHtml(account.team || "무소속")} 조직</div>
      <div class="account-creds">
        <div>ID: ${escapeHtml(account.demo_login_id || "")}</div>
        <div>PW: ${escapeHtml(account.demo_password || "")}</div>
      </div>
      <div class="hero-actions" style="margin-top: 14px;">
        <button
          type="button"
          class="secondary"
          data-demo-account="1"
          data-login-id="${escapeHtml(account.demo_login_id || "")}"
          data-password="${escapeHtml(account.demo_password || "")}"
        >
          이 계정으로 들어가기
        </button>
      </div>
    </article>
  `;
}

async function handleDemoLogin(event) {
  event.preventDefault();
  const form = event.currentTarget;
  await submitDemoLogin(form.login_id.value, form.password.value);
}

async function submitDemoLogin(loginId, password) {
  try {
    const result = await api("/api/org/demo-login", {
      method: "POST",
      body: JSON.stringify({ login_id: loginId, password }),
    });
    await startSession("login", result.user.id);
  } catch (error) {
    renderAccessGate(error.message);
  }
}

async function loadState({ onError = "fatal" } = {}) {
  try {
    state.data = await api(`/api/org/state?user_id=${state.currentUserId}`);
    renderDashboard();
  } catch (error) {
    if (onError === "gate") {
      renderAccessGate(error.message);
    } else {
      renderFatal(error.message);
    }
    throw error;
  }
}

function renderDashboard() {
  const {
    user,
    summary,
    highlights,
    tasks,
    reminders,
    my_weekly_focus: myWeeklyFocus,
    teams,
    reportees,
    missing_logs: missingLogs,
    review_queue: reviewQueue,
    assignee_options: assigneeOptions,
    logs_today: logsToday,
  } = state.data;

  renderSessionChrome(user);
  elAccessGate.classList.add("hidden");
  elApp.classList.remove("hidden");

  const myTasks = tasks.filter((task) => task.assignee_id === user.id);
  const openMyTasks = myTasks.filter((task) => task.status !== "done");

  elApp.innerHTML = `
    <section class="page-hero surface">
      <div class="page-hero__head">
        <div>
          <p class="section-label">${state.sessionMode === "guest" ? "Guest preview" : "Logged preview"}</p>
          <h3 class="page-hero__title">${escapeHtml(user.name)}님의 운영 화면</h3>
          <p class="page-hero__copy">${roleDescription(user.role, state.sessionMode)}</p>
        </div>
        <div class="hero-pills">
          ${highlights.map((item) => `
            <span class="highlight-chip ${item.includes("지연") || item.includes("미작성") || item.includes("막힘") ? "warn" : ""}">
              ${escapeHtml(item)}
            </span>
          `).join("")}
        </div>
      </div>
    </section>

    <section class="metrics-grid">
      ${metricLabels.map(([key, label, hint]) => renderMetricCard(label, summary[key] ?? 0, hint)).join("")}
    </section>

    <section class="dashboard-grid">
      <div class="stack">
        ${user.role !== "member" ? renderAssignmentPanel(assigneeOptions, tasks) : ""}
        ${renderWeeklyFocusPanel(myWeeklyFocus)}
        ${renderWorklogPanel(user, openMyTasks, logsToday)}
      </div>
      <div class="stack">
        ${renderRoleContextPanel(user)}
        ${renderRoleSpecificPanel(user, reminders, teams, reportees, missingLogs)}
        ${user.role !== "member" ? renderReviewPanel(reviewQueue) : ""}
      </div>
    </section>

    ${renderTasksPanel(user, tasks)}
  `;

  bindDashboardEvents();
}

function renderMetricCard(label, value, hint) {
  return `
    <article class="metric-card">
      <div class="metric-card__label">${label}</div>
      <div class="metric-card__value">${value}</div>
      <div class="metric-card__hint">${hint}</div>
    </article>
  `;
}

function renderAssignmentPanel(assigneeOptions, tasks) {
  return `
    <section class="panel">
      <div class="panel__head">
        <div>
          <p class="section-label">Assignment</p>
          <h3>업무 배정</h3>
          <p class="panel__sub">업무 제목, 담당자, 마감일, 상위 업무를 바로 연결할 수 있습니다.</p>
        </div>
      </div>
      <form id="taskForm" class="form-grid">
        <div class="field field--full">
          <label for="taskTitle">업무 제목</label>
          <input id="taskTitle" name="title" placeholder="예: 고객사 제안서 1차 초안 작성" required>
        </div>
        <div class="field">
          <label for="taskAssignee">담당자</label>
          <select id="taskAssignee" name="assignee_id">
            ${assigneeOptions.map((option) => `
              <option value="${option.id}">
                ${escapeHtml(option.name)} · ${escapeHtml(option.title || option.role_label)}
              </option>
            `).join("")}
          </select>
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
            ${tasks.map((task) => `<option value="${task.id}">${escapeHtml(task.title)}</option>`).join("")}
          </select>
        </div>
        <div class="field field--full">
          <label for="taskDescription">지시 내용</label>
          <textarea id="taskDescription" name="description" placeholder="완료 기준, 산출물, 체크포인트를 적어두면 팀원이 덜 헷갈립니다."></textarea>
        </div>
        <div class="field field--full">
          <div class="form-actions">
            <button type="submit">업무 배정하기</button>
            <span id="taskFormNotice" class="notice"></span>
          </div>
        </div>
      </form>
    </section>
  `;
}

function renderWeeklyFocusPanel(weeklyFocus) {
  return `
    <section class="panel">
      <div class="panel__head">
        <div>
          <p class="section-label">Weekly focus</p>
          <h3>이번 주 포커스</h3>
          <p class="panel__sub">이번 주 핵심 목표와 지원이 필요한 내용을 같이 남깁니다.</p>
        </div>
      </div>
      <form id="weeklyFocusForm" class="form-grid">
        <div class="field field--full">
          <label for="weeklyFocus">핵심 목표</label>
          <textarea id="weeklyFocus" name="focus" placeholder="예: 제안서 템플릿 마무리, 광고 리포트 검토 완료">${escapeHtml(weeklyFocus?.focus || "")}</textarea>
        </div>
        <div class="field field--full">
          <label for="weeklySupport">지원이 필요한 점</label>
          <textarea id="weeklySupport" name="support_needed" placeholder="예: 대표 최종 피드백 필요, 회계 자료 요청">${escapeHtml(weeklyFocus?.support_needed || "")}</textarea>
        </div>
        <div class="field field--full">
          <div class="form-actions">
            <button type="submit">주간 포커스 저장</button>
            <span id="weeklyFocusNotice" class="notice"></span>
          </div>
        </div>
      </form>
    </section>
  `;
}

function renderWorklogPanel(user, openMyTasks, logsToday) {
  const myLogs = logsToday.filter((log) => log.user_id === user.id);

  return `
    <section class="panel">
      <div class="panel__head">
        <div>
          <p class="section-label">Work log</p>
          <h3>오늘 업무일지</h3>
          <p class="panel__sub">${user.role === "member" ? "오늘 한 일과 다음 액션을 남기면 팀장이 바로 확인할 수 있습니다." : "관리자도 본인 업무 로그를 기록할 수 있습니다."}</p>
        </div>
      </div>

      ${openMyTasks.length ? `
        <form id="worklogForm" class="form-grid">
          <div class="field field--full">
            <label for="worklogTask">업무 선택</label>
            <select id="worklogTask" name="task_id">
              ${openMyTasks.map((task) => `<option value="${task.id}">${escapeHtml(task.title)}</option>`).join("")}
            </select>
          </div>
          <div class="field field--full">
            <label for="todayDone">오늘 한 일</label>
            <textarea id="todayDone" name="today_done" placeholder="오늘 실제로 처리한 내용과 전달한 내용을 적습니다."></textarea>
          </div>
          <div class="field field--full">
            <label for="nextPlan">다음 액션</label>
            <textarea id="nextPlan" name="next_plan" placeholder="다음으로 이어갈 일과 내일 할 일을 적습니다."></textarea>
          </div>
          <div class="field field--full">
            <label for="blockers">막힌 점</label>
            <textarea id="blockers" name="blockers" placeholder="지원이 필요하면 구체적으로 적습니다."></textarea>
          </div>
          <div class="field">
            <label for="progress">진행률</label>
            <input id="progress" name="progress" type="number" min="0" max="100" value="50">
          </div>
          <div class="field field--full">
            <div class="form-actions">
              <button type="submit">업무일지 저장</button>
              <span id="worklogNotice" class="notice"></span>
            </div>
          </div>
        </form>
      ` : '<div class="empty">내 이름으로 배정된 열린 업무가 있어야 업무일지를 작성할 수 있습니다.</div>'}

      <div class="panel__head" style="margin-top: 18px;">
        <div>
          <p class="section-label">Today logs</p>
          <h4>오늘 작성한 기록</h4>
        </div>
      </div>

      ${myLogs.length ? `
        <div class="list-grid">
          ${myLogs.map((log) => `
            <article class="list-card">
              <div class="task-badges">
                <span class="badge ${reviewBadgeClass(log.review_status)}">${escapeHtml(reviewLabel(log.review_status))}</span>
              </div>
              <h4>${escapeHtml(log.task_title)}</h4>
              <p>${escapeHtml(log.today_done)}</p>
              <div class="meta">다음 액션: ${escapeHtml(log.next_plan)} · 진행률 ${log.progress}%</div>
            </article>
          `).join("")}
        </div>
      ` : '<div class="empty">아직 오늘 작성한 업무일지가 없습니다.</div>'}
    </section>
  `;
}

function renderRoleContextPanel(user) {
  const cards = {
    ceo: [
      ["대표라면", "전사 지연 업무, 미작성 업무일지, 검토 대기 건을 먼저 확인하세요."],
      ["오늘의 목적", "팀장에게 위임한 일이 실제로 내려갔는지와 병목 인원이 있는지를 보는 것입니다."],
    ],
    manager: [
      ["팀장이라면", "오늘 팀원에게 내릴 일과 검토해야 할 일지를 먼저 정리하세요."],
      ["오늘의 목적", "업무 분담, 진행 막힘 파악, 보완 요청 처리를 끝내는 것입니다."],
    ],
    member: [
      ["팀원이라면", "오늘 마감 업무와 지연 업무를 먼저 보고, 퇴근 전 업무일지를 남기세요."],
      ["오늘의 목적", "지금 해야 할 한두 개 업무를 명확히 하고 다음 액션을 남기는 것입니다."],
    ],
  };

  return `
    <section class="panel">
      <div class="panel__head">
        <div>
          <p class="section-label">Control points</p>
          <h3>오늘의 운영 포인트</h3>
        </div>
      </div>
      <div class="list-grid">
        ${cards[user.role].map(([title, copy]) => `
          <article class="list-card">
            <h4>${title}</h4>
            <p>${copy}</p>
          </article>
        `).join("")}
      </div>
    </section>
  `;
}

function renderRoleSpecificPanel(user, reminders, teams, reportees, missingLogs) {
  if (user.role === "ceo") {
    return `
      <section class="panel">
        <div class="panel__head">
          <div>
            <p class="section-label">Company view</p>
            <h3>팀별 현황</h3>
          </div>
        </div>
        <div class="list-grid">
          ${teams.length ? teams.map((team) => `
            <article class="list-card">
              <h4>${escapeHtml(team.manager_name)} · ${escapeHtml(team.team)}</h4>
              <p>열린 업무 ${team.open_tasks}건 · 지연 ${team.overdue_tasks}건 · 오늘 일지 ${team.today_logs}건</p>
            </article>
          `).join("") : '<div class="empty">팀 현황 데이터가 없습니다.</div>'}
        </div>
        <div class="panel__head" style="margin-top: 18px;">
          <div>
            <p class="section-label">Missing logs</p>
            <h4>업무일지 미작성</h4>
          </div>
        </div>
        ${renderMissingLogList(missingLogs)}
      </section>
    `;
  }

  if (user.role === "manager") {
    return `
      <section class="panel">
        <div class="panel__head">
          <div>
            <p class="section-label">Team view</p>
            <h3>팀원 상태</h3>
          </div>
        </div>
        <div class="list-grid">
          ${reportees.length ? reportees.map((person) => `
            <article class="list-card">
              <h4>${escapeHtml(person.name)}</h4>
              <p>열린 업무 ${person.open_tasks}건 · 지연 ${person.overdue_tasks}건 · 오늘 일지 ${person.has_today_log ? "작성" : "미작성"}</p>
            </article>
          `).join("") : '<div class="empty">직속 팀원이 없습니다.</div>'}
        </div>
        <div class="panel__head" style="margin-top: 18px;">
          <div>
            <p class="section-label">Missing logs</p>
            <h4>즉시 체크 필요</h4>
          </div>
        </div>
        ${renderMissingLogList(missingLogs)}
      </section>
    `;
  }

  return `
    <section class="panel">
      <div class="panel__head">
        <div>
          <p class="section-label">Reminders</p>
          <h3>오늘 잊지 말 것</h3>
        </div>
      </div>
      ${reminders.length ? `
        <div class="list-grid">
          ${reminders.map((item) => `
            <article class="list-card">
              <div class="task-badges">
                <span class="badge ${item.kind === "지연" ? "status-blocked" : ""}">${escapeHtml(item.kind)}</span>
              </div>
              <h4>${escapeHtml(item.title)}</h4>
              <p>마감일 ${escapeHtml(item.due_date)}</p>
            </article>
          `).join("")}
        </div>
      ` : '<div class="empty">현재 표시할 리마인더가 없습니다.</div>'}
    </section>
  `;
}

function renderMissingLogList(items) {
  if (!items.length) {
    return '<div class="empty">오늘 기준 놓친 인원이 없습니다.</div>';
  }
  return `
    <div class="list-grid">
      ${items.map((item) => `
        <article class="list-card">
          <h4>${escapeHtml(item.name)}</h4>
          <p>${escapeHtml(item.team)} · 열린 업무 ${item.open_task_count}건</p>
        </article>
      `).join("")}
    </div>
  `;
}

function renderReviewPanel(reviewQueue) {
  return `
    <section class="panel">
      <div class="panel__head">
        <div>
          <p class="section-label">Review queue</p>
          <h3>검토 큐</h3>
          <p class="panel__sub">제출된 업무일지를 승인하거나 보완 요청할 수 있습니다.</p>
        </div>
      </div>
      ${reviewQueue.length ? `
        <div class="review-list">
          ${reviewQueue.map((log) => `
            <article class="review-card">
              <div class="task-badges">
                <span class="badge status-review">${escapeHtml(reviewLabel(log.review_status))}</span>
                <span class="badge">${escapeHtml(log.user_name)}</span>
              </div>
              <h4>${escapeHtml(log.task_title)}</h4>
              <p>${escapeHtml(log.today_done)}</p>
              <div class="meta">다음 액션: ${escapeHtml(log.next_plan)} · 막힌 점: ${escapeHtml(log.blockers || "없음")}</div>
              <div class="review-card__foot" style="margin-top: 14px;">
                <button type="button" class="secondary" data-review-id="${log.id}" data-review-status="approved">승인</button>
                <button type="button" class="ghost" data-review-id="${log.id}" data-review-status="needs_update">보완 요청</button>
              </div>
            </article>
          `).join("")}
        </div>
      ` : '<div class="empty">오늘 검토 대기 중인 업무일지가 없습니다.</div>'}
    </section>
  `;
}

function renderTasksPanel(user, tasks) {
  const visibleTasks = user.role === "member"
    ? tasks.filter((task) => task.assignee_id === user.id)
    : tasks;

  return `
    <section class="panel">
      <div class="panel__head">
        <div>
          <p class="section-label">Task board</p>
          <h3>${user.role === "member" ? "내 업무 보드" : "가시 범위 업무 보드"}</h3>
          <p class="panel__sub">${user.role === "member" ? "내가 지금 해야 하는 일만 집중해서 봅니다." : "상위 권한은 하위 조직 업무까지 함께 봅니다."}</p>
        </div>
      </div>
      ${visibleTasks.length ? `
        <div class="task-list">
          ${visibleTasks.map((task) => renderTaskRow(task, user)).join("")}
        </div>
      ` : '<div class="empty">표시할 업무가 없습니다.</div>'}
    </section>
  `;
}

function renderTaskRow(task, user) {
  const canEdit = user.role !== "member" || task.assignee_id === user.id;

  return `
    <article class="task-row">
      <div class="task-main">
        <div class="task-badges">
          <span class="badge role-${task.assignee_role}">${escapeHtml(task.assignee_name)}</span>
          <span class="badge ${statusBadgeClass(task.status)}">${escapeHtml(task.status_label)}</span>
          <span class="badge">${escapeHtml(task.priority_label)}</span>
        </div>
        <h4 style="margin-top: 12px;">${escapeHtml(task.title)}</h4>
        <p>${escapeHtml(task.description || "지시 내용이 아직 없습니다.")}</p>
        <div class="task-meta">
          지시자 ${escapeHtml(task.created_by_name)} · 마감 ${escapeHtml(task.due_date)}${task.parent_task_title ? ` · 상위 업무 ${escapeHtml(task.parent_task_title)}` : ""}
        </div>
      </div>
      <div class="task-side">
        ${canEdit ? `
          <select data-task-status="${task.id}">
            ${["planned", "in_progress", "blocked", "review", "done"].map((status) => `
              <option value="${status}" ${task.status === status ? "selected" : ""}>${statusLabel(status)}</option>
            `).join("")}
          </select>
          <div class="task-side__foot">
            <button type="button" class="secondary" data-task-save="${task.id}">상태 반영</button>
          </div>
        ` : '<div class="helper">읽기 전용</div>'}
      </div>
    </article>
  `;
}

function bindDashboardEvents() {
  document.getElementById("taskForm")?.addEventListener("submit", handleTaskCreate);
  document.getElementById("weeklyFocusForm")?.addEventListener("submit", handleWeeklyFocusSave);
  document.getElementById("worklogForm")?.addEventListener("submit", handleWorklogSave);

  document.querySelectorAll("[data-task-save]").forEach((button) => {
    button.addEventListener("click", async () => {
      const taskId = Number(button.dataset.taskSave);
      const status = document.querySelector(`[data-task-status="${taskId}"]`).value;
      await api(`/api/org/tasks/${taskId}`, {
        method: "PATCH",
        body: JSON.stringify({
          actor_id: state.currentUserId,
          status,
        }),
      });
      await loadState();
    });
  });

  document.querySelectorAll("[data-review-id]").forEach((button) => {
    button.addEventListener("click", async () => {
      const reviewId = Number(button.dataset.reviewId);
      const reviewStatus = button.dataset.reviewStatus;
      const reviewNote = reviewStatus === "needs_update"
        ? "보완이 필요합니다. 다음 액션을 더 구체적으로 적어주세요."
        : "확인했습니다.";
      await api(`/api/org/worklogs/${reviewId}/review`, {
        method: "POST",
        body: JSON.stringify({
          actor_id: state.currentUserId,
          review_status: reviewStatus,
          review_note: reviewNote,
        }),
      });
      await loadState();
    });
  });
}

async function handleTaskCreate(event) {
  event.preventDefault();
  const form = event.currentTarget;
  await api("/api/org/tasks", {
    method: "POST",
    body: JSON.stringify({
      actor_id: state.currentUserId,
      title: form.title.value,
      assignee_id: Number(form.assignee_id.value),
      due_date: form.due_date.value,
      priority: Number(form.priority.value),
      description: form.description.value,
      parent_task_id: form.parent_task_id.value ? Number(form.parent_task_id.value) : null,
    }),
  });
  document.getElementById("taskFormNotice").textContent = "업무를 배정했습니다.";
  form.reset();
  form.due_date.value = state.data.today;
  await loadState();
}

async function handleWeeklyFocusSave(event) {
  event.preventDefault();
  const form = event.currentTarget;
  await api("/api/org/weekly-focus", {
    method: "POST",
    body: JSON.stringify({
      actor_id: state.currentUserId,
      focus: form.focus.value,
      support_needed: form.support_needed.value,
    }),
  });
  document.getElementById("weeklyFocusNotice").textContent = "주간 포커스를 저장했습니다.";
  await loadState();
}

async function handleWorklogSave(event) {
  event.preventDefault();
  const form = event.currentTarget;
  await api("/api/org/worklogs", {
    method: "POST",
    body: JSON.stringify({
      actor_id: state.currentUserId,
      task_id: Number(form.task_id.value),
      today_done: form.today_done.value,
      next_plan: form.next_plan.value,
      blockers: form.blockers.value,
      progress: Number(form.progress.value),
    }),
  });
  document.getElementById("worklogNotice").textContent = "업무일지를 저장했습니다.";
  form.reset();
  form.progress.value = 50;
  await loadState();
}

async function api(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });

  if (!response.ok) {
    let message = "요청에 실패했습니다.";
    try {
      const data = await response.json();
      message = data.detail || message;
    } catch (error) {
      const text = await response.text();
      message = text || message;
    }
    throw new Error(message);
  }

  return response.json();
}

function renderFatal(message) {
  elAccessGate.classList.remove("hidden");
  elApp.classList.add("hidden");
  elAccessGate.innerHTML = `<div class="empty warning">${escapeHtml(message)}</div>`;
}

function roleDescription(role, mode) {
  const prefix = mode === "guest" ? "비로그인 체험 기준으로 " : "테스트 로그인 기준으로 ";
  if (role === "ceo") {
    return `${prefix}대표는 전사 진행률, 지연 업무, 업무일지 누락, 검토 대기 건을 가장 먼저 봅니다.`;
  }
  if (role === "manager") {
    return `${prefix}팀장은 팀원 배정, 진행 막힘, 업무일지 검토 큐를 먼저 정리합니다.`;
  }
  return `${prefix}팀원은 오늘 마감 업무, 주간 포커스, 업무일지를 빠르게 처리할 수 있게 구성했습니다.`;
}

function statusLabel(status) {
  return {
    planned: "예정",
    in_progress: "진행중",
    blocked: "막힘",
    review: "검토요청",
    done: "완료",
  }[status] || status;
}

function reviewLabel(status) {
  return {
    submitted: "제출됨",
    approved: "승인됨",
    needs_update: "보완요청",
  }[status] || status;
}

function statusBadgeClass(status) {
  return {
    planned: "",
    in_progress: "",
    blocked: "status-blocked",
    review: "status-review",
    done: "status-done",
  }[status] || "";
}

function reviewBadgeClass(status) {
  return {
    submitted: "status-review",
    approved: "status-done",
    needs_update: "status-blocked",
  }[status] || "";
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}
