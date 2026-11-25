<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'

/* ---------- 全局状态 ---------- */
const ready = ref(false) // 首屏 /api/auth/me 是否结束，避免已登录用户闪现登录页
const user = ref(null), page = ref('home')
const routeId = ref(null)
const notice = ref({ type: '', text: '' }) // type: success | info | warn | error
const pending = reactive({}) // 按操作名记录请求中状态，避免一次慢请求锁死整个界面
const busy = computed(() => Object.values(pending).some(Boolean))
const isBusy = (key) => !!pending[key]
const navBusy = computed(() => isBusy('nav') || isBusy('auth'))
const draftTransitionBusy = computed(() => navBusy.value || isBusy('submitReview'))
const REQUEST_TIMEOUT_MS = 20_000
const MAX_DRAFT_DEFERRALS = 3
const inFlightRequests = new Map()
let actionVersion = 0
let sessionVersion = 0
let detailRefreshVersion = 0
let signingOut = false

const assignments = ref([]), tasks = ref([]), students = ref([])
const search = ref(''), assignmentFilter = ref('all'), reviewFilter = ref('all'), clock = ref(Date.now())
const active = ref(null), detail = ref(null), dashboard = ref(null), feedback = ref(null)
const classroomList = ref([]), classroom = ref(null), classroomName = ref(''), classInviteCode = ref(''), classImportText = ref('')
const creationClassroomName = ref(''), caseEditor = ref(null), pendingCasesOnly = ref(false)
const adoptionEditor = ref(null), revisionDraft = ref(null), teacherRevision = ref(null), learning = ref(null)
const highDisagreementOnly = ref(false), diagnosticFocus = ref(null)
const dimensionLabels = { specificity: '具体性', evidence_grounding: '依据性', actionability: '可操作性' }
const adoptionLabel = decision => ({ ADOPTED: '采纳', NOT_ADOPTED: '暂不采纳' }[decision] || '尚未记录')
const meanLabel = value => Number.isFinite(value) ? value.toFixed(1) : '—'
const login = reactive({ username: '', password: '' })
const passwordForm = reactive({ current_password: '', new_password: '', password_confirmation: '' })
function clearPasswordForm() { Object.assign(passwordForm, { current_password: '', new_password: '', password_confirmation: '' }) }
const registering = ref(false)
const registration = reactive({ username: '', display_name: '', password: '', password_confirmation: '' })
const creation = reactive({ title: '', description: '', rubric: ['内容完整性', '逻辑与表达', '分析与创新'], submit_deadline: '', review_deadline: '', student_ids: [] })
const extending = ref(false)
const extension = reactive({ submit_deadline: '', review_deadline: '' })
const text = ref(''), pdf = ref(null), selected = ref([]), quality = ref(null), source = ref(null)
const pdfInput = ref(null), focusedEvidence = ref(null)
const review = reactive({ scores: [3, 3, 3], strengths: '', weaknesses: '', suggestions: '' })
const shownReview = computed(() => detail.value?.review || review)
const shownEvidence = computed(() => detail.value?.review?.evidence_anchors || selected.value)
const readingAnchors = computed(() => diagnosticFocus.value ? [...shownEvidence.value, diagnosticFocus.value] : shownEvidence.value)
const hasUnsentReview = computed(() => {
  const submitted = detail.value?.review
  return !!submitted && JSON.stringify(reviewBody()) !== JSON.stringify(editableReview(submitted))
})
// AI 检测面向草稿；至少写下一段评语才有检测意义，也避免白耗一次模型调用。
const canCheckAI = computed(() => ['strengths', 'weaknesses', 'suggestions'].some(key => (review[key] || '').trim()))
const commentLabels = { strengths: '做得好的地方', weaknesses: '存在的问题', suggestions: '改进建议' }
const selectionInfo = ref(null) // 最近一次有效的正文选区快照 {start,end,length}
let editVersion = 0 // 用于丢弃「在途 AI 检测」返回的过期结果
const draftState = ref('idle'), savedDraft = ref(''), draftVersion = ref(null)
let draftTimer = null
let draftRequest = null
let navigationRequest = null
let leaveReviewRequest = null
let draftDeferred = false
let draftDeferrals = 0

// 一次检测只对应发起时的草稿；任何编辑（含证据增删）都使旧结果失效。
watch([review, selected], () => { quality.value = diagnosticFocus.value = null; editVersion++; scheduleDraft(true) }, { deep: true })
watch(page, (value) => {
  if (value !== 'review') { cancelDraftTimer(); draftDeferred = false; draftDeferrals = 0 }
})
// Re-arm only when a relevant transition finishes, never poll a global busy lock.
watch(draftTransitionBusy, (blocked) => {
  if (!blocked && draftDeferred) { draftDeferred = false; scheduleDraft() }
})

const statusText = {
  SUBMITTING: '提交中', SUBMISSION_ENDED: '提交已截止', REVIEWING: '互评中',
  READY_TO_PUBLISH: '等待发布', REVIEW_NOT_STARTED: '互评窗口已过', CLOSED: '已结束',
  PENDING: '待评价', COMPLETED: '已评价', REVIEW_EXPIRED: '评价已截止'
}
const aiStatusText = { RUNNING: '检测中', FAILED: '检测失败' }
const statusClass = (s) => ['SUBMITTING', 'PENDING'].includes(s) ? 'green' : ['REVIEWING', 'READY_TO_PUBLISH'].includes(s) ? 'purple' : 'muted'
const teacher = computed(() => user.value?.role === 'TEACHER')
const readOnly = computed(() => !detail.value?.can_write)
const actions = computed(() => active.value?.available_actions ?? [])
const canSubmit = computed(() => active.value?.phase === 'SUBMITTING')
const keyword = computed(() => search.value.trim().toLocaleLowerCase())
const matchesSearch = (...values) => !keyword.value || values.some(value => String(value ?? '').toLocaleLowerCase().includes(keyword.value))
const visibleAssignments = computed(() => assignments.value.filter(item => matchesSearch(item.title, item.description) &&
  (assignmentFilter.value === 'all' || (assignmentFilter.value === 'active' ? item.phase !== 'CLOSED' : item.phase === 'CLOSED'))))
const visibleTasks = computed(() => tasks.value.filter(item => matchesSearch(item.assignment_title, item.anonymous_code) &&
  (reviewFilter.value === 'all' || (reviewFilter.value === 'pending' ? item.can_write : item.phase === reviewFilter.value))))
function dueSoon(deadline) {
  const remaining = new Date(deadline).getTime() - clock.value
  return remaining > 0 && remaining <= 24 * 60 * 60 * 1000
}
function submissionReminder(item) {
  return !teacher.value && item.phase === 'SUBMITTING' && !item.has_submission && dueSoon(item.submit_deadline)
}
const urgentAssignments = computed(() => assignments.value.filter(submissionReminder).length)
const urgentReviews = computed(() => tasks.value.filter(item => item.can_write && dueSoon(item.review_deadline)).length)
function assignmentStatus(item) {
  if (teacher.value) return statusText[item.phase]
  const submitted = item.has_submission ?? !!item.my_submission
  if (item.phase === 'SUBMITTING') return submitted ? '已交' : '未交'
  if (!submitted) return item.phase === 'CLOSED' ? '未提交' : '提交已截止'
  return item.phase === 'CLOSED' ? '已发布' : item.phase === 'SUBMISSION_ENDED' ? '已交' : '等待发布'
}
const pageLabel = { home: '我的任务', create: '创建任务', assignment: '任务详情', review: '撰写互评', feedback: '收到的反馈', dashboard: '教师看板', classrooms: '我的班级', classroom: '班级详情', learning: '学习档案', account: '账号安全' }
const activeMembers = computed(() => (classroom.value?.members || []).filter(member => member.status === 'ACTIVE'))
const displayedRecords = computed(() => (dashboard.value?.records || []).filter(record => (!highDisagreementOnly.value || record.summary.high_disagreement) && (!pendingCasesOnly.value || record.reviews.some(item => item.review_case?.status === 'PENDING'))))
const codePoints = (s) => Array.from(s ?? '').length

// Keep deferred requests bound to the account that initiated them. A response from
// a previous session must never restore a private assignment or review after logout.
function clearWorkspace() {
  resetDraftTracking()
  navigationRequest = draftRequest = leaveReviewRequest = null
  sessionVersion++
  actionVersion++
  // Authentication owns its lock until the response (including Set-Cookie) arrives.
  // A concurrent 401 must not allow another login to race a pending logout.
  for (const key of Object.keys(pending)) if (key !== 'auth') delete pending[key]
  user.value = null; assignments.value = []; tasks.value = []; students.value = []
  clearPasswordForm(); login.password = ''
  active.value = detail.value = dashboard.value = feedback.value = null
  classroomList.value = []; classroom.value = caseEditor.value = null
  classroomName.value = classInviteCode.value = classImportText.value = creationClassroomName.value = ''
  pendingCasesOnly.value = false
  highDisagreementOnly.value = false
  adoptionEditor.value = revisionDraft.value = teacherRevision.value = learning.value = diagnosticFocus.value = null
  search.value = ''; assignmentFilter.value = reviewFilter.value = 'all'
  selectionInfo.value = quality.value = focusedEvidence.value = pdf.value = null
  text.value = ''; selected.value = []
  Object.assign(review, { scores: [3, 3, 3], strengths: '', weaknesses: '', suggestions: '' })
  Object.assign(creation, { title: '', description: '', rubric: ['内容完整性', '逻辑与表达', '分析与创新'], submit_deadline: '', review_deadline: '', student_ids: [] })
  Object.assign(extension, { submit_deadline: '', review_deadline: '' })
  extending.value = false
  setPage('home', null, true)
}

/* ---------- 分级提示 ---------- */
function setNotice(type, text) { notice.value = { type, text } }
function clearNotice() { notice.value = { type: '', text: '' } }

/* ---------- 接口层：区分网络失败 / 服务错误 / 业务校验失败 ---------- */
async function api(path, options = {}) {
  const viewer = user.value, session = sessionVersion
  const controller = new AbortController()
  let timedOut = false
  const deadline = setTimeout(() => { timedOut = true; controller.abort() }, REQUEST_TIMEOUT_MS)
  inFlightRequests.set(controller, deadline)
  let response, body
  try {
    response = await fetch('/api' + path, {
      credentials: 'same-origin', ...options, signal: controller.signal,
      headers: { ...(options.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }), ...options.headers }
    })
    // Keep the deadline alive through response body consumption, not just headers.
    try { body = await response.json() } catch (error) {
      if (controller.signal.aborted) throw error
      body = {}
    }
  } catch {
    const err = new Error(timedOut
      ? '请求超时（20 秒），操作结果可能尚未确认，请检查网络后重试'
      : '网络连接失败，请检查网络或服务器状态后重试')
    err.kind = timedOut ? 'timeout' : 'network'
    throw err
  } finally {
    clearTimeout(deadline)
    inFlightRequests.delete(controller)
  }
  if (!response.ok) {
    if (response.status === 401 && viewer && user.value === viewer && sessionVersion === session) {
      clearWorkspace()
      setNotice('warn', '登录已过期，请重新登录')
    }
    const detailText = typeof body.detail === 'string' ? body.detail : ''
    const err = new Error(detailText || (response.status >= 500 ? '服务暂时不可用，请稍后重试' : '请求失败，请检查输入后重试'))
    err.kind = response.status >= 500 ? 'server' : 'business'
    err.status = response.status
    throw err
  }
  return body
}
async function run(key, fn, success = '', quiet = false) {
  if (isBusy(key)) return
  const session = sessionVersion, token = Symbol(key)
  actionVersion++
  if (!quiet) clearNotice()
  pending[key] = token
  try {
    await fn()
    if (sessionVersion === session && success) setNotice('success', success)
  } catch (e) {
    if (sessionVersion !== session) return
    if (e.status === 409 && user.value) {
      try { await refreshOpenDetail() } catch { /* Keep the original conflict message. */ }
    }
    if (sessionVersion === session && !quiet) setNotice(e.kind === 'business' ? 'warn' : 'error', e.message || '操作失败，请稍后重试')
  } finally {
    // A previous account's completion must not unlock the new account's request.
    if (pending[key] === token) delete pending[key]
  }
}
const post = (path, body = {}) => api(path, { method: 'POST', body: JSON.stringify(body) })

// DOM 更新后再滚动，避免旧页面高度导致 scrollTo 失效。
async function scrollTop() {
  await nextTick()
  window.scrollTo({ top: 0, left: 0, behavior: 'instant' })
}
async function refresh(background = false) {
  const viewer = user.value
  const version = actionVersion
  const [latestAssignments, latestTasks] = await Promise.all([
    api('/assignments'), teacher.value ? [] : api('/review-tasks')
  ])
  if (user.value !== viewer || (background && (page.value !== 'home' || actionVersion !== version))) return
  assignments.value = latestAssignments
  tasks.value = latestTasks
  clock.value = Date.now()
}
async function refreshOpenDetail() {
  const viewer = user.value
  const version = actionVersion
  const request = ++detailRefreshVersion
  const stillCurrent = () => user.value === viewer && actionVersion === version && detailRefreshVersion === request
  if (page.value === 'assignment' && active.value) {
    const id = active.value.id
    const latest = await api('/assignments/' + id)
    if (stillCurrent() && page.value === 'assignment' && active.value?.id === id) active.value = latest
  } else if (page.value === 'review' && detail.value) {
    const id = detail.value.id
    const latest = await api('/review-tasks/' + id)
    if (stillCurrent() && page.value === 'review' && detail.value?.id === id) detail.value = latest
  }
}
function changeAuthMode(value) {
  clearNotice()
  login.password = ''
  registration.password = registration.password_confirmation = ''
  registering.value = value
}
async function signUp() {
  await run('auth', async () => {
    const names = [registration.username.trim(), registration.display_name.trim()]
    if (names.some(name => !name || codePoints(name) > 100)) {
      const error = new Error('用户名和显示名称需要为 1–100 个字符'); error.kind = 'business'; throw error
    }
    if (codePoints(registration.password) < 12 || codePoints(registration.password) > 256) {
      const error = new Error('密码需要为 12–256 个字符'); error.kind = 'business'; throw error
    }
    if (registration.password !== registration.password_confirmation) {
      const error = new Error('两次输入的密码不一致'); error.kind = 'business'; throw error
    }
    await post('/auth/register', registration)
    login.username = names[0]
    changeAuthMode(false)
    registration.username = registration.display_name = ''
  }, '学生账号已注册，请登录')
}

