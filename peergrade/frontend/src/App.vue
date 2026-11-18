<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'

/* ---------- 全局状态 ---------- */
const ready = ref(false) // 首屏 /api/auth/me 是否结束，避免已登录用户闪现登录页
const user = ref(null), page = ref('home')
const notice = ref({ type: '', text: '' }) // type: success | info | warn | error
const pending = reactive({}) // 按操作名记录请求中状态，避免一次慢请求锁死整个界面
const busy = computed(() => Object.values(pending).some(Boolean))
const isBusy = (key) => !!pending[key]
let actionVersion = 0

const assignments = ref([]), tasks = ref([]), students = ref([])
const active = ref(null), detail = ref(null), dashboard = ref(null), feedback = ref(null)
const login = reactive({ username: '', password: '' })
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
const hasUnsentReview = computed(() => {
  const submitted = detail.value?.review
  return !!submitted && JSON.stringify(reviewBody()) !== JSON.stringify(editableReview(submitted))
})
const commentLabels = { strengths: '做得好的地方', weaknesses: '存在的问题', suggestions: '改进建议' }
const selectionInfo = ref(null) // 最近一次有效的正文选区快照 {start,end,length}
let editVersion = 0 // 用于丢弃「在途 AI 检测」返回的过期结果

// 一次检测只对应发起时的草稿；任何编辑（含证据增删）都使旧结果失效。
watch([review, selected], () => { quality.value = null; editVersion++ }, { deep: true })

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
function assignmentStatus(item) {
  if (teacher.value) return statusText[item.phase]
  const submitted = item.has_submission ?? !!item.my_submission
  if (item.phase === 'SUBMITTING') return submitted ? '已交' : '未交'
  if (!submitted) return item.phase === 'CLOSED' ? '未提交' : '提交已截止'
  return item.phase === 'CLOSED' ? '已发布' : item.phase === 'SUBMISSION_ENDED' ? '已交' : '等待发布'
}
const pageLabel = { home: '我的任务', create: '创建任务', assignment: '任务详情', review: '撰写互评', feedback: '收到的反馈', dashboard: '教师看板' }
const codePoints = (s) => Array.from(s ?? '').length

/* ---------- 分级提示 ---------- */
function setNotice(type, text) { notice.value = { type, text } }
function clearNotice() { notice.value = { type: '', text: '' } }

