import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { createRenderer, nextTick } from 'vue'
import { compileScript, parse } from 'vue/compiler-sfc'

const filename = new URL('../src/App.vue', import.meta.url)
const { descriptor, errors } = parse(await readFile(filename, 'utf8'), { filename: filename.pathname })
assert.deepEqual(errors, [])
const compiled = compileScript(descriptor, {
  id: 'peergrade-test',
  inlineTemplate: true,
  templateOptions: { compilerOptions: { hoistStatic: false } }
}).content.replace(/from (['"])vue\1/g, 'from ' + JSON.stringify(import.meta.resolve('vue')))
const { default: App } = await import('data:text/javascript;base64,' + Buffer.from(compiled).toString('base64'))

// Render the actual component into a small in-memory tree. Layout and scrolling
// coordinates still require a browser, while element wiring can run without one.
class Element {
  constructor(tag, value = '') {
    this.tag = tag
    this.value = ''
    this.content = value
    this.props = {}
    this.events = new Map()
    this.children = []
    this.parentNode = null
  }

  get tagName() { return this.tag.toUpperCase() }
  get options() { return this.children.filter(child => child.tag === 'option') }
  getRootNode() { return globalThis.document }
  addEventListener(name, handler) { this.events.set(name, handler) }
  focus() { globalThis.document.activeElement = this }
  scrollIntoView(options) { this.scroll = options }
}

function remove(node) {
  if (!node.parentNode) return
  const { children } = node.parentNode
  children.splice(children.indexOf(node), 1)
  node.parentNode = null
}

const renderer = createRenderer({
  createElement: tag => new Element(tag),
  createText: value => new Element('#text', value),
  createComment: () => new Element('#comment'),
  setText(node, value) { node.content = value },
  setElementText(node, value) { node.content = value; node.children.length = 0 },
  parentNode: node => node.parentNode,
  nextSibling(node) {
    const siblings = node.parentNode?.children
    return siblings?.[siblings.indexOf(node) + 1] ?? null
  },
  patchProp(node, key, _previous, value) {
    node.props[key] = value
    if (key === 'value') { node.value = value; node._value = value }
  },
  insert(node, parent, before = null) {
    remove(node)
    const position = before ? parent.children.indexOf(before) : parent.children.length
    assert.notEqual(position, -1)
    parent.children.splice(position, 0, node)
    node.parentNode = parent
  },
  remove
})

function descendants(node) {
  return [node, ...node.children.flatMap(descendants)]
}
function renderedText(node) {
  return descendants(node).map(el => el.content).join('')
}
function button(root, label) {
  const match = descendants(root).find(el => el.tag === 'button' && renderedText(el).includes(label))
  assert.ok(match, 'Missing button ' + label)
  return match
}
async function click(node) {
  assert.ok(!node.props.disabled)
  await node.props.onClick()
  await nextTick()
}

async function render(t, routes, role = 'STUDENT') {
  const root = new Element('root')
  const events = new Map()
  const responses = {
    '/api/auth/me': { id: 'test-user', role, display_name: '演示账号' },
    '/api/assignments': [],
    '/api/review-tasks': [],
    ...routes
  }
  const globals = {
    document: {
      hidden: false,
      activeElement: null,
      getElementById: id => descendants(root).find(el => el.props.id === id),
      addEventListener: (key, handler) => events.set(key, handler),
      removeEventListener: key => events.delete(key)
    },
    window: { scrollTo() {}, getSelection: () => null, confirm: () => true },
    Document: class {},
    ShadowRoot: class {},
    fetch: async (path, options) => {
      assert.ok(Object.hasOwn(responses, path), 'Unexpected request ' + path)
      const route = responses[path]
      const result = typeof route === 'function' ? await route(options) : route
      return result instanceof Response ? result : Response.json(result)
    }
  }
  const originals = Object.fromEntries(Object.keys(globals).map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  Object.assign(globalThis, globals)
  const app = renderer.createApp(App)
  t.after(() => {
    app.unmount()
    for (const [key, descriptor] of Object.entries(originals)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor)
      else delete globalThis[key]
    }
  })
  app.mount(root)
  await new Promise(resolve => setImmediate(resolve))
  return { root, refresh: events.get('visibilitychange'), document: globals.document }
}

const assignment = {
  id: 'assignment-1', title: '案例任务', description: '合成案例',
  rubric: ['完整性', '依据性', '可执行性'],
  submit_deadline: '2026-11-18T00:00:00Z', review_deadline: '2026-11-18T01:00:00Z'
}
const prefix = '😀 开头\n'
const quote = '同样的引文'
const secondPrefix = prefix + quote + '\n另一段 😀\n'
const content = secondPrefix + quote + '\n结尾'
const anchors = [prefix, secondPrefix].map(before => ({
  start: [...before].length,
  end: [...before].length + [...quote].length,
  quote,
  content_hash: 'fixture'
}))
const draft = {
  scores: [4, 4, 3], strengths: '已保存的草稿', weaknesses: '缺少对照',
  suggestions: '补充样本', evidence_anchors: [anchors[0]]
}

test('相同引文的两个反馈按钮分别聚焦正确的原文位置', async t => {
  const closed = { ...assignment, phase: 'CLOSED', review_started: true, has_submission: true }
  const { root, document } = await render(t, {
    '/api/assignments': [closed],
    '/api/assignments/assignment-1': { ...closed, my_submission: { content_type: 'TEXT', content_text: content } },
    '/api/assignments/assignment-1/my-feedback': {
      summary: { average_total: 11, dimension_averages: [4, 4, 3] },
      content_text: content,
      rubric: assignment.rubric,
      reviews: anchors.map(anchor => ({ ...draft, total_score: 11, evidence_anchors: [anchor] }))
    }
  })
  await click(button(root, assignment.title))
  await click(button(root, '查看收到的反馈'))
  const links = descendants(root).filter(el => el.tag === 'button' && el.props.class === 'link-button evidence-jump')
  assert.equal(links.length, 2)
  assert.notEqual(links[0].props['aria-label'], links[1].props['aria-label'])
  for (const [index, link] of links.entries()) {
    await click(link)
    assert.equal(document.activeElement.props.id, 'feedback-source-' + anchors[index].start)
    assert.equal(renderedText(document.activeElement), quote)
    assert.equal(document.activeElement.props.class, 'focused-evidence')
    assert.deepEqual(document.activeElement.scroll, { block: 'center' })
  }
  const marked = descendants(root).filter(el => el.tag === 'mark' && el.props.class === 'focused-evidence')
  assert.deepEqual(marked, [document.activeElement])
})

test('刷新待评任务保留本地编辑，收到正式评价后仍可查看未提交文字', async t => {
  const task = { id: 'review-1', assignment_title: assignment.title, can_write: true, phase: 'PENDING', anonymous_code: 'A1' }
  let detail = { ...task, assignment, content_text: content, draft, review: null, status: 'PENDING' }
  const { root, refresh } = await render(t, {
    '/api/review-tasks': [task],
    '/api/review-tasks/review-1': () => detail,
    '/api/review-tasks/review-1/draft': () => Response.json({ detail: '互评已提交或已结束，不能保存草稿' }, { status: 409 })
  })
  await click(button(root, assignment.title))
  const strengths = () => descendants(root).find(el => el.tag === 'textarea')
  assert.equal(strengths().value, draft.strengths)
  strengths().value = '本页正在写的评价'
  strengths().events.get('input')({ target: strengths() })
  await nextTick()

  detail = { ...detail, draft: { ...draft, strengths: '另一端保存的草稿' } }
  await refresh()
  assert.equal(strengths().value, '本页正在写的评价')

  detail = { ...detail, draft: null, review: { ...draft, strengths: '另一端已提交的评价' },
    can_write: false, status: 'COMPLETED', phase: 'COMPLETED' }
  await click(button(root, '保存草稿'))
  assert.match(renderedText(root), /不能保存草稿/)
  assert.equal(strengths().value, '另一端已提交的评价')
  assert.equal(descendants(root).find(el => el.tag === 'fieldset').props.disabled, true)
  const local = descendants(root).find(el => el.tag === 'details' && el.props.class === 'unsent-draft')
  assert.ok(local)
  assert.match(renderedText(local), /未提交的评价草稿.*本页正在写的评价/)

  await click(button(root, '返回任务列表'))
  await click(button(root, assignment.title))
  assert.doesNotMatch(renderedText(root), /未提交的评价草稿/)
})

test('草稿保存遇到网络失败时保留本地文字和证据，重试期间重复触发只发送一次', async t => {
  const task = { id: 'review-1', assignment_title: assignment.title, can_write: true, phase: 'PENDING', anonymous_code: 'A1' }
  const savedDraft = { ...draft, evidence_anchors: anchors }
  const detail = { ...task, assignment, content_text: content, draft: savedDraft, review: null, status: 'PENDING' }
  const writes = []
  let releaseRetry
  const { root } = await render(t, {
    '/api/review-tasks': [task],
    '/api/review-tasks/review-1': detail,
    '/api/review-tasks/review-1/draft': options => {
      assert.equal(options.method, 'POST')
      writes.push(JSON.parse(options.body))
      if (writes.length === 1) throw new TypeError('connection dropped')
      return new Promise(resolve => { releaseRetry = resolve })
    }
  })
  await click(button(root, assignment.title))
  const strengths = () => descendants(root).find(el => el.tag === 'textarea')
  await enter(strengths(), '网络失败时尚未保存的本地评价')
  const remove = descendants(root).filter(el => el.tag === 'button' && el.props['aria-label'] === '删除这条证据')
  assert.equal(remove.length, 2)
  await click(remove[0])
  const expected = { ...draft, strengths: '网络失败时尚未保存的本地评价', evidence_anchors: [anchors[1]] }
  const assertLocal = () => {
    assert.equal(strengths().value, expected.strengths)
    const count = descendants(root).find(el => el.props.class === 'evidence-count')
    assert.equal(renderedText(count), '1 / 3')
  }

  await click(button(root, '保存草稿'))
  assert.deepEqual(writes, [expected])
  assert.match(renderedText(root), /网络连接失败，请检查网络或服务器状态后重试/)
  assertLocal()
  assert.notEqual(button(root, '保存草稿').props.disabled, true)
  assert.notEqual(button(root, '保存草稿').props['aria-busy'], true)

  // Call the saved handler twice before the next render, as on rapid repeated activation.
  const saveHandler = button(root, '保存草稿').props.onClick
  const retry = saveHandler()
  const duplicate = saveHandler()
  await duplicate
  await nextTick()
  assert.equal(typeof releaseRetry, 'function')
  assert.deepEqual(writes, [expected, expected])
  const waiting = button(root, '正在保存…')
  assert.equal(waiting.props.disabled, true)
  assert.equal(waiting.props['aria-busy'], true)
  assertLocal()
  releaseRetry({ ok: true })
  await retry
  await nextTick()
  assert.match(renderedText(root), /草稿已保存，可以稍后继续/)
  assert.doesNotMatch(renderedText(root), /网络连接失败/)
  assert.notEqual(button(root, '保存草稿').props.disabled, true)
  assert.notEqual(button(root, '保存草稿').props['aria-busy'], true)
  assert.deepEqual(writes, [expected, expected])
  assertLocal()
})

test('第三次检测失败后刷新额度并禁用检测，保留本地草稿与提交入口', async t => {
  const task = { id: 'review-1', assignment_title: assignment.title, can_write: true, phase: 'PENDING', anonymous_code: 'A1' }
  let detail = { ...task, assignment, content_text: content, draft, review: null, status: 'PENDING',
    ai_limit_reached: false, ai_attempts_remaining: 1 }
  let calls = 0
  const { root } = await render(t, {
    '/api/review-tasks': [task],
    '/api/review-tasks/review-1': () => detail,
    '/api/review-tasks/review-1/ai-check': options => {
      assert.equal(options.method, 'POST')
      assert.equal(JSON.parse(options.body).strengths, '尚未保存的本页评价')
      calls++
      detail = { ...detail, ai_limit_reached: true, ai_attempts_remaining: 0 }
      return Response.json({ detail: 'AI 检测失败或超时，您仍可直接提交评价' }, { status: 503 })
    }
  })
  await click(button(root, assignment.title))
  assert.match(renderedText(root), /最多调用 3 次（含失败及超时）。剩余 1 次/)
  const strengths = () => descendants(root).find(el => el.tag === 'textarea')
  await enter(strengths(), '尚未保存的本页评价')
  await click(button(root, '检测反馈质量'))
  assert.equal(calls, 1)
  assert.match(renderedText(root), /AI 检测失败或超时/)
  assert.match(renderedText(root), /AI 检测额度已用完/)
  assert.equal(button(root, '检测反馈质量').props.disabled, true)
  assert.notEqual(button(root, '保存草稿').props.disabled, true)
  assert.notEqual(descendants(root).find(el => el.tag === 'fieldset').props.disabled, true)
  assert.ok(button(root, '提交评价'))
  assert.equal(strengths().value, '尚未保存的本页评价')
})

test('最后一份评价提交后，教师可在原页面看到发布入口', async t => {
  let detail = { ...assignment, phase: 'REVIEWING', available_actions: [], member_count: 3,
    submission_count: 3, minimum_submission_count: 3 }
  const { root, refresh } = await render(t, {
    '/api/assignments': [detail],
    '/api/assignments/assignment-1': () => detail
  }, 'TEACHER')
  await click(button(root, assignment.title))
  assert.doesNotMatch(renderedText(root), /发布反馈并结束任务/)
  detail = { ...detail, phase: 'READY_TO_PUBLISH', available_actions: ['close'] }
  await refresh()
  assert.ok(button(root, '发布反馈并结束任务'))
})

test('教师检测历史展示完整成功结果，不为失败、进行中和无效历史结果显示质量分', async t => {
  const detail = { ...assignment, phase: 'READY_TO_PUBLISH', available_actions: ['close'], member_count: 3,
    submission_count: 3, minimum_submission_count: 3 }
  const input_data = { review: draft, evidence_quotes: [quote] }
  const check = (status, result) => ({ status, result, input_data, created_at: '2026-11-18T00:30:00Z' })
  const valid = { specificity: 2, evidence_grounding: 1, actionability: 2, quality_level: 'HIGH',
    issues: [{ type: '证据不足', message: '请核对原文 <em>依据</em>' }], suggestions: ['补充具体原文行号'] }
  const stale = { ...valid, issues: [{ type: '旧问题', message: '旧检测结果' }], suggestions: ['不应展示的旧提示'] }
  const { root } = await render(t, {
    '/api/assignments': [detail],
    '/api/assignments/assignment-1': detail,
    '/api/assignments/assignment-1/dashboard': {
      assignment: detail, member_count: 3, submission_count: 3, review_count: 1, review_task_count: 6,
      complete_average: null, high_disagreement_count: 0, submission_roster: [], ai_call_count: 6,
      ai_success_count: 4, ai_failed_count: 1, ai_running_count: 1, ai_levels: { LOW: 1, MEDIUM: 0, HIGH: 1 },
      ai_paired_count: 0, ai_paired_mean_change: null, ai_issue_counts: {},
      records: [{ submission_id: 'work-1', student_name: '合成作者', content_text: content,
        summary: { incomplete: true, high_disagreement: false, average_total: null },
        reviews: [{ id: 'review-1', reviewer_name: '合成评审', phase: 'PENDING', review: null,
          ai_checks: [check('SUCCESS', valid), check('FAILED', stale), check('RUNNING', stale),
            check('SUCCESS', null), check('SUCCESS', { ...stale, specificity: 3 }),
            check('SUCCESS', { specificity: 0, evidence_grounding: 0, actionability: 0, quality_level: 'LOW', issues: [], suggestions: [] })]
        }]
      }]
    }
  }, 'TEACHER')
  await click(button(root, assignment.title))
  await click(button(root, '查看教师看板'))
  const histories = descendants(root).filter(el => el.tag === 'details' && el.props.class === 'check-record')
  assert.equal(histories.length, 6)
  const first = renderedText(histories[0])
  assert.match(first, /HIGH/)
  assert.match(first, /质量分：5 \/ 6（具体性 2\/2　依据性 1\/2　可操作性 2\/2）/)
  assert.match(first, /AI 检测问题：证据不足 · 请核对原文 <em>依据<\/em>/)
  assert.match(first, /质量提示：补充具体原文行号/)
  assert.ok(!descendants(histories[0]).some(el => el.tag === 'em' || Object.hasOwn(el.props, 'innerHTML')))
  for (const [index, status] of ['检测失败', '检测中', '结果不可用', '结果不可用'].entries()) {
    const history = renderedText(histories[index + 1])
    assert.ok(history.includes(status), history)
    assert.match(history, /优点：已保存的草稿/)
    assert.doesNotMatch(history, /质量分：|AI 检测问题：|质量提示：|不应展示的旧提示|HIGH/)
  }
  const zero = renderedText(histories[5])
  assert.match(zero, /LOW/)
  assert.match(zero, /质量分：0 \/ 6/)
  assert.match(zero, /AI 检测问题：无/)
  assert.match(zero, /质量提示：无/)
})

async function enter(node, value) {
  node.value = value
  node.events.get('input')({ target: node })
  await nextTick()
}

async function submitForm(root) {
  const form = descendants(root).find(el => el.tag === 'form')
  assert.ok(form)
  await form.props.onSubmit({ preventDefault() {} })
  await nextTick()
}

test('学生注册校验确认密码，成功后返回登录并保留用户名', async t => {
  let requests = 0
  const { root } = await render(t, {
    '/api/auth/me': Response.json({ detail: '未登录' }, { status: 401 }),
    '/api/auth/register': options => {
      requests++
      assert.equal(options.method, 'POST')
      assert.deepEqual(JSON.parse(options.body), { username: 'new_student', display_name: '新同学',
        password: 'sample password', password_confirmation: 'sample password' })
      return requests === 1 ? Response.json({ detail: '用户名已经存在' }, { status: 409 }) : { id: 'new', role: 'STUDENT' }
    }
  })
  await click(button(root, '注册学生账号'))
  const inputs = () => descendants(root).filter(el => el.tag === 'input')
  for (const [index, value] of ['new_student', '新同学', 'sample password', 'different password'].entries()) {
    await enter(inputs()[index], value)
  }
  await submitForm(root)
  assert.match(renderedText(root), /两次输入的密码不一致/)
  assert.equal(requests, 0)
  await enter(inputs()[3], 'sample password')
  await submitForm(root)
  assert.match(renderedText(root), /用户名已经存在/)
  assert.equal(inputs().length, 4)
  await submitForm(root)
  assert.equal(requests, 2)
  assert.match(renderedText(root), /学生账号已注册，请登录/)
  assert.equal(inputs().length, 2)
  assert.equal(inputs()[0].value, 'new_student')
  assert.equal(inputs()[1].value, '')
})

test('教师可以延期等待补交，以及结束未开启互评的过期任务', async t => {
  let detail = { ...assignment, phase: 'SUBMISSION_ENDED', available_actions: ['extend_deadlines'],
    member_count: 3, submission_count: 2, minimum_submission_count: 3, review_started: false }
  const { root, refresh } = await render(t, {
    '/api/assignments': () => [detail],
    '/api/assignments/assignment-1': () => detail,
    '/api/assignments/assignment-1/deadlines': options => {
      assert.equal(options.method, 'PATCH')
      const body = JSON.parse(options.body)
      assert.equal(body.submit_deadline, new Date('2026-11-19T20:00').toISOString())
      assert.equal(body.review_deadline, new Date('2026-11-20T20:00').toISOString())
      detail = { ...detail, ...body, phase: 'SUBMITTING' }
      return detail
    },
    '/api/assignments/assignment-1/close': () => {
      detail = { ...detail, phase: 'CLOSED', available_actions: [] }
      return { status: 'CLOSED' }
    }
  }, 'TEACHER')
  await click(button(root, assignment.title))
  assert.match(renderedText(root), /提交人数不足/)
  assert.doesNotMatch(renderedText(root), /开启匿名互评/)
  await click(button(root, '延长截止时间'))
  const fields = descendants(root).filter(el => el.tag === 'input')
  await enter(fields[0], '2026-11-19T20:00')
  await enter(fields[1], '2026-11-20T20:00')
  await submitForm(root)
  assert.match(renderedText(root), /截止时间已更新/)
  assert.ok(button(root, '延长截止时间'))
  detail = { ...detail, phase: 'REVIEW_NOT_STARTED', available_actions: ['close'] }
  await refresh()
  assert.doesNotMatch(renderedText(root), /延长截止时间/)
  await click(button(root, '结束任务'))
  assert.match(renderedText(root), /任务已结束，本次未开展互评/)
  assert.doesNotMatch(renderedText(root), /发布反馈并结束任务/)
})

test('已过期评价只保留历史入口，不再计入待完成数', async t => {
  const expiredTask = { id: 'expired', assignment_title: '过期任务', phase: 'REVIEW_EXPIRED', can_write: false, anonymous_code: 'A1' }
  const pendingTask = { id: 'pending', assignment_title: '进行中的任务', phase: 'PENDING', can_write: true, anonymous_code: 'A2' }
  const { root } = await render(t, { '/api/review-tasks': [expiredTask, pendingTask] })
  const overview = descendants(root).find(el => el.props.class === 'overview')
  assert.match(renderedText(overview), /待完成互评1需要你关注的评价/)
  assert.match(renderedText(button(root, '过期任务')), /评价已截止/)
  assert.match(renderedText(button(root, '进行中的任务')), /待评价/)
})

test('切换账号时丢弃旧账号仍在途的任务详情请求', async t => {
  let completeOldRequest, requestStarted = false, account = 'old'
  const old = { ...assignment, phase: 'SUBMITTING', has_submission: true }
  const newAssignment = { ...assignment, id: 'assignment-2', title: '新账号合法任务', phase: 'SUBMITTING', has_submission: false }
  const { root } = await render(t, {
    '/api/assignments': () => account === 'old' ? [old] : [newAssignment],
    '/api/assignments/assignment-1': () => {
      requestStarted = true
      return new Promise(resolve => { completeOldRequest = resolve })
    },
    '/api/auth/logout': () => { account = 'new'; return { ok: true } },
    '/api/auth/login': () => ({ id: 'new', role: 'STUDENT', display_name: '新账号' })
  })
  const oldNavigation = click(button(root, assignment.title))
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(requestStarted, true)
  const signOut = descendants(root).find(node => node.tag === 'button' && node.props['aria-label'] === '退出登录')
  assert.ok(signOut)
  await click(signOut)
  await submitForm(root)
  assert.match(renderedText(root), /新账号合法任务/)
  completeOldRequest({ ...old, my_submission: { content_type: 'TEXT', content_text: '上一账号的私有作业正文' } })
  await oldNavigation
  assert.match(renderedText(root), /新账号合法任务/)
  assert.doesNotMatch(renderedText(root), /上一账号的私有作业正文|案例任务/)
})

test('当前会话过期后清空已加载任务并返回登录', async t => {
  let expired = false
  const { root, refresh } = await render(t, {
    '/api/assignments': () => expired ? Response.json({ detail: '登录已过期' }, { status: 401 }) : [{ ...assignment, phase: 'SUBMITTING' }]
  })
  assert.match(renderedText(root), /案例任务/)
  expired = true
  await refresh()
  await nextTick()
  assert.match(renderedText(root), /登录 PeerGrade/)
  assert.match(renderedText(root), /登录已过期，请重新登录/)
  assert.doesNotMatch(renderedText(root), /案例任务/)
})