/* ---------- 认证与导航 ---------- */
function routeHash(name, id = null) {
  if (name === 'home') return '#/'
  if (name === 'account') return '#/account'
  if (name === 'classrooms') return '#/classrooms'
  if (name === 'learning') return id ? '#/learning/' + encodeURIComponent(id) : '#/learning'
  if (name === 'create') return id ? '#/classrooms/' + encodeURIComponent(id) + '/create' : '#/create'
  const resource = encodeURIComponent(id)
  if (name === 'classroom') return '#/classrooms/' + resource
  if (name === 'review') return '#/review-tasks/' + resource
  return '#/assignments/' + resource + (name === 'assignment' ? '' : '/' + name)
}
function browserHash() { return window.location?.hash || '#/' }
function parseRoute(hash) {
  if (hash === '#/') return { name: 'home' }
  if (hash === '#/account') return { name: 'account' }
  if (hash === '#/create') return { name: 'create' }
  if (hash === '#/classrooms') return { name: 'classrooms' }
  if (hash === '#/learning') return { name: 'learning' }
  const learnerMatch = /^#\/learning\/([^/]+)$/.exec(hash)
  if (learnerMatch) {
    try {
      const id = decodeURIComponent(learnerMatch[1])
      return id && !/[\/\\]/.test(id) && id !== '.' && id !== '..' ? { name: 'learning', id } : null
    } catch { return null }
  }
  const classMatch = /^#\/classrooms\/([^/]+)(?:\/(create))?$/.exec(hash)
  if (classMatch) {
    try {
      const id = decodeURIComponent(classMatch[1])
      if (!id || /[\/\\]/.test(id) || id === '.' || id === '..') return null
      return { name: classMatch[2] ? 'create' : 'classroom', id }
    } catch { return null }
  }
  const match = /^#\/(assignments|review-tasks)\/([^/]+)(?:\/(dashboard|feedback))?$/.exec(hash)
  if (!match || (match[1] === 'review-tasks' && match[3])) return null
  try {
    const id = decodeURIComponent(match[2])
    if (!id || /[\/\\]/.test(id) || id === '.' || id === '..') return null
    return { name: match[1] === 'review-tasks' ? 'review' : match[3] || 'assignment', id }
  } catch { return null }
}
function setPage(name, id = null, replace = false) {
  if (name !== 'account') clearPasswordForm()
  page.value = name; routeId.value = id
  syncRoute(replace)
}
function syncRoute(replace) {
  const hash = routeHash(page.value, routeId.value)
  if (browserHash() !== hash) window.history[replace ? 'replaceState' : 'pushState'](null, '', hash)
}
async function navigate(target, { fromLocation = false, template = null, discardDraft = false } = {}) {
  if (!user.value || isBusy('nav')) return
  if (signingOut || (page.value === 'account' && isBusy('auth'))) { if (fromLocation) syncRoute(true); return }
  const viewer = user.value, session = sessionVersion, initialHash = browserHash()
  const stillCurrent = () => user.value === viewer && sessionVersion === session && !signingOut && browserHash() === initialHash
  const request = run('nav', async () => {
    if (!discardDraft && !await finishReviewBeforeLeave()) return
    if (!stillCurrent()) return
    if (!target || (['create', 'dashboard'].includes(target.name) && !teacher.value) ||
        (['review', 'feedback'].includes(target.name) && teacher.value) ||
        (target.name === 'learning' && !!target.id !== teacher.value)) {
      setPage('home', null, true)
      setNotice('warn', '页面不存在或当前账号无权访问')
      return
    }
    const { name, id } = target
    // Never use a URL segment directly as an API path.
    const path = id == null ? '' : encodeURIComponent(id)
    if (name === 'home') {
      await refresh()
      if (!stillCurrent()) return
    } else if (name === 'assignment') {
      const result = await api('/assignments/' + path)
      if (!stillCurrent()) return
      active.value = result
      text.value = active.value.my_submission?.content_type === 'TEXT' ? active.value.my_submission.content_text : ''
      pdf.value = null
      if (pdfInput.value) pdfInput.value.value = ''
      extending.value = false
    } else if (name === 'classrooms') {
      const result = await api('/classrooms')
      if (!stillCurrent()) return
      classroomList.value = result; classroomName.value = classInviteCode.value = ''
    } else if (name === 'classroom') {
      const result = await api('/classrooms/' + path)
      if (!stillCurrent()) return
      classroom.value = result; classImportText.value = ''
    } else if (name === 'create') {
      const classSource = id ? await api('/classrooms/' + path) : null
      const list = classSource ? classSource.members.filter(member => member.status === 'ACTIVE') : await api('/students')
      if (!stillCurrent()) return
      students.value = list
      creationClassroomName.value = classSource?.name ?? ''
      Object.assign(creation, template ? {
        title: Array.from(template.title).slice(0, 195).join('') + '（副本）', description: template.description,
        rubric: [...template.rubric], submit_deadline: '', review_deadline: '', student_ids: []
      } : { title: '', description: '', rubric: ['内容完整性', '逻辑与表达', '分析与创新'], submit_deadline: '', review_deadline: '', student_ids: [] })
      if (classSource && list.length <= 500) creation.student_ids = list.map(student => student.id)
    } else if (name === 'learning') {
      const result = await api(teacher.value ? '/students/' + path + '/learning-profile' : '/learning-profile')
      if (!stillCurrent()) return
      learning.value = result
    } else if (name === 'dashboard') {
      const result = await api('/assignments/' + path + '/dashboard')
      if (!stillCurrent()) return
      dashboard.value = result
      pendingCasesOnly.value = false; caseEditor.value = null
      highDisagreementOnly.value = false; teacherRevision.value = null
    } else if (name === 'feedback') {
      const result = await api('/assignments/' + path + '/my-feedback')
      if (!stillCurrent()) return
      feedback.value = result; focusedEvidence.value = null; caseEditor.value = null
      adoptionEditor.value = revisionDraft.value = null
    } else if (name === 'review') {
      const result = await api('/review-tasks/' + path)
      if (!stillCurrent()) return
      resetDraftTracking()
      detail.value = result
      const { evidence_anchors, ...fields } = editableReview(result.review || result.draft)
      Object.assign(review, fields)
      selected.value = evidence_anchors
      savedDraft.value = JSON.stringify(reviewBody())
      draftVersion.value = result.draft_version ?? null
      draftState.value = result.draft ? 'saved' : 'idle'
      quality.value = diagnosticFocus.value = null; selectionInfo.value = null
      const sel = window.getSelection(); if (sel) sel.removeAllRanges()
    }
    setPage(name, id ?? null, fromLocation)
    await scrollTop()
  })
  navigationRequest = request
  try { await request } finally {
    if (navigationRequest === request) navigationRequest = null
    // On a failed or cancelled back/deep-link navigation, stay on the rendered page.
    if (fromLocation && stillCurrent()) syncRoute(true)
  }
}
async function restoreLocation() {
  const requested = browserHash()
  if (navigationRequest) await navigationRequest
  if (!user.value || browserHash() !== requested || requested === routeHash(page.value, routeId.value)) return
  await navigate(parseRoute(requested), { fromLocation: true })
}
async function signIn() {
  await run('auth', async () => {
    user.value = await post('/auth/login', { username: login.username, password: login.password })
    login.password = ''
    await refresh()
    await restoreLocation()
    await scrollTop()
  })
}
async function signOut() {
  await run('auth', async () => {
    signingOut = true
    try {
      if (!await finishReviewBeforeLeave() || !user.value) return
      // 本地登出优先：会话过期或网络失败时也必须清理界面状态。
      const session = sessionVersion
      let uncertain = false
      try { await post('/auth/logout') } catch (e) { uncertain = e.status !== 401 }
      if (sessionVersion === session) clearWorkspace()
      if (uncertain) setNotice('warn', '服务端尚未确认退出，恢复网络后请重新退出登录；刷新页面可能仍显示登录状态。')
      await scrollTop()
    } finally { signingOut = false }
  })
}
async function submitPasswordChange() {
  if (passwordForm.new_password !== passwordForm.password_confirmation) {
    setNotice('warn', '两次输入的新密码不一致'); return
  }
  if (codePoints(passwordForm.new_password) < 12 || codePoints(passwordForm.new_password) > 256) {
    setNotice('warn', '新密码须包含 12–256 个字符'); return
  }
  if (passwordForm.current_password === passwordForm.new_password) {
    setNotice('warn', '新密码必须与当前密码不同'); return
  }
  await run('auth', async () => {
    const session = sessionVersion
    await post('/auth/change-password', { ...passwordForm })
    if (sessionVersion === session) {
      clearWorkspace()
      setNotice('success', '密码已修改，所有登录会话已失效，请使用新密码重新登录')
      await scrollTop()
    }
  })
}
const openAccount = () => navigate({ name: 'account' })
const goHome = () => navigate({ name: 'home' })
const openAssignment = id => navigate({ name: 'assignment', id })
const newAssignment = (template = null) => navigate({ name: 'create' }, { template })
const openClassrooms = () => navigate({ name: 'classrooms' })
const openLearning = (id = null) => navigate({ name: 'learning', id })
const openClassroom = id => navigate({ name: 'classroom', id })
const newClassAssignment = id => navigate({ name: 'create', id })

async function addClassroom() {
  await run('classroom', async () => {
    const viewer = user.value, hash = browserHash()
    const result = await post('/classrooms', { name: classroomName.value.trim() })
    if (user.value === viewer && page.value === 'classrooms' && browserHash() === hash) await openClassroom(result.id)
  }, '班级已创建')
}
async function joinClassroom() {
  await run('classroom', async () => {
    const viewer = user.value, hash = browserHash()
    const result = await post('/classrooms/join', { invite_code: classInviteCode.value.trim() })
    if (user.value === viewer && page.value === 'classrooms' && browserHash() === hash) {
      classInviteCode.value = ''; await openClassroom(result.id)
      if (user.value === viewer) setNotice('success', result.membership_status === 'ACTIVE' ? '你已加入班级' : '已提交加入请求，请等待教师批准')
    }
  })
}
async function classroomAction(path, payload, method = 'POST', success = '') {
  await run('classroom', async () => {
    const viewer = user.value, id = classroom.value.id
    const result = await api('/classrooms/' + encodeURIComponent(id) + path, { method, body: JSON.stringify(payload) })
    if (user.value === viewer && page.value === 'classroom' && classroom.value?.id === id) {
      classroom.value = result
      if (path === '/members') classImportText.value = ''
    }
  }, success)
}
function changeInvite(action) {
  if (!window.confirm(action === 'rotate' ? '生成新邀请码后，旧邀请码立即失效。确定继续？' : '撤销后学生不能使用旧邀请码加入。确定继续？')) return
  return classroomAction('/invite', { action }, 'POST', '邀请码已更新')
}
function importMembers() {
  const usernames = [...new Set(classImportText.value.split(/[\r\n,，;；]+/).map(name => name.trim()).filter(Boolean))]
  return classroomAction('/members', { usernames }, 'POST', '成员名单已导入')
}
function changeMember(member, action) {
  if (action === 'remove' && !window.confirm('移出班级不会改变已有任务及评价记录。确定移出？')) return
  return classroomAction('/members/' + encodeURIComponent(member.id), { action }, 'PATCH', action === 'approve' ? '加入申请已批准' : '成员已移出')
}