/* ---------- 接口层：区分网络失败 / 服务错误 / 业务校验失败 ---------- */
async function api(path, options = {}) {
  let response
  try {
    response = await fetch('/api' + path, {
      credentials: 'same-origin', ...options,
      headers: { ...(options.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }), ...options.headers }
    })
  } catch {
    const err = new Error('网络连接失败，请检查网络或服务器状态后重试')
    err.kind = 'network'; throw err
  }
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detailText = typeof body.detail === 'string' ? body.detail : ''
    const err = new Error(detailText || (response.status >= 500 ? '服务暂时不可用，请稍后重试' : '请求失败，请检查输入后重试'))
    err.kind = response.status >= 500 ? 'server' : 'business'
    err.status = response.status
    throw err
  }
  return body
}
async function run(key, fn, success = '') {
  if (isBusy(key)) return
  actionVersion++
  clearNotice(); pending[key] = true
  try {
    await fn()
    if (success) setNotice('success', success)
  } catch (e) {
    if (e.status === 409 && user.value) {
      try { await refreshOpenDetail() } catch { /* Keep the original conflict message. */ }
    }
    setNotice(e.kind === 'business' ? 'warn' : 'error', e.message || '操作失败，请稍后重试')
  } finally {
    delete pending[key]
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
}
async function refreshOpenDetail() {
  const viewer = user.value
  const version = actionVersion
  const stillCurrent = () => user.value === viewer && actionVersion === version
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
async function signIn() {
  await run('auth', async () => {
    user.value = await post('/auth/login', { username: login.username, password: login.password })
    login.password = ''
    await refresh()
    page.value = 'home'
    await scrollTop()
  })
}
async function signOut() {
  await run('auth', async () => {
    // 本地登出优先：会话过期或网络失败时也必须清理界面状态。
    try { await post('/auth/logout') } catch { /* 忽略 */ }
    user.value = null; assignments.value = []; tasks.value = []
    active.value = detail.value = dashboard.value = feedback.value = null
    selectionInfo.value = null; quality.value = null
    page.value = 'home'
    await scrollTop()
  })
}
async function goHome() { await run('nav', async () => { await refresh(); page.value = 'home'; await scrollTop() }) }
async function openAssignment(id) {
  await run('nav', async () => {
    active.value = await api('/assignments/' + id)
    text.value = active.value.my_submission?.content_type === 'TEXT' ? active.value.my_submission.content_text : ''
    pdf.value = null
    if (pdfInput.value) pdfInput.value.value = ''
    extending.value = false
    page.value = 'assignment'
    await scrollTop()
  })
}
async function newAssignment() {
  await run('nav', async () => { students.value = await api('/students'); page.value = 'create'; await scrollTop() })
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
      student_ids: creation.student_ids
    })
    Object.assign(creation, { title: '', description: '', rubric: ['内容完整性', '逻辑与表达', '分析与创新'], submit_deadline: '', review_deadline: '', student_ids: [] })
    await refresh(); page.value = 'home'; await scrollTop()
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
    const originalReview = extension.review_deadline === localDateTime(item.review_deadline)
    const latest = await api('/assignments/' + item.id + '/deadlines', {
      method: 'PATCH',
      body: JSON.stringify({
        submit_deadline: new Date(extension.submit_deadline).toISOString(),
        review_deadline: originalReview ? item.review_deadline : new Date(extension.review_deadline).toISOString()
      })
    })
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
    const res = await post('/assignments/' + active.value.id + '/start-review')
    active.value = await api('/assignments/' + active.value.id)
    await refresh()
    setNotice('success', res.created ? `已生成 ${res.count} 条匿名互评任务` : '互评已开始')
  })
}
async function closeAssignment() {
  const skipped = active.value.phase === 'REVIEW_NOT_STARTED'
  if (skipped && !window.confirm('本次未生成互评任务。结束后，学生将看到本次未开展互评。确定结束？')) return
  await run('close', async () => {
    await post('/assignments/' + active.value.id + '/close')
    active.value = await api('/assignments/' + active.value.id)
    await refresh()
  }, skipped ? '任务已结束，本次未开展互评' : '任务已结束，学生可以查看反馈')
}
async function openDashboard(id) {
  await run('nav', async () => { dashboard.value = await api('/assignments/' + id + '/dashboard'); page.value = 'dashboard'; await scrollTop() })
}
async function openFeedback(id) {
  await run('nav', async () => {
    feedback.value = await api('/assignments/' + id + '/my-feedback')
    focusedEvidence.value = null
    page.value = 'feedback'
    await scrollTop()
  })
}

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