function openCaseEditor(feedbackId, action) {
  caseEditor.value = { feedbackId, action, text: '', confirm: false }
}
function openAdoption(item) {
  adoptionEditor.value = { feedbackId: item.feedback_id, decision: item.adoption?.decision || 'ADOPTED', reason: item.adoption?.reason || '' }
}
async function saveAdoption() {
  const editor = adoptionEditor.value
  if (!editor?.reason.trim()) return
  await run('adoption', async () => {
    const viewer = user.value, id = routeId.value
    const result = await api('/feedback/' + encodeURIComponent(editor.feedbackId) + '/adoption', { method: 'PUT', body: JSON.stringify({decision: editor.decision, reason: editor.reason.trim()}) })
    if (viewer !== user.value || page.value !== 'feedback' || routeId.value !== id) return
    const item = feedback.value.reviews.find(r => r.feedback_id === editor.feedbackId)
    if (item) item.adoption = result
    if (adoptionEditor.value === editor) adoptionEditor.value = null
  }, '采纳记录已保存')
}
function startRevision() {
  revisionDraft.value = { content_text: feedback.value.revisions?.at(-1)?.content_text ?? feedback.value.content_text, note: '' }
}
async function saveRevision() {
  const draft = revisionDraft.value
  if (!draft?.note.trim() || !draft.content_text.trim()) return
  await run('revision', async () => {
    const viewer = user.value, id = routeId.value
    const result = await post('/assignments/' + encodeURIComponent(id) + '/revisions', draft)
    if (viewer !== user.value || page.value !== 'feedback' || routeId.value !== id) return
    feedback.value.revisions = [...(feedback.value.revisions || []), result]
    if (revisionDraft.value === draft) revisionDraft.value = null
  }, '已保存独立修订版本')
}
async function showRevisionHistory(submissionId) {
  await run('revisionHistory', async () => {
    const viewer = user.value, id = routeId.value
    const result = await api('/assignments/' + encodeURIComponent(id) + '/revision-history/' + encodeURIComponent(submissionId))
    if (viewer === user.value && page.value === 'dashboard' && routeId.value === id) teacherRevision.value = { submissionId, ...result }
  })
}
async function submitCase() {
  const editor = caseEditor.value
  if (!editor || !editor.text.trim()) return
  await run('case', async () => {
    const viewer = user.value, id = routeId.value, target = page.value
    const payload = editor.action === 'resolve' ? { resolution: editor.text.trim(), confirm_anonymous: editor.confirm } : { reason: editor.text.trim() }
    const result = await post('/feedback/' + encodeURIComponent(editor.feedbackId) + '/' + editor.action, payload)
    if (user.value !== viewer || routeId.value !== id || page.value !== target) return
    if (target === 'feedback') {
      const review = feedback.value.reviews.find(item => item.feedback_id === editor.feedbackId)
      if (review) review.review_case = result
    } else {
      for (const record of dashboard.value.records) {
        const review = record.reviews.find(item => item.feedback_id === editor.feedbackId)
        if (review) review.review_case = result
      }
      const cases = dashboard.value.records.flatMap(record => record.reviews).map(item => item.review_case).filter(Boolean)
      dashboard.value.review_case_pending = cases.filter(item => item.status === 'PENDING').length
      dashboard.value.review_case_resolved = cases.filter(item => item.status === 'RESOLVED').length
    }
    if (caseEditor.value === editor) caseEditor.value = null
  }, '复核记录已保存')
}

/* ---------- 创建任务：截止时间前端校验 ---------- */
function deadlineIssues() {
  const issues = []
  const s = creation.submit_deadline, r = creation.review_deadline
  if (!s || !r) return issues
  const sd = new Date(s), rd = new Date(r)
  if (Number.isNaN(sd.getTime()) || Number.isNaN(rd.getTime())) return ['截止时间格式无效，请重新选择']
  if (sd.getTime() <= Date.now()) issues.push('提交截止时间必须晚于当前时间')
  if (rd.getTime() <= sd.getTime()) issues.push('互评截止时间必须晚于提交截止时间')
  return issues
}
const deadlineErrors = computed(deadlineIssues)

async function createAssignment() {
  await run('create', async () => {
    const viewer = user.value
    const issues = deadlineIssues()
    if (issues.length) { const err = new Error(issues.join('；')); err.kind = 'business'; throw err }
    if (creation.student_ids.length < 3) { const err = new Error('至少选择 3 名参与学生'); err.kind = 'business'; throw err }
    const submitDate = new Date(creation.submit_deadline), reviewDate = new Date(creation.review_deadline)
    if (Number.isNaN(submitDate.getTime()) || Number.isNaN(reviewDate.getTime())) {
      const err = new Error('截止时间格式无效，请重新选择'); err.kind = 'business'; throw err
    }
    // datetime-local 按浏览器本地时区解析，toISOString 转成带时区的 UTC，后端据此校验。
    await post('/assignments', {
      title: creation.title, description: creation.description, rubric: creation.rubric,
      submit_deadline: submitDate.toISOString(), review_deadline: reviewDate.toISOString(),
      student_ids: creation.student_ids,
      ...(routeId.value ? { classroom_id: routeId.value } : {})
    })
    if (user.value !== viewer) return
    Object.assign(creation, { title: '', description: '', rubric: ['内容完整性', '逻辑与表达', '分析与创新'], submit_deadline: '', review_deadline: '', student_ids: [] })
    await refresh(); if (user.value !== viewer || page.value !== 'create') return
    setPage('home'); await scrollTop()
  }, '任务已创建')
}

function localDateTime(value) {
  const time = new Date(value)
  return new Date(time.getTime() - time.getTimezoneOffset() * 60000).toISOString().slice(0, 16)
}
function openExtension() {
  extension.submit_deadline = ''
  extension.review_deadline = localDateTime(active.value.review_deadline)
  extending.value = true
}
async function extendDeadlines() {
  await run('extend', async () => {
    const item = active.value
    const viewer = user.value
    const originalReview = extension.review_deadline === localDateTime(item.review_deadline)
    const latest = await api('/assignments/' + item.id + '/deadlines', {
      method: 'PATCH',
      body: JSON.stringify({
        submit_deadline: new Date(extension.submit_deadline).toISOString(),
        review_deadline: originalReview ? item.review_deadline : new Date(extension.review_deadline).toISOString()
      })
    })
    if (user.value !== viewer) return
    if (page.value === 'assignment' && active.value?.id === item.id) {
      active.value = latest
      extending.value = false
    }
    await refresh()
  }, '截止时间已更新，学生可以继续提交作业')
}

/* ---------- 作业提交 ---------- */
async function submitText() {
  await run('submitText', async () => {
    const draft = text.value
    const id = active.value.id
    const viewer = user.value
    if (!draft.trim()) { const err = new Error('作业正文不能为空'); err.kind = 'business'; throw err }
    if (codePoints(draft.trim()) > 10000) { const err = new Error('作业正文最多 10000 字符'); err.kind = 'business'; throw err }
    await post('/assignments/' + id + '/submission', { content_text: draft })
    if (user.value !== viewer) return
    const latest = await api('/assignments/' + id)
    if (user.value === viewer && page.value === 'assignment' && active.value?.id === id) {
      active.value = latest
      if (text.value === draft) text.value = latest.my_submission?.content_text ?? draft
    }
  }, '作业已保存')
}
function onPdfChange(e) {
  const file = e.target.files?.[0] || null
  if (file && file.size > 5 * 1024 * 1024) {
    setNotice('warn', 'PDF 文件不能超过 5 MB，请重新选择')
    e.target.value = ''; pdf.value = null; return
  }
  pdf.value = file
}
async function submitPDF() {
  if (pdf.value && active.value.my_submission && !window.confirm('上传成功后会替换当前作业。确定提交此 PDF？')) return
  await run('upload', async () => {
    const file = pdf.value
    const id = active.value.id
    const viewer = user.value
    const previousText = active.value.my_submission?.content_type === 'TEXT' ? active.value.my_submission.content_text : ''
    if (!file) { const err = new Error('请先选择 PDF 文件'); err.kind = 'business'; throw err }
    if (file.size > 5 * 1024 * 1024) { const err = new Error('PDF 文件不能超过 5 MB'); err.kind = 'business'; throw err }
    const form = new FormData()
    form.append('file', file)
    await api('/assignments/' + id + '/submission/pdf', { method: 'POST', body: form })
    if (user.value !== viewer) return
    const latest = await api('/assignments/' + id)
    if (user.value === viewer && page.value === 'assignment' && active.value?.id === id) {
      active.value = latest
      if (text.value === previousText) text.value = ''
      if (pdf.value === file) {
        pdf.value = null
        if (pdfInput.value) pdfInput.value.value = ''
      }
    }
  }, 'PDF 文字已提取并保存')
}
async function startReview() {
  await run('start', async () => {
    const viewer = user.value, id = active.value.id
    const res = await post('/assignments/' + id + '/start-review')
    if (user.value !== viewer) return
    const latest = await api('/assignments/' + id)
    if (user.value !== viewer) return
    if (page.value === 'assignment' && active.value?.id === id) active.value = latest
    await refresh()
    if (user.value === viewer) setNotice('success', res.created ? `已生成 ${res.count} 条匿名互评任务` : '互评已开始')
  })
}
async function closeAssignment() {
  const skipped = active.value.phase === 'REVIEW_NOT_STARTED'
  if (skipped && !window.confirm('本次未生成互评任务。结束后，学生将看到本次未开展互评。确定结束？')) return
  await run('close', async () => {
    const viewer = user.value, id = active.value.id
    await post('/assignments/' + id + '/close')
    if (user.value !== viewer) return
    const latest = await api('/assignments/' + id)
    if (user.value !== viewer) return
    if (page.value === 'assignment' && active.value?.id === id) active.value = latest
    await refresh()
  }, skipped ? '任务已结束，本次未开展互评' : '任务已结束，学生可以查看反馈')
}
const openDashboard = id => navigate({ name: 'dashboard', id })
const openFeedback = id => navigate({ name: 'feedback', id })

async function goToEvidence(anchor) {
  focusedEvidence.value = anchor
  await nextTick()
  const target = document.getElementById(evidenceTargetId(anchor.start))
  if (target) {
    target.focus({ preventScroll: true })
    target.scrollIntoView({ block: 'center' })
  }
}

/* ---------- 打开互评：只回填可编辑字段，避免冗余字段灌入响应式对象 ---------- */
function editableReview(saved) {
  const scores = Array.isArray(saved?.scores) && saved.scores.length === 3 ? saved.scores.map(Number) : [3, 3, 3]
  return {
    scores,
    strengths: saved?.strengths ?? '',
    weaknesses: saved?.weaknesses ?? '',
    suggestions: saved?.suggestions ?? '',
    evidence_anchors: (saved?.evidence_anchors || []).map(({ start, end, quote, content_hash }) => ({
      start, end, quote, content_hash
    }))
  }
}

const openReview = id => navigate({ name: 'review', id })

/* ---------- 证据锚定 ----------
   后端 validate_anchors 用 Python text[start:end]（Unicode 码点）校验 quote 与偏移，
   因此 quote 必须从原文按码点切片取得，不能依赖 Range.toString() 的 DOM 拼接结果。 */
function readSelection() {
  const root = source.value, content = detail.value?.content_text
  const sel = window.getSelection()
  if (readOnly.value || !root || !content || !sel || sel.rangeCount === 0 || sel.isCollapsed) { selectionInfo.value = null; return }
  const range = sel.getRangeAt(0)
  if (!root.contains(range.startContainer) || !root.contains(range.endContainer)) { selectionInfo.value = null; return }
  const prefix = range.cloneRange()
  prefix.selectNodeContents(root)
  prefix.setEnd(range.startContainer, range.startOffset)
  const prefixText = prefix.toString(), selectedText = range.toString()
  const chars = Array.from(content)
  const start = Array.from(prefixText).length, end = start + Array.from(selectedText).length
  // DOM 文本与原文若存在任何差异（换行规范化、跨节点拼接等），拒绝生成不可靠锚点。
  if (end > chars.length || chars.slice(0, start).join('') !== prefixText || chars.slice(start, end).join('') !== selectedText) {
    selectionInfo.value = null; return
  }
  selectionInfo.value = start < end ? { start, end, length: end - start } : null
}
// selectionchange 只在存在有效选区时刷新缓存；点击按钮导致选区消失时保留快照，避免锚点丢失。
function onSelectionChange() {
  if (page.value !== 'review' || readOnly.value) return
  const sel = window.getSelection()
  if (sel && sel.rangeCount && !sel.isCollapsed) readSelection()
}
function addEvidence() {
  if (isBusy('nav') || isBusy('auth')) return
  clearNotice()
  if (readOnly.value) { setNotice('warn', '当前互评不可编辑，不能修改证据'); return }
  const info = selectionInfo.value
  if (!info) { setNotice('warn', '请先在左侧作业正文中选中一段连续文字'); return }
  if (selected.value.length >= 3) { setNotice('warn', '最多添加三处证据，请先删除一条再添加'); return }
  const chars = Array.from(detail.value.content_text)
  if (info.start < 0 || info.start >= info.end || info.end > chars.length) {
    setNotice('warn', '所选内容已失效，请重新选取'); selectionInfo.value = null; return
  }
  const quote = chars.slice(info.start, info.end).join('')
  if (!quote.trim()) { setNotice('warn', '请选择包含文字的片段'); return }
  // 与后端 Anchor.quote 上限一致（按码点计数，匹配 Python len），避免超长选区在保存/提交/检测时反复 422。
  if (Array.from(quote).length > 10000) { setNotice('warn', '证据引用过长（最多 10000 字符），请缩短选区'); return }
  if (selected.value.some(a => a.start === info.start && a.end === info.end)) { setNotice('warn', '该片段已经添加'); return }
  selected.value.push({ start: info.start, end: info.end, quote, content_hash: detail.value.content_hash })
  selectionInfo.value = null
  const sel = window.getSelection(); if (sel) sel.removeAllRanges()
  setNotice('success', `已添加第 ${selected.value.length} 处证据（${Array.from(quote).length} 字）`)
}
function removeEvidence(i) {
  if (readOnly.value || isBusy('nav') || isBusy('auth')) return
  selected.value.splice(i, 1)
  selectionInfo.value = null
}

/* ---------- 评价草稿 / 检测 / 提交 ---------- */
function reviewBody() {
  return {
    scores: review.scores.map(Number), strengths: review.strengths, weaknesses: review.weaknesses,
    suggestions: review.suggestions, evidence_anchors: selected.value
  }
}
const hasUnsavedDraft = computed(() => page.value === 'review' && JSON.stringify(reviewBody()) !== savedDraft.value)
const draftStatus = computed(() => ({ idle: '尚未编辑', waiting: '有修改，稍后自动保存', saving: '正在保存草稿…',
  saved: '草稿已保存，可稍后继续', failed: '草稿保存失败，本页内容仍保留，请重试',
  conflict: '其他页面已更新草稿，本页内容尚未保存，请处理冲突' })[draftState.value])
function cancelDraftTimer() {
  if (draftTimer !== null) clearTimeout(draftTimer)
  draftTimer = null
}
function resetDraftTracking() {
  cancelDraftTimer(); savedDraft.value = ''; draftVersion.value = null; draftState.value = 'idle'
  draftDeferred = false; draftDeferrals = 0
}
async function finishReviewBeforeLeave() {
  if (page.value !== 'review') return true
  if (leaveReviewRequest) return leaveReviewRequest
  const request = flushReviewBeforeLeave()
  leaveReviewRequest = request
  try { return await request } finally {
    if (leaveReviewRequest === request) leaveReviewRequest = null
  }
}
async function flushReviewBeforeLeave() {
  if (isBusy('submitReview')) { setNotice('info', '请等待评价提交完成'); return false }
  // Wait for the in-flight snapshot to settle, then persist the latest edits before leaving.
  const viewer = user.value, id = detail.value?.id
  cancelDraftTimer(); draftDeferred = false
  if (draftRequest) await draftRequest
  if (user.value !== viewer || detail.value?.id !== id) return false
  // A timed-out write may have reached the server. Keep the local text and let
  // the student explicitly retry or confirm leaving, instead of blindly retrying.
  if (hasUnsavedDraft.value && !readOnly.value && !['conflict', 'failed'].includes(draftState.value)) await saveDraft(true)
  if (user.value !== viewer || detail.value?.id !== id) return false
  return !hasUnsavedDraft.value || window.confirm('当前评价有尚未保存的修改，离开后可能丢失。确定离开？')
}
function warnBeforeUnload(event) {
  if (hasUnsavedDraft.value || (page.value === 'review' && (isBusy('draft') || isBusy('submitReview')))) {
    event.preventDefault(); event.returnValue = ''
  }
}
function scheduleDraft(newEdit = false) {
  cancelDraftTimer()
  if (newEdit) { draftDeferred = false; draftDeferrals = 0 }
  if (readOnly.value || draftState.value === 'conflict') { draftDeferred = false; return }
  if (!hasUnsavedDraft.value) {
    draftDeferred = false; draftDeferrals = 0
    if (page.value === 'review' && !readOnly.value && !isBusy('draft')) {
      draftState.value = (draftVersion.value === 'none' || (!detail.value?.draft && draftVersion.value === null)) ? 'idle' : 'saved'
    }
    return
  }
  // An in-flight save re-arms the latest snapshot on success; on failure it
  // leaves the editable snapshot and an explicit manual retry. Do not requeue.
  if (isBusy('draft') || (draftState.value === 'failed' && !newEdit)) return
  if (draftTransitionBusy.value) {
    if (draftDeferrals >= MAX_DRAFT_DEFERRALS) {
      draftDeferred = false; draftState.value = 'failed'
      return
    }
    draftDeferrals++; draftDeferred = true; draftState.value = 'waiting'
    return
  }
  draftDeferred = false; draftState.value = 'waiting'
  draftTimer = setTimeout(() => {
    draftTimer = null
    if (readOnly.value || !hasUnsavedDraft.value || draftState.value === 'conflict') return
    if (isBusy('draft') || draftTransitionBusy.value) { scheduleDraft(); return }
    void saveDraft(true)
  }, 1500)
}
async function saveDraft(auto = false, overwrite = false) {
  auto = auto === true
  if (readOnly.value) { setNotice('warn', '当前互评阶段已结束，不能保存草稿'); return }
  if (isBusy('draft') || isBusy('submitReview') || (draftState.value === 'conflict' && !overwrite)) return
  cancelDraftTimer(); draftDeferred = false
  if (!auto) draftDeferrals = 0
  const viewer = user.value, id = detail.value.id, session = sessionVersion
  const current = () => user.value === viewer && sessionVersion === session && page.value === 'review' && detail.value?.id === id
  let saved = false
  draftState.value = 'saving'
  const request = run('draft', async () => {
    try {
      if (overwrite) {
        const latest = await api('/review-tasks/' + id)
        if (!current()) return
        if (!latest.can_write) { detail.value = latest; throw Object.assign(new Error('互评已提交或已结束，不能保存草稿'), { status: 409, kind: 'closed' }) }
        draftVersion.value = latest.draft_version ?? null
      }
      const snapshot = JSON.stringify(reviewBody())
      const result = await api('/review-tasks/' + id + '/draft', { method: 'POST', body: snapshot,
        headers: {'X-Draft-Version': draftVersion.value ?? 'none'} })
      if (!current()) return
      savedDraft.value = snapshot
      draftVersion.value = result.draft_version ?? draftVersion.value
      draftState.value = 'saved'; saved = true; draftDeferrals = 0
    } catch (error) {
      // A closed task is not a version conflict: the conflict actions cannot succeed there.
      if (current()) draftState.value = error.status === 409 && error.kind !== 'closed' ? 'conflict' : 'failed'
      throw error
    }
  }, auto ? '' : '草稿已保存，可以稍后继续', auto)
  draftRequest = request
  try { await request } finally { if (draftRequest === request) draftRequest = null }
  if (current() && saved && hasUnsavedDraft.value) scheduleDraft()
}
async function overwriteDraft() {
  if (window.confirm('确认使用当前页面内容覆盖服务器上的其他版本？')) await saveDraft(false, true)
}
async function reloadDraft() {
  if (!window.confirm('加载服务器草稿将放弃本页未保存的修改，确定继续？')) return
  await navigate({ name: 'review', id: detail.value.id }, { discardDraft: true })
}
async function checkAI() {
  if (readOnly.value) { setNotice('warn', '当前互评阶段已结束，不能再次检测'); return }
  await run('ai', async () => {
    const id = detail.value.id
    const viewer = user.value
    const stillCurrent = () => user.value === viewer && page.value === 'review' && detail.value?.id === id
    const version = editVersion, snapshot = JSON.stringify(reviewBody())
    let result
    try {
      result = await post('/review-tasks/' + id + '/ai-check', reviewBody())
    } finally {
      // A timeout may consume the final attempt. Refresh the quota without replacing the form.
      if (stillCurrent()) {
        try { await refreshOpenDetail() } catch { /* The detection response still applies. */ }
      }
    }
    if (!stillCurrent() || readOnly.value) return
    // 检测期间若草稿或证据被改动，结果已不对应当前内容，必须丢弃。
    if (version !== editVersion || JSON.stringify(reviewBody()) !== snapshot) {
      setNotice('info', '评价内容在检测期间被修改，本次结果已丢弃，请重新检测'); return
    }
    quality.value = result
  })
}
async function submitReview() {
  if (readOnly.value) { setNotice('warn', '当前互评阶段已结束，不能提交评价'); return }
  if (isBusy('draft')) return
  if (draftState.value === 'conflict' && !window.confirm('其他页面的草稿已更新。确定仍以本页评价正式提交？')) return
  cancelDraftTimer()
  const viewer = user.value, id = detail.value.id
  await run('submitReview', async () => {
    try {
      await api('/review-tasks/' + id + '/submit', { method: 'POST', body: JSON.stringify(reviewBody()),
        headers: {'X-Draft-Version': draftVersion.value ?? 'none'} })
    } catch (error) {
      // 正式提交与草稿保存共用乐观并发校验：另一页面可能已保存更新的草稿。
      // 刷新到最新版本并进入冲突态，由学生通过既有冲突 UI 确认是否仍以本页内容提交。
      if (error.status === 409 && error.message.startsWith('草稿版本冲突') && user.value === viewer && detail.value?.id === id) {
        try {
          const latest = await api('/review-tasks/' + id)
          if (user.value === viewer && detail.value?.id === id) {
            draftVersion.value = latest.draft_version ?? null
            if (latest.draft) detail.value = { ...detail.value, draft: latest.draft }
            draftState.value = latest.can_write ? 'conflict' : 'idle'
          }
        } catch { /* 刷新失败时保留原始冲突提示，学生可手动重试 */ }
      }
      throw error
    }
    if (user.value !== viewer) return
    await refresh()
    if (user.value !== viewer || page.value !== 'review' || detail.value?.id !== id) return
    resetDraftTracking(); setPage('home'); await scrollTop()
  }, '评价已提交，感谢你的认真反馈')
  if (user.value === viewer && page.value === 'review' && detail.value?.id === id) scheduleDraft()
}