async function openReview(id) {
  await run('nav', async () => {
    detail.value = await api('/review-tasks/' + id)
    const { evidence_anchors, ...fields } = editableReview(detail.value.review || detail.value.draft)
    Object.assign(review, fields)
    selected.value = evidence_anchors
    quality.value = null
    selectionInfo.value = null
    const sel = window.getSelection(); if (sel) sel.removeAllRanges()
    page.value = 'review'
    await scrollTop()
  })
}

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
  if (selected.value.some(a => a.start === info.start && a.end === info.end)) { setNotice('warn', '该片段已经添加'); return }
  selected.value.push({ start: info.start, end: info.end, quote, content_hash: detail.value.content_hash })
  selectionInfo.value = null
  const sel = window.getSelection(); if (sel) sel.removeAllRanges()
  setNotice('success', `已添加第 ${selected.value.length} 处证据（${Array.from(quote).length} 字）`)
}
function removeEvidence(i) {
  if (readOnly.value) return
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
async function saveDraft() {
  if (readOnly.value) { setNotice('warn', '当前互评阶段已结束，不能保存草稿'); return }
  await run('draft', async () => { await post('/review-tasks/' + detail.value.id + '/draft', reviewBody()) }, '草稿已保存，可以稍后继续')
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
  await run('submitReview', async () => {
    await post('/review-tasks/' + detail.value.id + '/submit', reviewBody())
    await refresh(); page.value = 'home'; await scrollTop()
  }, '评价已提交，感谢你的认真反馈')
}

/* ---------- 渲染辅助 ---------- */
const percent = (n, d) => d ? Math.round(n / d * 100) + '%' : '—'
const date = (d) => d ? new Date(d).toLocaleString('zh-CN', { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—'
const joinList = (v) => Array.isArray(v) ? (v.join('；') || '无') : '—'
const evidenceTargetId = (start) => 'feedback-source-' + start
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
  if (!ready.value || !user.value || busy.value || document.hidden || refreshingStatus) return
  refreshingStatus = true
  try {
    if (page.value === 'home') await refresh(true)
    else if ((page.value === 'assignment' && active.value?.phase !== 'CLOSED')
        || (page.value === 'review' && detail.value?.can_write)) await refreshOpenDetail()
  } catch { /* The current state remains visible while connectivity recovers. */ }
  finally { refreshingStatus = false }
}
onMounted(async () => {
  document.addEventListener('selectionchange', onSelectionChange)
  document.addEventListener('visibilitychange', updateStatus)
  statusTimer = setInterval(updateStatus, 15000)
  try {
    user.value = await api('/auth/me')
    await refresh()
  } catch (e) {
    user.value = null
    // 401 是「尚未登录」的正常路径；其余错误必须可见，不能静默停在登录页。
    if (e.status !== 401) setNotice('error', e.kind === 'network' ? '无法连接到服务器，请检查网络后刷新页面重试' : '服务暂时不可用，请稍后刷新页面重试')
  } finally {
    ready.value = true
  }
})
onBeforeUnmount(() => {
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
    <aside class="sidebar"><button class="brand brand-button" @click="goHome" aria-label="返回我的任务"><span class="brand-icon">P<span>✳</span></span><strong>PeerGrade</strong></button><div class="side-label">工作空间</div><nav class="side-nav" aria-label="主导航"><button class="nav-item" :class="{selected:page==='home'}" :aria-current="page==='home' ? 'page' : undefined" :disabled="isBusy('nav')" @click="goHome"><span aria-hidden="true">▦</span> 我的任务</button></nav><div class="side-bottom"><div class="side-quote">“好的评价，<br>让想法继续生长。”</div><div class="profile"><span class="avatar">{{ user.display_name.slice(0,1) }}</span><span><strong>{{ user.display_name }}</strong><small>{{ teacher ? '教师账号' : '学生账号' }}</small></span><button @click="signOut" title="退出登录" aria-label="退出登录">↗</button></div></div></aside>
    <main class="main"><header class="topbar"><span>{{ teacher ? '教师工作空间' : '学生工作空间' }} <span class="divider">/</span> {{ pageLabel[page] || '任务详情' }}</span><button class="top-user" @click="signOut" title="退出登录" aria-label="退出登录">{{ user.display_name.slice(0,1) }}</button></header>
      <div class="content"><div v-if="notice.text" class="toast" :class="'toast-'+notice.type" :role="notice.type==='error' ? 'alert' : 'status'" aria-live="polite"><span>{{ notice.text }}</span><button @click="clearNotice()" aria-label="关闭提示">×</button></div>
        <template v-if="page==='home'"><div class="page-heading"><div><span class="eyebrow">YOUR WORKSPACE</span><h1>{{ teacher ? '教学工作台' : '我的学习任务' }}<span class="heading-dot">.</span></h1><p>你好，{{ user.display_name }}。{{ teacher ? '在这里组织互评，让反馈更有价值。' : '认真阅读，留下有依据的反馈。' }}</p></div><button v-if="teacher" class="button primary" :disabled="isBusy('nav')" @click="newAssignment">＋ 创建任务</button></div>
          <div class="overview"><div><span>课程任务</span><strong>{{ assignments.length }}</strong><small>已加入的任务</small></div><div><span>{{ teacher ? '进行中' : '待完成互评' }}</span><strong>{{ teacher ? assignments.filter(a=>a.phase!=='CLOSED').length : tasks.filter(t=>t.can_write).length }}</strong><small>{{ teacher ? '提交或互评阶段' : '需要你关注的评价' }}</small></div><div class="overview-message"><span class="spark">✳</span><div><strong>让每一条反馈都有来处</strong><small>选取原文，说明问题，再给出可执行的建议。</small></div></div></div>
          <section v-if="!teacher && tasks.length" class="section"><div class="section-title"><div><span class="eyebrow">REVIEW QUEUE</span><h2>我的互评 <span>{{ tasks.length }}</span></h2></div><p>阅读匿名作业，给同伴一些启发。</p></div><div class="task-list"><button v-for="t in tasks" :key="t.id" class="task-row" :disabled="isBusy('nav')" @click="openReview(t.id)"><div class="task-symbol" aria-hidden="true">✎</div><div class="task-main"><strong>{{ t.assignment_title }}</strong><small>匿名作业 · {{ t.anonymous_code }}</small></div><span class="pill" :class="statusClass(t.phase)">{{ statusText[t.phase] }}</span><span class="arrow" aria-hidden="true">↗</span></button></div></section>
          <section class="section"><div class="section-title"><div><span class="eyebrow">COURSE ASSIGNMENTS</span><h2>课程任务 <span>{{ assignments.length }}</span></h2></div><p>{{ teacher ? '创建和管理你的课程互评。' : '查看要求、提交作业和收到的反馈。' }}</p></div><div v-if="!assignments.length" class="empty"><div aria-hidden="true">◌</div><strong>这里还没有任务</strong><p>{{ teacher ? '创建一个任务，开启第一次互评。' : '课程教师添加任务后会显示在这里。' }}</p></div><div v-else class="cards"><button v-for="a in assignments" :key="a.id" class="assignment-card" :disabled="isBusy('nav')" @click="openAssignment(a.id)"><div class="card-top"><span class="card-icon" aria-hidden="true">✳</span><span class="pill" :class="statusClass(a.phase)">{{ assignmentStatus(a) }}</span></div><h3>{{ a.title }}</h3><p>{{ a.description }}</p><div class="card-bottom"><span>提交截止 {{ date(a.submit_deadline) }}</span><span aria-hidden="true">↗</span></div></button></div></section>
        </template>
        <template v-else-if="page==='create'"><button class="back" @click="goHome">← 返回任务列表</button><div class="page-heading compact"><div><span class="eyebrow">NEW ASSIGNMENT</span><h1>创建互评任务<span class="heading-dot">.</span></h1><p>清晰的要求，是有价值的反馈的起点。</p></div></div><form class="panel form-panel" @submit.prevent="createAssignment"><div class="form-section"><div><h3>基本信息</h3><p>告诉学生作业的目标和要求。</p></div><div class="fields"><label>任务名称<input v-model="creation.title" maxlength="200" required placeholder="例如：人工智能与教育案例分析"></label><label>作业描述 <small>{{ codePoints(creation.description) }} / 10000</small><textarea v-model="creation.description" rows="5" maxlength="10000" required placeholder="描述作业要求、范围和注意事项"></textarea></label><div class="two-fields"><label>提交截止<input v-model="creation.submit_deadline" type="datetime-local" required></label><label>互评截止<input v-model="creation.review_deadline" type="datetime-local" required></label></div><p v-if="deadlineErrors.length" class="field-error" role="alert">{{ deadlineErrors.join('；') }}</p><p v-else-if="creation.submit_deadline && creation.review_deadline" class="field-ok">✓ 时间安排有效，将按本机时区换算为 UTC 保存</p></div></div><div class="form-section"><div><h3>评分维度</h3><p>每项 0–5 分，互评开始后固定。</p></div><div class="fields"><label v-for="(_,i) in creation.rubric" :key="i">维度 {{ i+1 }}<input v-model="creation.rubric[i]" maxlength="100" required></label></div></div><div class="form-section"><div><h3>参与学生</h3><p>至少选择 3 名学生。</p></div><div class="student-grid"><div class="student-tools"><span>已选 {{ creation.student_ids.length }} / {{ students.length }} 人（至少 3 人）</span><button type="button" class="link-button" @click="creation.student_ids = students.map(s => s.id)">全选</button><button type="button" class="link-button" @click="creation.student_ids = []">清空</button></div><label v-for="s in students" :key="s.id" class="check"><input type="checkbox" v-model="creation.student_ids" :value="s.id"> {{ s.display_name }} <small>{{ s.username }}</small></label><p v-if="!students.length" class="subtle">尚无学生账号。学生注册后会出现在名单中。</p></div></div><div class="form-actions"><button type="button" class="button secondary" @click="goHome">取消</button><button class="button primary" :disabled="busy || creation.student_ids.length<3 || deadlineErrors.length" :aria-busy="isBusy('create')">{{ isBusy('create') ? '正在创建…' : '创建任务 →' }}</button></div></form></template>
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
                <button class="button secondary wide" :disabled="busy" @click="openDashboard(active.id)">查看教师看板</button>
              </template>
              <template v-else>
                <p v-if="active.phase === 'REVIEW_NOT_STARTED' || (active.phase === 'CLOSED' && !active.review_started)">本次未开展互评。</p>
                <p v-else-if="active.phase === 'CLOSED' && !active.my_submission">你未提交作业，本次没有个人反馈。</p>
                <p v-else-if="active.phase === 'SUBMISSION_ENDED' && active.my_submission">作业已交，等待教师开启互评。</p>
                <p v-else-if="active.my_submission && ['REVIEWING', 'READY_TO_PUBLISH'].includes(active.phase)">互评反馈将在教师发布后显示。</p>
                <p v-else-if="canSubmit">请在提交截止前完成作业。</p>
                <button v-if="active.phase === 'CLOSED' && active.my_submission" class="button primary wide" :disabled="isBusy('nav')" @click="openFeedback(active.id)">查看收到的反馈 →</button>
              </template>
            </aside>
          </div>
        </template>
        <template v-else-if="page==='review' && detail"><button class="back" @click="goHome">← 返回任务列表</button><div class="page-heading compact"><div><span class="eyebrow">EVIDENCE-BASED REVIEW</span><h1>认真阅读，具体反馈<span class="heading-dot">.</span></h1><p>{{ detail.assignment.title }}　·　匿名作业 {{ detail.anonymous_code }}</p></div><span class="pill" :class="statusClass(detail.phase)">{{ statusText[detail.phase] }}</span></div><div v-if="readOnly" class="readonly-banner" role="status"><span aria-hidden="true">✓</span> {{ detail.status==='COMPLETED' ? '该互评已提交，以下内容为只读存档，不能修改或重复提交。' : '互评阶段已结束，当前内容仅供查看，不能修改或提交。' }}</div><div class="review-layout"><section class="panel reading"><div class="reading-head"><div><span class="panel-kicker">01 / 阅读原文</span><h2>同伴的作业</h2></div><span class="anonymous">匿名 {{ detail.anonymous_code }}</span></div><div v-if="!readOnly" class="selection-tip" :class="{active: !!selectionInfo, limit: selected.length>=3}" aria-live="polite"><template v-if="selected.length>=3">已添加 3 处证据，达到上限；删除一条后可继续添加。</template><template v-else-if="selectionInfo">✳ 已选中 {{ selectionInfo.length }} 字，可添加为第 {{ selected.length+1 }} 处证据。</template><template v-else>✳　选中一段原文，再点击「添加证据」。</template></div><div class="source-text" ref="source" tabindex="0" role="region" aria-label="作业原文，可选中文字作为证据" @mouseup="readSelection" @keyup="readSelection" @touchend="readSelection"><template v-for="(part,i) in highlighted(detail.content_text,shownEvidence)" :key="part.start"><mark v-if="part.marked">{{ part.text }}</mark><span v-else>{{ part.text }}</span></template></div><button v-if="!readOnly" class="button secondary" type="button" :disabled="isBusy('nav') || !selectionInfo || selected.length>=3" @mousedown.prevent @click="addEvidence">{{ selected.length>=3 ? '已达 3 处证据上限' : '＋ 添加选中内容为证据' }}</button></section><section class="panel review-form"><span class="panel-kicker">02 / 撰写评价</span><h2>你的反馈</h2><p class="subtle">指出具体内容，并提供作者可以执行的建议。</p><form @submit.prevent="submitReview"><fieldset :disabled="readOnly || isBusy('submitReview')"><div class="score-list"><label v-for="(r,i) in detail.assignment.rubric" :key="i"><span>{{ r }} <small>/ 5 分</small></span><select v-model.number="shownReview.scores[i]" :aria-label="r + ' 评分'"><option v-for="n in 6" :key="n" :value="n-1">{{ n-1 }} 分</option></select></label></div><label>做得好的地方 <small>{{ codePoints(shownReview.strengths) }} / 3000</small><textarea v-model="shownReview.strengths" rows="3" maxlength="3000" required placeholder="指出值得保留的具体内容"></textarea></label><label>存在的问题 <small>{{ codePoints(shownReview.weaknesses) }} / 3000</small><textarea v-model="shownReview.weaknesses" rows="3" maxlength="3000" required placeholder="哪些地方还可以更清晰或完整？"></textarea></label><label>改进建议 <small>{{ codePoints(shownReview.suggestions) }} / 3000</small><textarea v-model="shownReview.suggestions" rows="3" maxlength="3000" required placeholder="给出可以执行的下一步"></textarea></label><div class="evidence-head"><strong>原文证据</strong><small class="evidence-count">{{ shownEvidence.length }} / 3</small></div><div v-if="!shownEvidence.length" class="evidence-empty">{{ readOnly ? '无原文证据。' : '从左侧选取原文作为证据。' }}</div><div v-for="(a,i) in shownEvidence" :key="i" class="evidence"><span>“{{ a.quote }}”</span><button v-if="!readOnly" type="button" @click="removeEvidence(i)" title="删除这条证据" aria-label="删除这条证据">×</button></div><div v-if="!readOnly" class="review-actions"><button type="button" class="button secondary" :disabled="isBusy('draft') || isBusy('submitReview')" :aria-busy="isBusy('draft')" @click="saveDraft">{{ isBusy('draft') ? '正在保存…' : '保存草稿' }}</button><button type="button" class="button secondary" :disabled="isBusy('ai') || isBusy('submitReview') || detail.ai_limit_reached" :aria-busy="isBusy('ai')" @click="checkAI">{{ isBusy('ai') ? '✳ 检测中…' : '✳ 检测反馈质量' }}</button><button class="button primary" :disabled="isBusy('submitReview')" :aria-busy="isBusy('submitReview')">{{ isBusy('submitReview') ? '正在提交…' : '提交评价 →' }}</button></div><p v-if="!readOnly && detail.ai_limit_reached" class="hint" role="status">本次互评的 AI 检测额度已用完，你仍可以保存草稿或提交评价。</p><p v-else-if="!readOnly" class="hint">AI 检测可选；检测会将作业摘录、所选证据和评价草稿发送至配置的模型服务。提示不计入作业分数，也不会修改评价。</p></fieldset></form>
          <details v-if="hasUnsentReview" class="unsent-draft">
            <summary>未提交的评价草稿</summary>
            <p class="inline-meta">{{ detail.assignment.rubric.map((label, i) => label + ' ' + review.scores[i] + '/5').join(' · ') }}</p>
            <template v-for="(label, key) in commentLabels" :key="key">
              <h3 v-if="review[key]" class="small-heading">{{ label }}</h3>
              <p v-if="review[key]" class="prose">{{ review[key] }}</p>
            </template>
            <div v-for="anchor in selected" :key="anchor.start + ':' + anchor.end" class="evidence">“{{ anchor.quote }}”</div>
          </details>
          <div v-if="quality && !readOnly" class="quality" role="status"><span class="eyebrow">QUALITY CHECK</span><h3>反馈质量提示 <span>{{ quality.quality_level }}</span></h3><p>具体性 {{ quality.specificity }}/2　依据性 {{ quality.evidence_grounding }}/2　可操作性 {{ quality.actionability }}/2</p><ul><li v-for="(issue,i) in quality.issues||[]" :key="'i'+i">{{ issue.message }}</li><li v-for="(s,i) in quality.suggestions||[]" :key="'s'+i">{{ s }}</li></ul></div></section></div></template>
        <template v-else-if="page==='feedback' && feedback">
          <button class="back" @click="openAssignment(active.id)">← 返回任务详情</button>
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
            </article>
          </div>
          <div v-if="!feedback.reviews.length" class="empty">{{ feedback.review_started ? '本次收到 0 份评价。' : '本次未开展互评，收到 0 份评价。' }}</div>
        </template>
        <template v-else-if="page==='dashboard' && dashboard"><button class="back" @click="openAssignment(dashboard.assignment.id)">← 返回任务详情</button><div class="page-heading compact"><div><span class="eyebrow">TEACHER DASHBOARD</span><h1>互评进展<span class="heading-dot">.</span></h1><p>{{ dashboard.assignment.title }} · 分歧提示仅供教师复核，不代表评价异常。</p></div></div><div class="overview dashboard-stats"><div><span>作业提交率</span><strong>{{ percent(dashboard.submission_count,dashboard.member_count) }}</strong><small>{{ dashboard.submission_count }} / {{ dashboard.member_count }} 人</small></div><div><span>互评完成率</span><strong>{{ percent(dashboard.review_count,dashboard.review_task_count) }}</strong><small>{{ dashboard.review_count }} / {{ dashboard.review_task_count }} 条</small></div><div><span>完整评分平均</span><strong>{{ dashboard.complete_average?.toFixed(1) ?? '—' }}</strong><small>满分 15 分</small></div><div><span>需关注的分歧</span><strong>{{ dashboard.high_disagreement_count }}</strong><small>两次评分相差 ≥ 5 分</small></div></div><section class="panel detail-panel">
            <h2>学生提交名单</h2>
            <ul class="submission-roster">
              <li v-for="student in dashboard.submission_roster" :key="student.id">
                <span><strong>{{ student.display_name }}</strong> <small>{{ student.username }}</small></span>
                <span class="pill" :class="student.submitted ? 'green' : 'muted'">{{ student.submitted ? '已交' : '未交' }}</span>
              </li>
            </ul>
          </section>
          <section class="panel detail-panel"><div class="panel-kicker">QUALITY INSIGHTS</div><h2>反馈质量检测</h2><p class="subtle">共发起 {{ dashboard.ai_call_count }} 次检测（成功 {{ dashboard.ai_success_count }} 次 / 失败 {{ dashboard.ai_failed_count }} 次 / 检测中 {{ dashboard.ai_running_count }} 次）· 低 {{ dashboard.ai_levels.LOW }} · 中 {{ dashboard.ai_levels.MEDIUM }} · 高 {{ dashboard.ai_levels.HIGH }}。只有与最终评价内容一致的检测结果计入等级。</p><p class="hint">存在 {{ dashboard.ai_paired_count }} 组评语发生变化的前后草稿检测（草稿差异检测，不构成实验配对数据）；平均变化 {{ dashboard.ai_paired_mean_change === null ? "—" : dashboard.ai_paired_mean_change.toFixed(1) }} 分。模型评分仅供参考，不能代替人工实验；正式效果验证须按 docs/experiment.md 组织人工盲评。</p><div class="tags"><span v-for="(count,key) in dashboard.ai_issue_counts" :key="key">{{ key }} · {{ count }}</span></div></section><section class="section"><div class="section-title"><div><span class="eyebrow">SUBMISSIONS</span><h2>作业与评价 <span>{{ dashboard.records.length }}</span></h2></div></div><div v-if="!dashboard.records.length" class="empty">尚无学生提交作业，暂无可查看的互评记录。</div><details v-for="record in dashboard.records" :key="record.submission_id" class="panel record"><summary><strong>{{ record.student_name }}</strong><span v-if="record.summary.high_disagreement" class="pill purple">高分歧，建议复核</span><span>{{ record.summary.incomplete ? '评价数量不足' : '平均 '+(record.summary.average_total ?? '—')+' / 15' }}</span></summary><div class="record-body"><h3>作业正文</h3><p class="prose">{{ record.content_text }}</p><div v-for="r in record.reviews" :key="r.id" class="record-review"><strong>评审：{{ r.reviewer_name }} · {{ statusText[r.phase] }}</strong><template v-if="r.review"><p>得分：{{ (r.review.scores||[]).join(' / ') }}（总分 {{ r.review.total_score }}）</p><p>优点：{{ r.review.strengths }}</p><p>问题：{{ r.review.weaknesses }}</p><p>建议：{{ r.review.suggestions }}</p><div v-for="a in (r.review.evidence_anchors||[])" :key="a.start" class="evidence">“{{ a.quote }}”</div></template><details v-for="(c,i) in r.ai_checks" :key="i" class="check-record"><summary>检测草稿 {{ i+1 }} · {{ date(c.created_at) }} · {{ c.status==='SUCCESS' ? (c.result?.quality_level ?? '—') : (aiStatusText[c.status] ?? '—') }}</summary><p>优点：{{ c.input_data?.review?.strengths ?? '—' }}</p><p>问题：{{ c.input_data?.review?.weaknesses ?? '—' }}</p><p>建议：{{ c.input_data?.review?.suggestions ?? '—' }}</p><p>证据：{{ joinList(c.input_data?.evidence_quotes) }}</p><p v-if="c.result">质量提示：{{ joinList(c.result?.suggestions) }}</p></details></div></div></details></section></template>
      </div>
    </main>
  </div>
</template>