/* ---------- 渲染辅助 ---------- */
const percent = (n, d) => d ? Math.round(n / d * 100) + '%' : '—'
const date = (d) => d ? new Date(d).toLocaleString('zh-CN', { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—'
const joinList = (v) => Array.isArray(v) ? (v.join('；') || '无') : '—'
function aiQualityTotal(check) {
  // The server is the single authority on validity and normalizes historical results.
  if (check.status !== 'SUCCESS' || !check.result) return null
  return check.result.specificity + check.result.evidence_grounding + check.result.actionability
}
function aiCheckStatus(check) {
  if (check.status !== 'SUCCESS') return aiStatusText[check.status] ?? '—'
  if (aiQualityTotal(check) === null) return '结果不可用'
  return check.result.quality_level
}
const issueLabel = type => ({ VAGUE_FEEDBACK: '评价空泛', INSUFFICIENT_EVIDENCE: '证据不足',
  LACK_OF_ACTIONABILITY: '建议缺乏可操作性', OTHER: '其他问题' })[type] ?? type
const aiCheckIssues = (result) => Array.isArray(result?.issues)
  ? result.issues.filter(issue => typeof issue?.type === 'string' && typeof issue?.message === 'string') : []
const aiCheckSuggestions = (result) => joinList(Array.isArray(result?.suggestions)
  ? result.suggestions.filter(value => typeof value === 'string') : null)
const evidenceTargetId = (start) => 'feedback-source-' + start
function verifiedLocation(location, content) {
  const chars = Array.from(content || '')
  return location && Number.isInteger(location.start) && Number.isInteger(location.end) &&
    location.start >= 0 && location.end > location.start && location.end <= chars.length &&
    chars.slice(location.start, location.end).join('') === location.quote
}
function goToDiagnosticReview(item) {
  const content = shownReview.value[item.field], location = item.review_location
  if (!commentLabels[item.field] || !verifiedLocation(location, content)) return
  const field = document.getElementById('review-field-' + item.field)
  if (!field) return
  const chars = Array.from(content)
  field.focus(); field.setSelectionRange(chars.slice(0, location.start).join('').length, chars.slice(0, location.end).join('').length)
  field.scrollIntoView?.({block: 'center'})
}
async function goToDiagnosticSource(item) {
  const location = item.source_location
  if (!verifiedLocation(location, detail.value?.content_text) || location.content_hash !== detail.value?.content_hash) return
  diagnosticFocus.value = location
  await nextTick()
  const mark = document.getElementById('review-source-' + location.start)
  mark?.focus(); mark?.scrollIntoView?.({block: 'center'})
}
function highlighted(content, anchors) {
  const chars = Array.from(content ?? ''), total = chars.length
  const valid = (anchors || []).filter(a => a && Number.isInteger(a.start) && Number.isInteger(a.end) && a.start >= 0 && a.end <= total && a.start < a.end)
  const points = new Set([0, total])
  for (const a of valid) { points.add(a.start); points.add(a.end) }
  const breaks = [...points].sort((x, y) => x - y)
  return breaks.slice(0, -1).map((start, i) => {
    const end = breaks[i + 1]
    return { start, end, text: chars.slice(start, end).join(''), marked: valid.some(a => a.start <= start && a.end >= end) }
  })
}

let statusTimer = null
let refreshingStatus = false
async function updateStatus() {
  if (!ready.value || !user.value || navBusy.value || document.hidden || refreshingStatus) return
  // Skip only writes that can change the state being polled.
  if (page.value === 'review' && ['draft', 'submitReview'].some(isBusy)) return
  if (['home', 'assignment'].includes(page.value) &&
      ['create', 'extend', 'submitText', 'upload', 'start', 'close', 'submitReview'].some(isBusy)) return
  refreshingStatus = true
  try {
    if (page.value === 'home') await refresh(true)
    else if ((page.value === 'assignment' && active.value?.phase !== 'CLOSED')
        || (page.value === 'review' && detail.value?.can_write)) await refreshOpenDetail()
  } catch { /* The current state remains visible while connectivity recovers. */ }
  finally { refreshingStatus = false }
}
onMounted(async () => {
  window.addEventListener?.('hashchange', restoreLocation)
  window.addEventListener?.('beforeunload', warnBeforeUnload)
  document.addEventListener('selectionchange', onSelectionChange)
  document.addEventListener('visibilitychange', updateStatus)
  statusTimer = setInterval(updateStatus, 15000)
  try {
    user.value = await api('/auth/me')
    await refresh()
    await restoreLocation()
  } catch (e) {
    user.value = null
    // 401 是「尚未登录」的正常路径；其余错误必须可见，不能静默停在登录页。
    if (e.status !== 401) setNotice('error', ['network', 'timeout'].includes(e.kind)
      ? '无法连接到服务器或请求超时，请检查网络后刷新页面重试' : '服务暂时不可用，请稍后刷新页面重试')
  } finally {
    ready.value = true
  }
})
onBeforeUnmount(() => {
  cancelDraftTimer(); draftDeferred = false
  for (const [controller, deadline] of inFlightRequests) { clearTimeout(deadline); controller.abort() }
  inFlightRequests.clear()
  window.removeEventListener?.('hashchange', restoreLocation)
  window.removeEventListener?.('beforeunload', warnBeforeUnload)
  document.removeEventListener('selectionchange', onSelectionChange)
  document.removeEventListener('visibilitychange', updateStatus)
  if (statusTimer) clearInterval(statusTimer)
})
</script>

<template>
  <div v-if="!ready" class="boot" role="status" aria-live="polite"><div class="boot-brand"><span class="brand-icon">P<span>✳</span></span><strong>PeerGrade</strong></div><p>正在加载工作空间…</p></div>
  <div v-else-if="!user" class="login-shell">
    <div class="login-story"><div class="brand light"><span class="brand-icon">P<span>✳</span></span><strong>PeerGrade</strong></div><div class="story-copy"><span class="eyebrow light-label">为更好的学习反馈而设计</span><h1>好的反馈，<br><em>从一处证据开始。</em></h1><p>让每一次互评更具体、有依据，也更能帮助同伴进步。</p><div class="story-note"><span>“</span> 从原文出发，给出真正有用的建议。<small>READ · REFLECT · IMPROVE</small></div></div><div class="story-footer">PEERGRADE &nbsp; / &nbsp; 学习共同体</div></div>
    <div class="login-form-wrap">
      <form class="login-form" @submit.prevent="registering ? signUp() : signIn()">
        <div class="mobile-brand brand"><span class="brand-icon">P<span>✳</span></span><strong>PeerGrade</strong></div>
        <template v-if="registering">
          <h2>注册学生账号</h2>
          <p class="subtle">注册后，课程教师可以将你加入任务。</p>
          <label>用户名<input v-model="registration.username" autocomplete="username" required></label>
          <label>显示名称<input v-model="registration.display_name" autocomplete="nickname" required></label>
          <label>密码<input v-model="registration.password" type="password" autocomplete="new-password" aria-describedby="registration-password-hint" required></label>
          <p id="registration-password-hint" class="hint">使用 12–256 个字符。</p>
          <label>确认密码<input v-model="registration.password_confirmation" type="password" autocomplete="new-password" required></label>
        </template>
        <template v-else>
          <span class="eyebrow">欢迎回来</span>
          <h2>登录 PeerGrade</h2>
          <p class="subtle">登录你的账号，继续课程互评。</p>
          <label>用户名<input v-model="login.username" autocomplete="username" required placeholder="输入用户名"></label>
          <label>密码<input v-model="login.password" type="password" autocomplete="current-password" required placeholder="输入密码"></label>
        </template>
        <p v-if="notice.text" :class="notice.type === 'success' ? 'toast toast-success' : 'alert'" :role="notice.type === 'success' ? 'status' : 'alert'">{{ notice.text }}</p>
        <button class="button primary wide" :disabled="isBusy('auth')" :aria-busy="isBusy('auth')">{{ registering ? (isBusy('auth') ? '正在注册…' : '注册') : (isBusy('auth') ? '正在登录…' : '登录 →') }}</button>
        <p class="login-hint"><button type="button" class="link-button" :disabled="isBusy('auth')" @click="changeAuthMode(!registering)">{{ registering ? '返回登录' : '注册学生账号' }}</button></p>
      </form>
    </div>
  </div>
  <div v-else class="app-shell">
    <aside class="sidebar"><button class="brand brand-button" @click="goHome" aria-label="返回我的任务"><span class="brand-icon">P<span>✳</span></span><strong>PeerGrade</strong></button><div class="side-label">工作空间</div><nav class="side-nav" aria-label="主导航"><button class="nav-item" :class="{selected:page==='home'}" :aria-current="page==='home' ? 'page' : undefined" :disabled="navBusy" @click="goHome"><span aria-hidden="true">▦</span> 我的任务</button><button class="nav-item" :class="{selected:['classrooms','classroom'].includes(page)}" :aria-current="['classrooms','classroom'].includes(page) ? 'page' : undefined" :disabled="navBusy" @click="openClassrooms"><span aria-hidden="true">▣</span> 我的班级</button><button v-if="!teacher" class="nav-item" :class="{selected:page==='learning'}" :aria-current="page==='learning' ? 'page' : undefined" :disabled="navBusy" @click="openLearning()"><span aria-hidden="true">◉</span> 学习档案</button><button class="nav-item" :class="{selected:page==='account'}" :aria-current="page==='account' ? 'page' : undefined" :disabled="navBusy" @click="openAccount"><span aria-hidden="true">◇</span> 账号安全</button></nav><div class="side-bottom"><div class="side-quote">“好的评价，<br>让想法继续生长。”</div><div class="profile"><span class="avatar">{{ user.display_name.slice(0,1) }}</span><span><strong>{{ user.display_name }}</strong><small>{{ teacher ? '教师账号' : '学生账号' }}</small></span><button :disabled="isBusy('auth')" @click="signOut" title="退出登录" aria-label="退出登录">↗</button></div></div></aside>
    <main class="main"><header class="topbar"><span>{{ teacher ? '教师工作空间' : '学生工作空间' }} <span class="divider">/</span> {{ pageLabel[page] || '任务详情' }}</span><button class="top-user" :disabled="isBusy('auth')" @click="signOut" title="退出登录" aria-label="退出登录">{{ user.display_name.slice(0,1) }}</button></header>
      <div class="content"><div v-if="notice.text" class="toast" :class="'toast-'+notice.type" :role="notice.type==='error' ? 'alert' : 'status'" aria-live="polite"><span>{{ notice.text }}</span><button @click="clearNotice()" aria-label="关闭提示">×</button></div>
        <template v-if="page==='home'"><div class="page-heading"><div><span class="eyebrow">YOUR WORKSPACE</span><h1>{{ teacher ? '教学工作台' : '我的学习任务' }}<span class="heading-dot">.</span></h1><p>你好，{{ user.display_name }}。{{ teacher ? '在这里组织互评，让反馈更有价值。' : '认真阅读，留下有依据的反馈。' }}</p></div><button v-if="teacher" class="button primary" :disabled="navBusy" @click="newAssignment()">＋ 创建任务</button></div>
          <div class="filter-bar task-search"><label>搜索任务<input v-model="search" aria-label="搜索任务" placeholder="输入任务名称、说明或匿名编号"></label><button v-if="search" type="button" class="link-button" @click="search = ''">清空搜索</button></div>
          <div v-if="!teacher && (urgentAssignments || urgentReviews)" class="deadline-notice" role="status">⏳ 24 小时内截止：{{ urgentAssignments }} 项待提交作业，{{ urgentReviews }} 项待完成互评。</div>
          <div class="overview"><div><span>课程任务</span><strong>{{ assignments.length }}</strong><small>已加入的任务</small></div><div><span>{{ teacher ? '进行中' : '待完成互评' }}</span><strong>{{ teacher ? assignments.filter(a=>a.phase!=='CLOSED').length : tasks.filter(t=>t.can_write).length }}</strong><small>{{ teacher ? '提交或互评阶段' : '需要你关注的评价' }}</small></div><div class="overview-message"><span class="spark">✳</span><div><strong>让每一条反馈都有来处</strong><small>选取原文，说明问题，再给出可执行的建议。</small></div></div></div>
          <section v-if="!teacher && tasks.length" class="section"><div class="section-title"><div><span class="eyebrow">REVIEW QUEUE</span><h2>我的互评 <span>{{ visibleTasks.length }} / {{ tasks.length }}</span></h2></div><p>阅读匿名作业，给同伴一些启发。</p></div><div class="filter-bar"><label>互评状态<select v-model="reviewFilter" aria-label="互评状态筛选"><option value="all">全部</option><option value="pending">待评价</option><option value="COMPLETED">已评价</option><option value="REVIEW_EXPIRED">已截止</option></select></label></div><div v-if="!visibleTasks.length" class="empty">没有匹配的互评任务</div><div v-else class="task-list"><button v-for="t in visibleTasks" :key="t.id" class="task-row" :disabled="navBusy" @click="openReview(t.id)"><div class="task-symbol" aria-hidden="true">✎</div><div class="task-main"><strong>{{ t.assignment_title }}</strong><small>匿名作业 · {{ t.anonymous_code }}</small></div><span v-if="t.can_write && dueSoon(t.review_deadline)" class="pill deadline-pill">24 小时内截止</span><span class="pill" :class="statusClass(t.phase)">{{ statusText[t.phase] }}</span><span class="arrow" aria-hidden="true">↗</span></button></div></section>
          <section class="section"><div class="section-title"><div><span class="eyebrow">COURSE ASSIGNMENTS</span><h2>课程任务 <span>{{ visibleAssignments.length }} / {{ assignments.length }}</span></h2></div><p>{{ teacher ? '创建和管理你的课程互评。' : '查看要求、提交作业和收到的反馈。' }}</p></div><div class="filter-bar"><label>任务状态<select v-model="assignmentFilter" aria-label="课程任务状态筛选"><option value="all">全部</option><option value="active">未结束</option><option value="closed">已结束</option></select></label></div><div v-if="!assignments.length" class="empty"><div aria-hidden="true">◌</div><strong>这里还没有任务</strong><p>{{ teacher ? '创建一个任务，开启第一次互评。' : '课程教师添加任务后会显示在这里。' }}</p></div><div v-else-if="!visibleAssignments.length" class="empty">没有匹配的课程任务</div><div v-else class="cards"><button v-for="a in visibleAssignments" :key="a.id" class="assignment-card" :disabled="navBusy" @click="openAssignment(a.id)"><div class="card-top"><span class="card-icon" aria-hidden="true">✳</span><span class="pill" :class="statusClass(a.phase)">{{ assignmentStatus(a) }}</span></div><h3>{{ a.title }}</h3><p>{{ a.description }}</p><div class="card-bottom"><span>提交截止 {{ date(a.submit_deadline) }}</span><span v-if="submissionReminder(a)" class="pill deadline-pill">24 小时内截止</span><span aria-hidden="true">↗</span></div></button></div></section>
        </template>
        <template v-else-if="page==='account'">
          <button class="back" @click="goHome" :disabled="isBusy('auth')">← 返回任务列表</button><div class="page-heading compact"><div><span class="eyebrow">ACCOUNT SECURITY</span><h1>账号安全<span class="heading-dot">.</span></h1><p>保护你的个人学习记录与教学数据。</p></div></div>
          <section class="panel detail-panel password-panel"><h2>修改密码</h2><p class="subtle">输入当前密码确认身份。修改成功后，所有设备上的登录会话都会失效，请重新登录。</p>
            <form @submit.prevent="submitPasswordChange"><fieldset :disabled="isBusy('auth')"><label>当前密码<input v-model="passwordForm.current_password" type="password" autocomplete="current-password" required maxlength="256"></label><label>新密码<input v-model="passwordForm.new_password" type="password" autocomplete="new-password" required minlength="12" maxlength="256" aria-describedby="new-password-hint"></label><p id="new-password-hint" class="hint">12–256 个字符，不与当前密码相同。</p><label>确认新密码<input v-model="passwordForm.password_confirmation" type="password" autocomplete="new-password" required minlength="12" maxlength="256"></label><button class="button primary wide" :disabled="isBusy('auth')">{{ isBusy('auth') ? '正在修改…' : '修改密码并重新登录' }}</button></fieldset></form>
          </section><p class="hint">忘记密码？请通过学校指定渠道联系系统运营管理员核对身份，教师不能在网页中重置其他人的账号。</p>
        </template>
        <template v-else-if="page==='classrooms'">
          <div class="page-heading"><div><span class="eyebrow">CLASSROOMS</span><h1>我的班级<span class="heading-dot">.</span></h1><p>{{ teacher ? '管理成员，快速为班级布置互评任务。' : '通过老师提供的邀请码申请加入班级。' }}</p></div><button class="button secondary" :disabled="navBusy" @click="openClassrooms">刷新班级</button></div>
          <form v-if="teacher" class="panel detail-panel classroom-form" @submit.prevent="addClassroom"><label>班级名称<input v-model="classroomName" maxlength="100" required placeholder="例如：软件工程 2026 级"></label><button class="button primary" :disabled="busy || !classroomName.trim()">创建班级</button></form>
          <form v-else class="panel detail-panel classroom-form" @submit.prevent="joinClassroom"><label>班级邀请码<input v-model="classInviteCode" maxlength="64" required autocomplete="off" placeholder="输入教师提供的邀请码"></label><button class="button primary" :disabled="busy || !classInviteCode.trim()">申请加入</button></form>
          <div v-if="!classroomList.length" class="empty">还没有班级</div>
          <div v-else class="cards"><button v-for="item in classroomList" :key="item.id" class="assignment-card" :disabled="navBusy" @click="openClassroom(item.id)"><div class="card-top"><span class="card-icon" aria-hidden="true">▣</span><span v-if="!teacher" class="pill" :class="item.membership_status === 'ACTIVE' ? 'green' : 'muted'">{{ item.membership_status === 'ACTIVE' ? '已加入' : '待批准' }}</span></div><h3>{{ item.name }}</h3><p>{{ teacher ? '查看成员、邀请码与班级任务' : '查看班级及已分配任务' }}</p></button></div>
        </template>
        <template v-else-if="page==='classroom' && classroom">
          <button class="back" @click="openClassrooms">← 返回班级列表</button>
          <div class="page-heading compact"><div><span class="eyebrow">CLASSROOM</span><h1>{{ classroom.name }}<span class="heading-dot">.</span></h1><p v-if="teacher">已批准 {{ activeMembers.length }} 人 · 待批准 {{ (classroom.members || []).length - activeMembers.length }} 人</p><p v-else>{{ classroom.membership_status === 'ACTIVE' ? '你已加入这个班级。' : '加入申请已提交，等待教师批准。' }}</p></div><button class="button secondary" :disabled="navBusy" @click="openClassroom(classroom.id)">刷新班级</button></div>
          <template v-if="teacher">
            <section class="panel detail-panel"><h2>邀请码</h2><p class="subtle">学生持邀请码申请加入，教师批准后成为成员。请仅在班级中分享邀请码。</p><p class="invite-code">{{ classroom.invite_code || '邀请码已撤销' }}</p><div class="classroom-actions"><button class="button secondary" :disabled="busy" @click="changeInvite('rotate')">生成新邀请码</button><button v-if="classroom.invite_code" class="button secondary" :disabled="busy" @click="changeInvite('disable')">撤销邀请码</button></div></section>
            <section class="panel detail-panel"><h2>批量导入成员</h2><p class="subtle">输入已注册学生的用户名，每行一个，也支持逗号分隔。导入后直接成为已批准成员；若含无效账号则整批不导入。</p><form @submit.prevent="importMembers"><label>学生用户名<textarea v-model="classImportText" rows="4" maxlength="51000" required placeholder="student_01&#10;student_02"></textarea></label><button class="button primary" :disabled="busy || !classImportText.trim()">批量导入</button></form></section>
            <section class="panel detail-panel"><h2>成员与加入申请</h2><p class="subtle">移出班级不会改变已经创建的任务及其参与名单。</p><div v-if="!classroom.members.length" class="empty">尚无成员或申请</div><ul v-else class="submission-roster"><li v-for="member in classroom.members" :key="member.id" class="class-member"><span><strong>{{ member.display_name }}</strong><small>{{ member.username }}</small></span><span class="pill" :class="member.status === 'ACTIVE' ? 'green' : 'muted'">{{ member.status === 'ACTIVE' ? '已批准' : '待批准' }}</span><div class="classroom-actions"><button v-if="member.status === 'PENDING'" class="button secondary" :disabled="busy" @click="changeMember(member,'approve')">批准</button><button class="link-button" :disabled="busy" @click="changeMember(member,'remove')">{{ member.status === 'PENDING' ? '拒绝' : '移出' }}</button></div></li></ul></section>
            <section class="panel detail-panel"><h2>班级任务</h2><p class="subtle">任务建立时保存参与学生名单，此后变更班级成员不会影响历史任务。</p><button class="button primary" :disabled="navBusy || activeMembers.length < 3" @click="newClassAssignment(classroom.id)">从本班创建任务</button></section>
          </template>
          <section v-if="teacher || classroom.membership_status === 'ACTIVE'" class="section"><div class="section-title"><h2>本班已分配任务</h2></div><div v-if="!classroom.assignments?.length" class="empty">暂无班级任务</div><div v-else class="cards"><button v-for="item in classroom.assignments" :key="item.id" class="assignment-card" :disabled="navBusy" @click="openAssignment(item.id)"><h3>{{ item.title }}</h3><p>{{ item.status === 'CLOSED' ? '已结束' : '查看任务详情' }}</p></button></div></section>
        </template>
        <template v-else-if="page==='create'"><button class="back" @click="routeId ? openClassroom(routeId) : goHome()">{{ routeId ? '← 返回班级' : '← 返回任务列表' }}</button><div class="page-heading compact"><div><span class="eyebrow">NEW ASSIGNMENT</span><h1>创建互评任务<span class="heading-dot">.</span></h1><p>清晰的要求，是有价值的反馈的起点。</p><p v-if="creationClassroomName" class="subtle">班级：{{ creationClassroomName }} · 仅可选择已批准成员</p></div></div><form class="panel form-panel" @submit.prevent="createAssignment"><div class="form-section"><div><h3>基本信息</h3><p>告诉学生作业的目标和要求。</p></div><div class="fields"><label>任务名称<input v-model="creation.title" maxlength="200" required placeholder="例如：人工智能与教育案例分析"></label><label>作业描述 <small>{{ codePoints(creation.description) }} / 10000</small><textarea v-model="creation.description" rows="5" maxlength="10000" required placeholder="描述作业要求、范围和注意事项"></textarea></label><div class="two-fields"><label>提交截止<input v-model="creation.submit_deadline" type="datetime-local" required></label><label>互评截止<input v-model="creation.review_deadline" type="datetime-local" required></label></div><p v-if="deadlineErrors.length" class="field-error" role="alert">{{ deadlineErrors.join('；') }}</p><p v-else-if="creation.submit_deadline && creation.review_deadline" class="field-ok">✓ 时间安排有效，将按本机时区换算为 UTC 保存</p></div></div><div class="form-section"><div><h3>评分维度</h3><p>每项 0–5 分，互评开始后固定。</p></div><div class="fields"><label v-for="(_,i) in creation.rubric" :key="i">维度 {{ i+1 }}<input v-model="creation.rubric[i]" maxlength="100" required></label></div></div><div class="form-section"><div><h3>参与学生</h3><p>至少选择 3 名学生。</p></div><div class="student-grid"><div class="student-tools"><span>已选 {{ creation.student_ids.length }} / {{ students.length }} 人（至少 3 人）</span><button type="button" class="link-button" @click="creation.student_ids = students.map(s => s.id)" :disabled="students.length > 500">全选</button><button type="button" class="link-button" @click="creation.student_ids = []">清空</button></div><label v-for="s in students" :key="s.id" class="check"><input type="checkbox" v-model="creation.student_ids" :value="s.id"> {{ s.display_name }} <small>{{ s.username }}</small></label><p v-if="!students.length" class="subtle">尚无学生账号。学生注册后会出现在名单中。</p></div></div><div class="form-actions"><button type="button" class="button secondary" @click="goHome">取消</button><button class="button primary" :disabled="busy || creation.student_ids.length<3 || creation.student_ids.length>500 || deadlineErrors.length" :aria-busy="isBusy('create')">{{ isBusy('create') ? '正在创建…' : '创建任务 →' }}</button></div></form></template>
        <template v-else-if="page==='assignment' && active">
          <button class="back" @click="goHome">← 返回任务列表</button>
          <div class="page-heading compact">
            <div>
              <h1>{{ active.title }}<span class="heading-dot">.</span></h1>
              <p><span class="pill" :class="statusClass(active.phase)">{{ assignmentStatus(active) }}</span> <span class="inline-meta">提交截止 {{ date(active.submit_deadline) }}　·　互评截止 {{ date(active.review_deadline) }}</span></p>
            </div>
          </div>
          <div class="detail-grid">
            <div>
              <section class="panel detail-panel">
                <h2>任务说明</h2>
                <p class="prose">{{ active.description }}</p>
                <h3 class="small-heading">评分维度 <small>每项 5 分</small></h3>
                <div class="rubrics"><span v-for="(r,i) in active.rubric" :key="i">{{ r }}</span></div>
              </section>
              <section v-if="!teacher" class="panel detail-panel">
                <h2>你的作业</h2>
                <template v-if="active.my_submission">
                  <p class="inline-meta">当前已交 · {{ active.my_submission.content_type === 'PDF' ? 'PDF 提取文本' : '在线文字' }}</p>
                  <div class="prose submission-preview" tabindex="0" role="region" aria-label="当前已提交的作业正文">{{ active.my_submission.content_text }}</div>
                </template>
                <p v-else class="subtle">{{ canSubmit ? '你还没有提交作业。' : '你未在截止前提交作业。' }}</p>
                <details v-if="!canSubmit && text.trim() && text !== active.my_submission?.content_text" class="unsent-draft">
                  <summary>未提交的文字草稿</summary>
                  <div class="prose submission-preview">{{ text }}</div>
                </details>
                <template v-if="canSubmit">
                  <p class="subtle">提交前请删除姓名、学号等个人信息。</p>
                  <label>在线输入 <small>{{ codePoints(text.trim()) }} / 10000</small><textarea v-model="text" rows="10" placeholder="在这里输入你的作业正文…"></textarea></label>
                  <button class="button primary" :disabled="isBusy('submitText') || isBusy('upload') || !text.trim() || codePoints(text.trim()) > 10000" :aria-busy="isBusy('submitText')" @click="submitText">{{ isBusy('submitText') ? '正在保存…' : '保存文字作业' }}</button>
                  <div class="upload">
                    <div><strong>或上传文字型 PDF</strong><p>最多 5 MB、10 页，提取文本最多 30000 字符。</p><p v-if="active.my_submission">上传成功后会替换当前版本。</p></div>
                    <input ref="pdfInput" type="file" accept="application/pdf,.pdf" aria-label="选择 PDF 文件" @change="onPdfChange">
                    <button class="button secondary" :disabled="isBusy('upload') || isBusy('submitText') || !pdf" :aria-busy="isBusy('upload')" @click="submitPDF">{{ isBusy('upload') ? '正在解析…' : '提取并提交' }}</button>
                  </div>
                </template>
              </section>
            </div>
            <aside class="panel action-panel">
              <h3>{{ teacher ? '管理任务' : '反馈进度' }}</h3>
              <template v-if="teacher">
                <p>有效提交 {{ active.submission_count }} / {{ active.member_count }} 人 · 至少 {{ active.minimum_submission_count }} 人才能开启互评。</p>
                <p v-if="active.phase === 'SUBMISSION_ENDED' && active.submission_count < active.minimum_submission_count">提交人数不足，可延长截止时间等待补交。</p>
                <p v-else-if="active.phase === 'REVIEW_NOT_STARTED'">互评截止时间已过，本次未生成互评任务。</p>
                <p v-else-if="active.phase === 'CLOSED' && !active.review_started">本次未开展互评。</p>
                <button v-if="actions.includes('start_review')" class="button primary wide" :disabled="busy" :aria-busy="isBusy('start')" @click="startReview">{{ isBusy('start') ? '正在开启…' : '开启匿名互评 →' }}</button>
                <button v-if="actions.includes('close')" class="button primary wide" :disabled="busy" :aria-busy="isBusy('close')" @click="closeAssignment">{{ isBusy('close') ? '正在结束…' : active.phase === 'REVIEW_NOT_STARTED' ? '结束任务' : '发布反馈并结束任务' }}</button>
                <template v-if="actions.includes('extend_deadlines')">
                  <button v-if="!extending" class="button secondary wide" :disabled="busy" @click="openExtension">延长截止时间</button>
                  <form v-else class="deadline-form" @submit.prevent="extendDeadlines">
                    <label>新提交截止<input v-model="extension.submit_deadline" type="datetime-local" required></label>
                    <label>互评截止<input v-model="extension.review_deadline" type="datetime-local" required></label>
                    <button class="button primary wide" :disabled="busy">{{ isBusy('extend') ? '正在保存…' : '保存截止时间' }}</button>
                    <button type="button" class="button secondary wide" :disabled="isBusy('extend')" @click="extending = false">取消</button>
                  </form>
                </template>
                <button class="button secondary wide" :disabled="navBusy" @click="openDashboard(active.id)">查看教师看板</button>
                <button class="button secondary wide" :disabled="navBusy" @click="newAssignment(active)">复制为新任务</button>
              </template>
              <template v-else>
                <p v-if="active.phase === 'REVIEW_NOT_STARTED' || (active.phase === 'CLOSED' && !active.review_started)">本次未开展互评。</p>
                <p v-else-if="active.phase === 'CLOSED' && !active.my_submission">你未提交作业，本次没有个人反馈。</p>
                <p v-else-if="active.phase === 'SUBMISSION_ENDED' && active.my_submission">作业已交，等待教师开启互评。</p>
                <p v-else-if="active.my_submission && ['REVIEWING', 'READY_TO_PUBLISH'].includes(active.phase)">互评反馈将在教师发布后显示。</p>
                <p v-else-if="canSubmit">请在提交截止前完成作业。</p>
                <button v-if="active.phase === 'CLOSED' && active.my_submission" class="button primary wide" :disabled="navBusy" @click="openFeedback(active.id)">查看收到的反馈 →</button>
              </template>
            </aside>
          </div>
        </template>
        <template v-else-if="page==='review' && detail"><button class="back" @click="goHome">← 返回任务列表</button><div class="page-heading compact"><div><span class="eyebrow">EVIDENCE-BASED REVIEW</span><h1>认真阅读，具体反馈<span class="heading-dot">.</span></h1><p>{{ detail.assignment.title }}　·　匿名作业 {{ detail.anonymous_code }}</p></div><span class="pill" :class="statusClass(detail.phase)">{{ statusText[detail.phase] }}</span></div><div v-if="readOnly" class="readonly-banner" role="status"><span aria-hidden="true">✓</span> {{ detail.status==='COMPLETED' ? '该互评已提交，以下内容为只读存档，不能修改或重复提交。' : '互评阶段已结束，当前内容仅供查看，不能修改或提交。' }}</div><div class="review-layout"><section class="panel reading"><div class="reading-head"><div><span class="panel-kicker">01 / 阅读原文</span><h2>同伴的作业</h2></div><span class="anonymous">匿名 {{ detail.anonymous_code }}</span></div><div v-if="!readOnly" class="selection-tip" :class="{active: !!selectionInfo, limit: selected.length>=3}" aria-live="polite"><template v-if="selected.length>=3">已添加 3 处证据，达到上限；删除一条后可继续添加。</template><template v-else-if="selectionInfo">✳ 已选中 {{ selectionInfo.length }} 字，可添加为第 {{ selected.length+1 }} 处证据。</template><template v-else>✳　选中一段原文，再点击「添加证据」。</template></div><div class="source-text" ref="source" tabindex="0" role="region" aria-label="作业原文，可选中文字作为证据" @mouseup="readSelection" @keyup="readSelection" @touchend="readSelection"><template v-for="(part,i) in highlighted(detail.content_text,readingAnchors)" :key="part.start"><mark v-if="part.marked" :id="'review-source-'+part.start" tabindex="-1" :class="{'focused-evidence': diagnosticFocus && diagnosticFocus.start <= part.start && part.end <= diagnosticFocus.end}">{{ part.text }}</mark><span v-else>{{ part.text }}</span></template></div><button v-if="!readOnly" class="button secondary" type="button" :disabled="isBusy('nav') || isBusy('auth') || !selectionInfo || selected.length>=3" @mousedown.prevent @click="addEvidence">{{ selected.length>=3 ? '已达 3 处证据上限' : '＋ 添加选中内容为证据' }}</button></section><section class="panel review-form"><span class="panel-kicker">02 / 撰写评价</span><h2>你的反馈</h2><p class="subtle">指出具体内容，并提供作者可以执行的建议。</p><form @submit.prevent="submitReview"><fieldset :disabled="readOnly || isBusy('submitReview') || isBusy('nav') || isBusy('auth')"><div class="score-list"><label v-for="(r,i) in detail.assignment.rubric" :key="i"><span>{{ r }} <small>/ 5 分</small></span><select v-model.number="shownReview.scores[i]" :aria-label="r + ' 评分'"><option v-for="n in 6" :key="n" :value="n-1">{{ n-1 }} 分</option></select></label></div><label>做得好的地方 <small>{{ codePoints(shownReview.strengths) }} / 3000</small><textarea id="review-field-strengths" v-model="shownReview.strengths" rows="3" maxlength="3000" required placeholder="指出值得保留的具体内容"></textarea></label><label>存在的问题 <small>{{ codePoints(shownReview.weaknesses) }} / 3000</small><textarea id="review-field-weaknesses" v-model="shownReview.weaknesses" rows="3" maxlength="3000" required placeholder="哪些地方还可以更清晰或完整？"></textarea></label><label>改进建议 <small>{{ codePoints(shownReview.suggestions) }} / 3000</small><textarea id="review-field-suggestions" v-model="shownReview.suggestions" rows="3" maxlength="3000" required placeholder="给出可以执行的下一步"></textarea></label><div class="evidence-head"><strong>原文证据</strong><small class="evidence-count">{{ shownEvidence.length }} / 3</small></div><div v-if="!shownEvidence.length" class="evidence-empty">{{ readOnly ? '无原文证据。' : '从左侧选取原文作为证据。' }}</div><div v-for="(a,i) in shownEvidence" :key="i" class="evidence"><span>“{{ a.quote }}”</span><button v-if="!readOnly" type="button" @click="removeEvidence(i)" title="删除这条证据" aria-label="删除这条证据">×</button></div><div v-if="!readOnly" class="draft-status" :class="'draft-'+draftState" role="status" aria-live="polite"><span>{{ draftStatus }}</span><template v-if="draftState === 'conflict'"><button type="button" class="link-button" :disabled="isBusy('draft')" @click="reloadDraft">加载服务器草稿</button><button type="button" class="link-button" :disabled="isBusy('draft')" @click="overwriteDraft">保留本页并覆盖</button></template></div><div v-if="!readOnly" class="review-actions"><button type="button" class="button secondary" :disabled="isBusy('draft') || isBusy('submitReview') || draftState === 'conflict'" :aria-busy="isBusy('draft')" @click="saveDraft">{{ isBusy('draft') ? '正在保存…' : '保存草稿' }}</button><button type="button" class="button secondary" :disabled="isBusy('ai') || isBusy('submitReview') || detail.ai_limit_reached || !canCheckAI" :aria-busy="isBusy('ai')" @click="checkAI">{{ isBusy('ai') ? '✳ 检测中…' : '✳ 检测反馈质量' }}</button><button class="button primary" :disabled="isBusy('submitReview') || isBusy('draft')" :aria-busy="isBusy('submitReview')">{{ isBusy('submitReview') ? '正在提交…' : '提交评价 →' }}</button></div><p v-if="!readOnly && detail.ai_limit_reached" class="hint" role="status">本次互评的 AI 检测额度已用完（最多 3 次，含失败及超时），你仍可以保存草稿或提交评价。</p><p v-else-if="!readOnly && !canCheckAI" class="hint">请先写下至少一段评价，再检测反馈质量；检测不强制，也不会修改你的文字。</p><p v-else-if="!readOnly" class="hint">AI 检测可选；每条互评最多调用 3 次（含失败及超时）。<template v-if="Number.isInteger(detail.ai_attempts_remaining)">剩余 {{ detail.ai_attempts_remaining }} 次。</template>检测会将作业摘录、所选证据和评价草稿发送至配置的模型服务。提示不计入作业分数，也不会修改评价。</p></fieldset></form>
          <details v-if="hasUnsentReview" class="unsent-draft">
            <summary>未提交的评价草稿</summary>
            <p class="inline-meta">{{ detail.assignment.rubric.map((label, i) => label + ' ' + review.scores[i] + '/5').join(' · ') }}</p>
            <template v-for="(label, key) in commentLabels" :key="key">
              <h3 v-if="review[key]" class="small-heading">{{ label }}</h3>
              <p v-if="review[key]" class="prose">{{ review[key] }}</p>
            </template>
            <div v-for="anchor in selected" :key="anchor.start + ':' + anchor.end" class="evidence">“{{ anchor.quote }}”</div>
          </details>
          <div v-if="quality && !readOnly" class="quality" role="status"><span class="eyebrow">QUALITY CHECK</span><h3>反馈质量提示 <span>{{ quality.quality_level }}</span></h3><p>具体性 {{ quality.specificity }}/2　依据性 {{ quality.evidence_grounding }}/2　可操作性 {{ quality.actionability }}/2</p><ul><li v-for="(issue,i) in quality.issues||[]" :key="'i'+i">{{ issue.message }}</li><li v-for="(s,i) in quality.suggestions||[]" :key="'s'+i">{{ s }}</li></ul>
          <div v-for="(d,i) in quality.diagnostics || []" :key="i" class="diagnostic-card"><strong>{{ issueLabel(d.type) }} · {{ commentLabels[d.field] }}</strong><p>{{ d.reason }}</p><p class="prose">改进方向：{{ d.guidance }}</p><div class="classroom-actions"><button v-if="d.review_location" type="button" class="button secondary" @click="goToDiagnosticReview(d)">定位评语</button><button v-if="d.source_location" type="button" class="button secondary" @click="goToDiagnosticSource(d)">定位原文</button></div><p v-if="d.location_status !== 'LOCATED'" class="hint">部分或全部引文无法核实，请结合原文自行判断。</p></div>
          <p v-if="!quality.diagnostics?.length" class="hint">本次检测未提供可定位的诊断。</p></div></section></div></template>
        <template v-else-if="page==='feedback' && feedback">
          <button class="back" @click="openAssignment(routeId)">← 返回任务详情</button>
          <div class="page-heading compact"><h1>收到的反馈<span class="heading-dot">.</span></h1></div>
          <div class="overview dashboard-stats">
            <div><span>互评平均分</span><strong>{{ feedback.summary.average_total ?? '—' }}<small> / 15</small></strong><small>{{ feedback.summary.incomplete ? '评价数量不足' : '两位同伴的平均分' }}</small></div>
            <div v-for="(rubric,i) in feedback.rubric" :key="i"><span>{{ rubric }}</span><strong>{{ feedback.summary.dimension_averages?.[i] ?? '—' }}<small> / 5</small></strong></div>
          </div>
          <section class="panel detail-panel">
            <h2>作业原文</h2>
            <div class="source-text">
              <template v-for="part in highlighted(feedback.content_text, feedback.reviews.flatMap(r => r.evidence_anchors || []))" :key="part.start"><mark v-if="part.marked" :id="evidenceTargetId(part.start)" tabindex="-1" :class="{'focused-evidence': focusedEvidence && focusedEvidence.start <= part.start && part.end <= focusedEvidence.end}">{{ part.text }}</mark><span v-else>{{ part.text }}</span></template>
            </div>
          </section>
          <div class="feedback-grid">
            <article v-for="(r,i) in feedback.reviews" :key="i" class="panel detail-panel">
              <h2>匿名评审 {{ i+1 }} · {{ r.total_score }} 分</h2>
              <h3>值得保留</h3><p class="prose">{{ r.strengths }}</p>
              <h3>可以改进</h3><p class="prose">{{ r.weaknesses }}</p>
              <h3>下一步建议</h3><p class="prose">{{ r.suggestions }}</p>
              <div v-for="(a,j) in r.evidence_anchors || []" :key="j" class="evidence">
                <button class="link-button evidence-jump" type="button" :aria-label="'跳到匿名评审 ' + (i+1) + ' 的第 ' + (j+1) + ' 处原文证据：' + a.quote" @click="goToEvidence(a)">“{{ a.quote }}” <span aria-hidden="true">↗</span></button>
              </div>
              <section v-if="r.feedback_id" class="review-case">
                <h3>反馈采纳 <span class="pill" :class="r.adoption?.decision === 'ADOPTED' ? 'green' : 'muted'">{{ adoptionLabel(r.adoption?.decision) }}</span></h3>
                <p v-if="r.adoption" class="prose">{{ r.adoption.reason }}</p>
                <button v-if="adoptionEditor?.feedbackId !== r.feedback_id" type="button" class="button secondary" :disabled="busy" @click="openAdoption(r)">{{ r.adoption ? '修改采纳记录' : '记录采纳决定' }}</button>
                <form v-else @submit.prevent="saveAdoption"><label>采纳决定<select v-model="adoptionEditor.decision"><option value="ADOPTED">采纳</option><option value="NOT_ADOPTED">暂不采纳</option></select></label><label>采纳理由<textarea v-model="adoptionEditor.reason" rows="3" maxlength="1500" required></textarea></label><div class="classroom-actions"><button class="button primary" :disabled="busy || !adoptionEditor.reason.trim()">保存采纳记录</button><button type="button" class="button secondary" :disabled="isBusy('adoption')" @click="adoptionEditor=null">取消</button></div></form>
              </section>
              <section v-if="r.feedback_id" class="review-case">
                <h3>反馈复核 <span v-if="r.review_case" class="pill" :class="r.review_case.status === 'RESOLVED' ? 'green' : 'purple'">{{ r.review_case.status === 'RESOLVED' ? '已处理' : '待处理' }}</span></h3>
                <p v-if="r.review_case?.student_reason" class="prose">我的申请：{{ r.review_case.student_reason }}</p>
                <p v-if="r.review_case && !r.review_case.student_reason" class="subtle">教师已标记该反馈需要复核。</p>
                <p v-if="r.review_case?.resolution" class="prose">教师处理意见：{{ r.review_case.resolution }}</p>
                <button v-if="(!r.review_case || (r.review_case.status === 'PENDING' && !r.review_case.student_reason)) && caseEditor?.feedbackId !== r.feedback_id" type="button" class="button secondary" :disabled="busy" @click="openCaseEditor(r.feedback_id, 'appeal')">申请复核</button>
                <form v-if="caseEditor?.feedbackId === r.feedback_id && caseEditor.action === 'appeal'" @submit.prevent="submitCase"><label>申请理由<textarea v-model="caseEditor.text" rows="4" maxlength="1500" required placeholder="说明需要教师复核的具体问题"></textarea></label><div class="classroom-actions"><button class="button primary" :disabled="busy || !caseEditor.text.trim()">提交复核申请</button><button type="button" class="button secondary" :disabled="isBusy('case')" @click="caseEditor = null">取消</button></div></form>
                <p v-if="r.review_case" class="hint">复核不会修改原匿名评价或其分数。</p>
              </section>
            </article>
          </div>
          <div v-if="!feedback.reviews.length" class="empty">{{ feedback.review_started ? '本次收到 0 份评价。' : '本次未开展互评，收到 0 份评价。' }}</div>
          <section v-if="feedback.reviews.length" class="panel detail-panel"><span class="panel-kicker">REFLECT &amp; REVISE</span><h2>作业修订轨迹</h2><p class="subtle">原作业和评价证据保持不变。每次保存会创建独立版本及当时的采纳记录，教师可查看。</p>
            <p>{{ feedback.revisions?.length || 0 }} / {{ feedback.revision_limit ?? 5 }} 版</p>
            <details v-for="v in feedback.revisions || []" :key="v.id" class="check-record"><summary>修订版 {{ v.version }} · {{ date(v.created_at) }}</summary><h3>修订说明</h3><p class="prose">{{ v.note }}</p><h3>修订正文</h3><p class="prose">{{ v.content_text }}</p><h3>当时的采纳记录</h3><p v-for="a in v.adoption_snapshot" :key="a.feedback_id">匿名评审 {{ feedback.reviews.findIndex(r => r.feedback_id === a.feedback_id)+1 }} · {{ adoptionLabel(a.decision) }} · {{ a.reason || '无理由' }}</p></details>
            <button v-if="!revisionDraft && (feedback.revisions?.length || 0) < (feedback.revision_limit ?? 5)" class="button primary" :disabled="busy" @click="startRevision">开始修订</button>
            <form v-if="revisionDraft" @submit.prevent="saveRevision"><label>修订正文<textarea v-model="revisionDraft.content_text" rows="10" maxlength="30000" required></textarea></label><label>修订说明<textarea v-model="revisionDraft.note" rows="4" maxlength="1500" required placeholder="说明作了哪些调整、为什么调整"></textarea></label><div class="classroom-actions"><button :disabled="busy || !revisionDraft.note.trim() || !revisionDraft.content_text.trim()" class="button primary">保存新修订版本</button><button type="button" :disabled="isBusy('revision')" class="button secondary" @click="revisionDraft=null">取消</button></div></form>
          </section>
        </template>
        <template v-else-if="page==='learning' && learning">
          <button class="back" @click="goHome">← 返回任务列表</button><div class="page-heading compact"><div><span class="eyebrow">LEARNING JOURNEY</span><h1>{{ learning.display_name }}的学习档案<span class="heading-dot">.</span></h1><p>{{ teacher ? '仅展示您所负责的任务' : '观察历次互评完成情况及反馈质量' }}</p></div></div>
          <div class="overview dashboard-stats"><div><span>完成互评</span><strong>{{ learning.completed_reviews }} / {{ learning.assigned_reviews }}</strong><small>共计 {{ learning.assignments.length }} 项任务</small></div><div v-for="(label,key) in dimensionLabels" :key="key"><span>{{ label }}</span><strong>{{ meanLabel(learning.dimension_means?.[key]) }}</strong><small>AI 质量提示 · 0–2</small></div></div>
          <section class="panel detail-panel"><h2>历次互评</h2><p class="hint">只统计与最终提交评价一致的成功检测，共 {{ learning.sample_count }} 个样本。— 表示没有数据，0 是有效诊断分值。AI 质量提示不是作业成绩，不用于学生排名。</p><div v-if="!learning.assignments.length" class="empty">暂无任务记录。</div><article v-for="row in learning.assignments" :key="row.assignment_id" class="learning-row"><div><button class="link-button" @click="openAssignment(row.assignment_id)">{{ row.title }}</button><p class="subtle">完成 {{ row.completed_reviews }} / {{ row.assigned_reviews }} 条 · 检测样本 {{ row.sample_count }} 条</p></div><div class="dimension-grid"><div v-for="(label,key) in dimensionLabels" :key="key"><span>{{ label }}</span><strong>{{ meanLabel(row.dimension_means?.[key]) }}</strong><progress v-if="Number.isFinite(row.dimension_means?.[key])" :value="row.dimension_means[key]" max="2"></progress></div></div></article></section>
        </template>
        <template v-else-if="page==='dashboard' && dashboard"><button class="back" @click="openAssignment(dashboard.assignment.id)">← 返回任务详情</button><div class="page-heading compact"><div><span class="eyebrow">TEACHER DASHBOARD</span><h1>互评进展<span class="heading-dot">.</span></h1><p>{{ dashboard.assignment.title }} · 分歧提示仅供教师复核，不代表评价异常。</p></div><a class="button secondary" :href="'/api/assignments/' + dashboard.assignment.id + '/results.csv'" download>导出统计 CSV</a></div><div class="overview dashboard-stats"><div><span>作业提交率</span><strong>{{ percent(dashboard.submission_count,dashboard.member_count) }}</strong><small>{{ dashboard.submission_count }} / {{ dashboard.member_count }} 人</small></div><div><span>互评完成率</span><strong>{{ percent(dashboard.review_count,dashboard.review_task_count) }}</strong><small>{{ dashboard.review_count }} / {{ dashboard.review_task_count }} 条</small></div><div><span>完整评分平均</span><strong>{{ dashboard.complete_average?.toFixed(1) ?? '—' }}</strong><small>满分 15 分</small></div><div><span>需关注的分歧</span><strong>{{ dashboard.high_disagreement_count }}</strong><small>两次评分相差 ≥ 5 分</small></div></div><section class="panel detail-panel">
            <h2>学生提交名单</h2>
            <ul class="submission-roster">
              <li v-for="student in dashboard.submission_roster" :key="student.id">
                <span><strong>{{ student.display_name }}</strong> <small>{{ student.username }}</small></span>
                <span class="pill" :class="student.submitted ? 'green' : 'muted'">{{ student.submitted ? '已交' : '未交' }}</span>
                <button class="link-button" :disabled="navBusy" @click="openLearning(student.id)">查看学习档案</button>
              </li>
            </ul>
          </section>
          <section v-if="dashboard.assignment.status === 'CLOSED'" class="panel detail-panel"><h2>反馈复核</h2><p>待处理 {{ dashboard.review_case_pending ?? 0 }} 条 · 已处理 {{ dashboard.review_case_resolved ?? 0 }} 条</p><label class="check"><input type="checkbox" v-model="pendingCasesOnly"> 仅查看有待处理复核的作业</label><p class="hint">教师可标记争议并给出处理意见，原评价及分数不变。</p></section>
          <section class="panel detail-panel"><div class="panel-kicker">QUALITY INSIGHTS</div><h2>反馈质量检测</h2><p class="subtle">共发起 {{ dashboard.ai_call_count }} 次检测（成功 {{ dashboard.ai_success_count }} 次 / 失败 {{ dashboard.ai_failed_count }} 次 / 检测中 {{ dashboard.ai_running_count }} 次）· 低 {{ dashboard.ai_levels.LOW }} · 中 {{ dashboard.ai_levels.MEDIUM }} · 高 {{ dashboard.ai_levels.HIGH }}。只有与最终评价内容一致的检测结果计入等级。</p><p class="hint">存在 {{ dashboard.ai_paired_count }} 组评语发生变化的前后草稿检测（草稿差异检测，不构成实验配对数据）；平均变化 {{ dashboard.ai_paired_mean_change === null ? "—" : dashboard.ai_paired_mean_change.toFixed(1) }} 分。模型评分仅供参考，不能代替人工实验；正式效果验证须按 docs/experiment.md 组织人工盲评。</p><div class="quality-distribution"><div v-for="(label,key) in {LOW:'低',MEDIUM:'中',HIGH:'高'}" :key="key"><span>{{ label }} · {{ dashboard.ai_levels[key] }}</span><progress :value="dashboard.ai_levels[key]" :max="Math.max(1, dashboard.ai_dimension_sample_count ?? Object.values(dashboard.ai_levels).reduce((a,b)=>a+b,0))"></progress></div></div><p class="subtle">有效最终评价样本：{{ dashboard.ai_dimension_sample_count ?? 0 }} 条 · 维度均分（0–2）</p><div class="dimension-grid"><div v-for="(label,key) in dimensionLabels" :key="key"><strong>{{ label }}：{{ meanLabel(dashboard.ai_dimension_means?.[key]) }}</strong><p>首次 {{ meanLabel(dashboard.ai_paired_dimensions?.before?.[key]) }} → 末次 {{ meanLabel(dashboard.ai_paired_dimensions?.after?.[key]) }}</p></div></div><div class="tags"><span v-for="(count,key) in dashboard.ai_issue_counts" :key="key">{{ issueLabel(key) }} · {{ count }}</span></div></section><section class="section"><div class="section-title"><div><span class="eyebrow">SUBMISSIONS</span><h2>作业与评价 <span>{{ dashboard.records.length }}</span></h2></div></div><label class="check"><input type="checkbox" v-model="highDisagreementOnly"> 仅查看高分歧作业</label><div v-if="!displayedRecords.length" class="empty">{{ highDisagreementOnly || pendingCasesOnly ? '没有符合筛选条件的作业。' : '尚无学生提交作业，暂无可查看的互评记录。' }}</div><details v-for="record in displayedRecords" :key="record.submission_id" class="panel record"><summary><strong>{{ record.student_name }}</strong><span v-if="record.summary.high_disagreement" class="pill purple">高分歧，建议复核</span><span>{{ record.summary.incomplete ? '评价数量不足' : '平均 '+(record.summary.average_total ?? '—')+' / 15' }}</span></summary><div class="record-body"><h3>作业正文</h3><p class="prose">{{ record.content_text }}</p><button v-if="record.revision_count" class="button secondary" type="button" :disabled="busy" @click="showRevisionHistory(record.submission_id)">查看修订轨迹（{{ record.revision_count }} 版）</button><div v-if="teacherRevision?.submissionId === record.submission_id" class="revision-list"><details v-for="v in teacherRevision.revisions" :key="v.id" class="check-record"><summary>修订版 {{ v.version }} · {{ date(v.created_at) }}</summary><p class="prose">{{ v.note }}</p><p class="prose">{{ v.content_text }}</p><p v-for="a in v.adoption_snapshot" :key="a.feedback_id">{{ adoptionLabel(a.decision) }} · {{ a.reason || "未记录理由" }}</p></details></div><div v-for="r in record.reviews" :key="r.id" class="record-review"><strong>评审：{{ r.reviewer_name }} · {{ statusText[r.phase] }}</strong><template v-if="r.review"><p>得分：{{ (r.review.scores||[]).join(' / ') }}（总分 {{ r.review.total_score }}）</p><p>优点：{{ r.review.strengths }}</p><p>问题：{{ r.review.weaknesses }}</p><p>建议：{{ r.review.suggestions }}</p><div v-for="a in (r.review.evidence_anchors||[])" :key="a.start" class="evidence">“{{ a.quote }}”</div></template><p v-if="r.adoption" class="prose"><strong>作者{{ adoptionLabel(r.adoption.decision) }}：</strong>{{ r.adoption.reason }}</p><section v-if="r.feedback_id && dashboard.assignment.status === 'CLOSED'" class="review-case"><h3>反馈复核 <span v-if="r.review_case" class="pill" :class="r.review_case.status === 'RESOLVED' ? 'green' : 'purple'">{{ r.review_case.status === 'RESOLVED' ? '已处理' : '待处理' }}</span></h3><p v-if="r.review_case?.student_reason" class="prose">学生申请：{{ r.review_case.student_reason }}</p><p v-if="r.review_case?.teacher_reason" class="prose">教师标记（仅教师可见）：{{ r.review_case.teacher_reason }}</p><p v-if="r.review_case?.resolution" class="prose">处理意见：{{ r.review_case.resolution }}</p><div class="classroom-actions" v-if="caseEditor?.feedbackId !== r.feedback_id"><button v-if="!r.review_case || (r.review_case.status === 'PENDING' && !r.review_case.teacher_reason)" type="button" class="button secondary" :disabled="busy" @click="openCaseEditor(r.feedback_id,'flag')">标记争议</button><button v-if="r.review_case?.status === 'PENDING'" type="button" class="button primary" :disabled="busy" @click="openCaseEditor(r.feedback_id,'resolve')">填写处理意见</button></div><form v-if="caseEditor?.feedbackId === r.feedback_id" @submit.prevent="submitCase"><label>{{ caseEditor.action === 'flag' ? '标记原因（仅教师可见）' : '公开处理意见（不含匿名评审身份）' }}<textarea v-model="caseEditor.text" rows="4" maxlength="1500" required></textarea></label><label v-if="caseEditor.action === 'resolve'" class="check"><input type="checkbox" v-model="caseEditor.confirm"> 我已核对公开意见不包含评审者身份</label><div class="classroom-actions"><button class="button primary" :disabled="busy || !caseEditor.text.trim() || (caseEditor.action === 'resolve' && !caseEditor.confirm)">{{ caseEditor.action === 'flag' ? '保存争议标记' : '确认处理' }}</button><button type="button" class="button secondary" :disabled="isBusy('case')" @click="caseEditor=null">取消</button></div></form></section><details v-for="(c,i) in r.ai_checks" :key="i" class="check-record">
                <summary>检测草稿 {{ i+1 }} · {{ date(c.created_at) }} · {{ aiCheckStatus(c) }}</summary>
                <p>优点：{{ c.input_data?.review?.strengths ?? '—' }}</p>
                <p>问题：{{ c.input_data?.review?.weaknesses ?? '—' }}</p>
                <p>建议：{{ c.input_data?.review?.suggestions ?? '—' }}</p>
                <p>证据：{{ joinList(c.input_data?.evidence_quotes) }}</p>
                <template v-if="aiQualityTotal(c) !== null">
                  <p>质量分：{{ aiQualityTotal(c) }} / 6（具体性 {{ c.result.specificity }}/2　依据性 {{ c.result.evidence_grounding }}/2　可操作性 {{ c.result.actionability }}/2）</p>
                  <p v-if="!aiCheckIssues(c.result).length">AI 检测问题：无</p>
                  <p v-for="(issue,j) in aiCheckIssues(c.result)" :key="j">AI 检测问题：{{ issueLabel(issue.type) }} · {{ issue.message }}</p>
                  <p>质量提示：{{ aiCheckSuggestions(c.result) }}</p>
                  <div v-for="(d,j) in c.result.diagnostics || []" :key="j" class="diagnostic-card"><strong>{{ issueLabel(d.type) }} · {{ commentLabels[d.field] }}</strong><p>{{ d.reason }} · {{ d.guidance }}</p><p v-if="d.review_location">评语：“{{ d.review_location.quote }}”</p><p v-if="d.source_location">原文：“{{ d.source_location.quote }}”</p><p v-if="d.location_status !== 'LOCATED'" class="hint">部分引文无法核实</p></div>
                </template>
                <p v-else-if="c.status === 'SUCCESS'">该历史检测无可用质量结果。</p>
              </details></div></div></details></section></template>
      </div>
    </main>
  </div>
</template>
