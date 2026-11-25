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
  setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end }
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

async function render(t, routes, role = 'STUDENT', hash = '') {
  const root = new Element('root')
  const events = new Map()
  const responses = {
    '/api/auth/me': { id: 'test-user', role, display_name: '演示账号' },
    '/api/assignments': [],
    '/api/review-tasks': [],
    ...routes
  }
  const location = { hash }
  const windowEvents = new Map()
  const globals = {
    document: {
      hidden: false,
      activeElement: null,
      getElementById: id => descendants(root).find(el => el.props.id === id),
      addEventListener: (key, handler) => events.set(key, handler),
      removeEventListener: key => events.delete(key)
    },
    window: { scrollTo() {}, getSelection: () => null, confirm: () => true, location,
      history: { pushState: (_state, _title, url) => { location.hash = url },
        replaceState: (_state, _title, url) => { location.hash = url } },
      addEventListener: (key, handler) => windowEvents.set(key, handler),
      removeEventListener: key => windowEvents.delete(key) },
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
  return { root, refresh: events.get('visibilitychange'), document: globals.document, location,
    changeHash: async hash => { location.hash = hash; await windowEvents.get('hashchange')?.(); await nextTick() },
    beforeUnload: event => windowEvents.get('beforeunload')?.(event) }
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

test('未写评语时禁用 AI 检测，写下至少一段后恢复', async t => {
  const task = { id: 'review-1', assignment_title: assignment.title, can_write: true, phase: 'PENDING', anonymous_code: 'A1' }
  const detail = { ...task, assignment, content_text: content, draft: null, review: null, status: 'PENDING',
    ai_limit_reached: false, ai_attempts_remaining: 3 }
  const { root } = await render(t, {
    '/api/review-tasks': [task],
    '/api/review-tasks/review-1': detail
  })
  await click(button(root, assignment.title))
  assert.equal(button(root, '检测反馈质量').props.disabled, true)
  assert.match(renderedText(root), /请先写下至少一段评价/)
  const strengths = () => descendants(root).find(el => el.tag === 'textarea')
  await enter(strengths(), '写得很好，继续加油。')
  assert.notEqual(button(root, '检测反馈质量').props.disabled, true)
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
    issues: [{ type: 'INSUFFICIENT_EVIDENCE', message: '请核对原文 <em>依据</em>' }], suggestions: ['补充具体原文行号'] }
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
            check('SUCCESS', null), check('SUCCESS', null),
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

test('班级内创建任务只预选已批准成员，提交携带班级编号', async t => {
  const classId = 'class-1'
  const members = [1, 2, 3, 4].map(number => ({ id: 'student-' + number, username: 's' + number,
    display_name: '合成学生' + number, status: number === 4 ? 'PENDING' : 'ACTIVE' }))
  let sent
  const { root, location } = await render(t, {
    '/api/classrooms': [{ id: classId, name: '软件工程班', invite_code: 'synthetic-code' }],
    '/api/classrooms/class-1': { id: classId, name: '软件工程班', invite_code: 'synthetic-code', members, assignments: [] },
    '/api/assignments': options => {
      if (options.method === 'POST') { sent = JSON.parse(options.body); return { id: 'new' } }
      return []
    }
  }, 'TEACHER')
  await click(button(root, '我的班级'))
  await click(button(root, '软件工程班'))
  assert.match(renderedText(root), /已批准 3 人 · 待批准 1 人/)
  await click(button(root, '从本班创建任务'))
  assert.equal(location.hash, '#/classrooms/class-1/create')
  assert.match(renderedText(root), /班级：软件工程班/)
  assert.match(renderedText(root), /已选 3 \/ 3 人/)
  assert.doesNotMatch(renderedText(root), /合成学生4/)
  const fields = descendants(root).filter(el => el.tag === 'input')
  await enter(fields[0], '班级作业')
  await enter(descendants(root).find(el => el.tag === 'textarea'), '合成要求')
  const deadline = hours => {
    const date = new Date(Date.now() + hours * 3600000)
    return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16)
  }
  await enter(fields[1], deadline(1)); await enter(fields[2], deadline(2))
  await submitForm(root)
  assert.equal(sent.classroom_id, classId)
  assert.deepEqual(sent.student_ids, ['student-1', 'student-2', 'student-3'])
})

test('学生加入班级后只显示个人申请状态，不出现教师管理入口', async t => {
  const info = { id: 'class-1', name: '软件工程班', membership_status: 'PENDING', assignments: [] }
  const { root } = await render(t, {
    '/api/classrooms': [],
    '/api/classrooms/join': options => { assert.deepEqual(JSON.parse(options.body), { invite_code: 'synthetic-code' }); return info },
    '/api/classrooms/class-1': info
  })
  await click(button(root, '我的班级'))
  const field = descendants(root).find(el => el.tag === 'input')
  await enter(field, 'synthetic-code')
  await submitForm(root)
  assert.match(renderedText(root), /等待教师批准/)
  assert.doesNotMatch(renderedText(root), /批量导入成员|生成新邀请码|从本班创建任务/)
})

test('已发布反馈可以申请复核，提交成功后回显申请且禁止重复', async t => {
  const { root } = await render(t, {
    '/api/assignments/assignment-1/my-feedback': { summary: { average_total: 11, dimension_averages: [4, 4, 3] },
      rubric: assignment.rubric, content_text: content, review_started: true,
      reviews: [{ ...draft, feedback_id: 'feedback-1', total_score: 11, review_case: null }] },
    '/api/feedback/feedback-1/appeal': options => {
      assert.deepEqual(JSON.parse(options.body), { reason: '请核实原文依据' })
      return { status: 'PENDING', student_reason: '请核实原文依据', resolution: null }
    }
  }, 'STUDENT', '#/assignments/assignment-1/feedback')
  await click(button(root, '申请复核'))
  await enter(descendants(root).find(el => el.tag === 'textarea'), '请核实原文依据')
  await submitForm(root)
  assert.match(renderedText(root), /待处理.*我的申请：请核实原文依据/)
  assert.doesNotMatch(renderedText(root), /申请复核|教师标记（仅教师可见）/)
  assert.match(renderedText(root), /匿名评审 1 · 11 分/)
})

test('AI 诊断定位 Unicode 评语和原文，未核实诊断显示不确定，编辑后失效', async t => {
  const task = { id: 'review-1', assignment_title: assignment.title, can_write: true, phase: 'PENDING', anonymous_code: 'A1' }
  const detail = { ...task, assignment, content_text: content, content_hash: 'fixture', draft: {...draft, strengths: '😀具体反馈'}, review: null, status: 'PENDING', ai_attempts_remaining: 3 }
  const diagnostic = { type:'VAGUE_FEEDBACK', field:'strengths', reason:'具体说明', guidance:'结合证据', review_location:{start:1,end:3,quote:'具体'}, source_location:anchors[1], location_status:'LOCATED' }
  const { root, document } = await render(t, {'/api/review-tasks': [task], '/api/review-tasks/review-1': detail,
    '/api/review-tasks/review-1/ai-check': { quality_level:'MEDIUM', specificity:1,evidence_grounding:1,actionability:1,issues:[],suggestions:[], diagnostics:[diagnostic,{...diagnostic,review_location:null,source_location:null,location_status:'UNCERTAIN'}] }})
  await click(button(root, assignment.title))
  await click(button(root, '检测反馈质量'))
  await click(button(root, '定位评语'))
  assert.equal(document.activeElement.props.id,'review-field-strengths')
  assert.equal(document.activeElement.selectionStart,2)
  assert.equal(document.activeElement.selectionEnd,4)
  await click(button(root, '定位原文'))
  assert.equal(document.activeElement.props.id,'review-source-'+anchors[1].start)
  assert.match(renderedText(root), /引文无法核实/)
  assert.equal(renderedText(descendants(root).find(el => el.props.class === 'evidence-count')), '1 / 3')
  await enter(descendants(root).find(el => el.props.id === 'review-field-strengths'), '编辑后的反馈')
  assert.doesNotMatch(renderedText(root), /定位评语|定位原文/)
})

test('学习档案区分零值与缺失，不接受学生查看他人的深链接', async t => {
  const profile = {display_name:'演示账号',scope:'SELF',assigned_reviews:2,completed_reviews:1,sample_count:1,
    dimension_means:{specificity:0,evidence_grounding:1,actionability:2},assignments:[{assignment_id:'assignment-1',title:'无检测任务',assigned_reviews:2,completed_reviews:1,sample_count:0,dimension_means:{specificity:null,evidence_grounding:null,actionability:null}}]}
  const { root, location, changeHash } = await render(t, {'/api/learning-profile':profile})
  await click(button(root,'学习档案'))
  assert.equal(location.hash,'#/learning')
  assert.match(renderedText(root), /完成互评1 \/ 2/)
  assert.match(renderedText(root), /具体性0\.0/)
  assert.match(renderedText(root), /具体性—/)
  await changeHash('#/learning/other-student')
  assert.equal(location.hash,'#/')
  assert.doesNotMatch(renderedText(root), /无检测任务/)
})

test('任务搜索、状态筛选和截止提醒仅显示匹配的未完成事项', async t => {
  const future = new Date(Date.now() + 3600000).toISOString()
  const { root } = await render(t, {
    '/api/assignments': [
      { ...assignment, title: '数据分析作业', phase: 'SUBMITTING', submit_deadline: future, has_submission: false },
      { ...assignment, id: 'closed', title: '旧课程', phase: 'CLOSED', submit_deadline: future, has_submission: true }
    ],
    '/api/review-tasks': [{ id: 'near', assignment_title: '数据分析作业', phase: 'PENDING', can_write: true, review_deadline: future, anonymous_code: 'A1' },
      { id: 'done', assignment_title: '旧课程', phase: 'COMPLETED', can_write: false, review_deadline: future, anonymous_code: 'A2' }]
  })
  assert.match(renderedText(root), /24 小时内截止：1 项待提交作业，1 项待完成互评/)
  const input = descendants(root).find(el => el.props['aria-label'] === '搜索任务')
  await enter(input, '数据分析')
  assert.match(renderedText(root), /数据分析作业/)
  assert.doesNotMatch(renderedText(root), /旧课程/)
  await click(button(root, '清空搜索'))
  const filter = descendants(root).find(el => el.props['aria-label'] === '课程任务状态筛选')
  for (const option of filter.options) option.selected = option.value === 'closed'
  filter.events.get('change')({ target: filter })
  await nextTick()
  const cards = descendants(root).filter(el => el.props.class === 'assignment-card')
  assert.equal(cards.length, 1)
  assert.match(renderedText(cards[0]), /旧课程/)
})

test('教师复制任务只预填名称、说明和量规，不复制截止时间或学生', async t => {
  const record = { ...assignment, phase: 'CLOSED', available_actions: [], submission_count: 3, member_count: 3, minimum_submission_count: 3 }
  const { root } = await render(t, {
    '/api/assignments': [record], '/api/assignments/assignment-1': record,
    '/api/students': [{ id: 'student1', username: 's1', display_name: '学生一' }]
  }, 'TEACHER')
  await click(button(root, assignment.title))
  await click(button(root, '复制为新任务'))
  const fields = descendants(root).filter(el => el.tag === 'input')
  assert.equal(fields[0].value, assignment.title + '（副本）')
  assert.equal(descendants(root).find(el => el.tag === 'textarea').value, assignment.description)
  assert.equal(fields[1].value, '')
  assert.equal(fields[2].value, '')
  assert.deepEqual(fields.slice(3, 6).map(el => el.value), assignment.rubric)
  assert.match(renderedText(root), /已选 0 \/ 1 人/)
  assert.equal(button(root, '创建任务 →').props.disabled, true)
})

test('停止编辑后自动保存最后版本，冲突时保留本页并停止后台覆盖', async t => {
  const task = { id: 'review-1', assignment_title: assignment.title, phase: 'PENDING', can_write: true, anonymous_code: 'A1' }
  let latest = { ...task, assignment, content_text: content, draft, draft_version: 'version1', review: null, status: 'PENDING' }
  const requests = []
  const { root } = await render(t, {
    '/api/review-tasks': [task], '/api/review-tasks/review-1': () => latest,
    '/api/review-tasks/review-1/draft': options => {
      const body = JSON.parse(options.body)
      requests.push({ body, version: options.headers['X-Draft-Version'] })
      if (options.headers['X-Draft-Version'] !== latest.draft_version) return Response.json({ detail: '草稿已在其他页面更新' }, { status: 409 })
      latest = { ...latest, draft: body, draft_version: 'version2' }
      return { ok: true, draft_version: latest.draft_version }
    }
  })
  await click(button(root, assignment.title))
  const field = () => descendants(root).find(el => el.tag === 'textarea')
  await enter(field(), '第一次编辑')
  await enter(field(), '自动保存最新编辑')
  assert.match(renderedText(root), /稍后自动保存/)
  assert.equal(requests.length, 0)
  await new Promise(resolve => setTimeout(resolve, 1650))
  await nextTick()
  assert.equal(requests.length, 1)
  assert.equal(requests[0].body.strengths, '自动保存最新编辑')
  assert.equal(requests[0].version, 'version1')
  assert.match(renderedText(root), /草稿已保存，可稍后继续/)
  latest = { ...latest, draft: { ...draft, strengths: '另一端的草稿' }, draft_version: 'version3' }
  await enter(field(), '本页更新')
  await new Promise(resolve => setTimeout(resolve, 1650))
  await nextTick()
  assert.equal(requests.length, 2)
  assert.equal(requests[1].version, 'version2')
  assert.equal(field().value, '本页更新')
  assert.match(renderedText(root), /其他页面已更新草稿/)
  await enter(field(), '本页继续编辑')
  assert.equal(button(root, '保存草稿').props.disabled, true)
  await click(button(root, '加载服务器草稿'))
  assert.equal(field().value, '另一端的草稿')
  assert.equal(requests.length, 2)
})

test('正式提交携带草稿版本号，防止旧页面覆盖新草稿', async t => {
  const task = { id: 'review-1', assignment_title: assignment.title, phase: 'PENDING', can_write: true, anonymous_code: 'A1' }
  const detail = { ...task, assignment, content_text: content, draft, draft_version: 'version1', review: null, status: 'PENDING' }
  let submitted = null
  const { root } = await render(t, {
    '/api/review-tasks': [task], '/api/review-tasks/review-1': () => detail,
    '/api/review-tasks/review-1/submit': options => {
      submitted = { version: options.headers['X-Draft-Version'], body: JSON.parse(options.body) }
      return { ...draft, total_score: 11, submitted_at: '2026-11-18T00:30:00Z' }
    }
  })
  await click(button(root, assignment.title))
  await submitForm(root)
  assert.equal(submitted.version, 'version1')
  assert.equal(submitted.body.strengths, draft.strengths)
})

test('正式提交遇到草稿版本冲突时刷新版本并由学生确认覆盖', async t => {
  const task = { id: 'review-1', assignment_title: assignment.title, phase: 'PENDING', can_write: true, anonymous_code: 'A1' }
  let latest = { ...task, assignment, content_text: content, draft, draft_version: 'version1', review: null, status: 'PENDING' }
  const attempts = []
  const { root } = await render(t, {
    '/api/review-tasks': [task], '/api/review-tasks/review-1': () => latest,
    '/api/review-tasks/review-1/submit': options => {
      const version = options.headers['X-Draft-Version']
      attempts.push(version)
      if (version !== latest.draft_version) return Response.json({ detail: '草稿版本冲突：其他页面已更新草稿，请确认后提交' }, { status: 409 })
      return { ...draft, total_score: 11, submitted_at: '2026-11-18T00:30:00Z' }
    }
  })
  await click(button(root, assignment.title))
  // 另一页面已把草稿推进到 version2，本页仍持 version1：首次提交被拒并进入冲突态。
  latest = { ...latest, draft: { ...draft, strengths: '另一页面的新草稿' }, draft_version: 'version2' }
  await submitForm(root)
  assert.deepEqual(attempts, ['version1'])
  assert.match(renderedText(root), /其他页面已更新草稿/)
  // 学生确认仍以本页内容提交：携带刷新后的 version2 重试成功。
  await submitForm(root)
  assert.deepEqual(attempts, ['version1', 'version2'])
})

test('添加证据拒绝超过后端上限的超长选区', async t => {
  const longContent = '证'.repeat(10001)
  const task = { id: 'review-1', assignment_title: assignment.title, phase: 'PENDING', can_write: true, anonymous_code: 'A1' }
  const detail = { ...task, assignment, content_text: longContent, content_hash: 'fixture', draft: null, draft_version: 'none', review: null, status: 'PENDING' }
  const { root } = await render(t, { '/api/review-tasks': [task], '/api/review-tasks/review-1': () => detail })
  await click(button(root, assignment.title))
  const sourceEl = descendants(root).find(el => el.props.class === 'source-text')
  sourceEl.contains = () => true
  const range = { startContainer: sourceEl, endContainer: sourceEl, startOffset: 0, endOffset: longContent.length,
    cloneRange: () => ({ selectNodeContents() {}, setEnd() {}, toString: () => '' }), toString: () => longContent }
  globalThis.window.getSelection = () => ({ rangeCount: 1, isCollapsed: false, getRangeAt: () => range, removeAllRanges() {} })
  await sourceEl.props.onMouseup()
  await nextTick()
  await click(button(root, '添加选中内容为证据'))
  assert.match(renderedText(root), /证据引用过长/)
  assert.match(renderedText(root), /0 \/ 3/)
})

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

test('账号修改密码校验确认并清理全部本地私有状态', async t => {
  let requests = 0
  const { root, location } = await render(t, {
    '/api/assignments': [{ ...assignment, phase:'SUBMITTING',has_submission:false }],
    '/api/auth/change-password': options => {
      requests++
      assert.equal(options.method,'POST')
      assert.deepEqual(JSON.parse(options.body), {current_password:'current sample password',new_password:' new sample password ',password_confirmation:' new sample password '})
      return requests===1 ? Response.json({detail:'当前密码错误'},{status:403}) : {ok:true,reauthenticate:true}
    }
  })
  await click(button(root,'账号安全'))
  assert.equal(location.hash,'#/account')
  const fields = () => descendants(root).filter(el => el.tag==='input' && el.props.type==='password')
  assert.equal(fields().length,3)
  await enter(fields()[0],'current sample password')
  await enter(fields()[1],' new sample password ')
  await enter(fields()[2],'wrong confirmation')
  await submitForm(root)
  assert.equal(requests,0)
  assert.match(renderedText(root),/两次输入的新密码不一致/)
  await enter(fields()[2],' new sample password ')
  await submitForm(root)
  assert.equal(requests,1)
  assert.match(renderedText(root),/当前密码错误/)
  assert.equal(fields().length,3)
  await submitForm(root)
  assert.equal(requests,2)
  assert.equal(location.hash,'#/')
  assert.match(renderedText(root),/密码已修改，所有登录会话已失效/)
  assert.doesNotMatch(renderedText(root),/案例任务|修改密码并重新登录/)
  assert.equal(fields().length,1)
  assert.equal(fields()[0].value,'')
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

test('直接打开互评链接恢复服务器草稿，浏览器返回前保存最新编辑', async t => {
  const task = { id: 'review-1', assignment_title: assignment.title, phase: 'PENDING', can_write: true, anonymous_code: 'A1' }
  let latest = { ...task, assignment, content_text: content, draft, draft_version: 'version1', review: null, status: 'PENDING' }
  const requests = []
  const { root, location, changeHash, beforeUnload } = await render(t, {
    '/api/review-tasks': [task], '/api/review-tasks/review-1': () => latest,
    '/api/review-tasks/review-1/draft': options => {
      assert.equal(options.headers['X-Draft-Version'], latest.draft_version)
      const body = JSON.parse(options.body)
      requests.push(body)
      latest = { ...latest, draft: body, draft_version: 'version' + (requests.length + 1) }
      return { ok: true, draft_version: latest.draft_version }
    }
  }, 'STUDENT', '#/review-tasks/review-1')
  const field = () => descendants(root).find(el => el.tag === 'textarea')
  assert.equal(field().value, draft.strengths)
  await enter(field(), '离开前刚刚修改的评价')
  let warned = false
  beforeUnload({ preventDefault() { warned = true } })
  assert.ok(warned)
  // Simulate back/forward without waiting for the normal debounce.
  await changeHash('#/')
  assert.equal(requests.length, 1)
  assert.equal(requests[0].strengths, '离开前刚刚修改的评价')
  assert.equal(location.hash, '#/')
  assert.ok(button(root, assignment.title))
  await changeHash('#/review-tasks/review-1')
  assert.equal(field().value, '离开前刚刚修改的评价')
  assert.match(renderedText(root), /草稿已保存，可稍后继续/)
})

test('离开互评时等待旧草稿写入再保存最新修改，避免旧请求覆盖新版本', async t => {
  const task = { id: 'review-1', assignment_title: assignment.title, phase: 'PENDING', can_write: true, anonymous_code: 'A1' }
  let latest = { ...task, assignment, content_text: content, draft, draft_version: 'v1', review: null, status: 'PENDING' }
  const requests = []
  let release
  const { root } = await render(t, {
    '/api/review-tasks': [task], '/api/review-tasks/review-1': () => latest,
    '/api/review-tasks/review-1/draft': async options => {
      assert.equal(options.headers['X-Draft-Version'], latest.draft_version)
      const body = JSON.parse(options.body)
      requests.push(body)
      if (requests.length === 1) await new Promise(resolve => { release = resolve })
      latest = { ...latest, draft: body, draft_version: 'v' + (requests.length + 1) }
      return { ok: true, draft_version: latest.draft_version }
    }
  }, 'STUDENT', '#/review-tasks/review-1')
  const field = () => descendants(root).find(el => el.tag === 'textarea')
  await enter(field(), '保存中的旧版本')
  const firstSave = click(button(root, '保存草稿'))
  await nextTick()
  await enter(field(), '本页最新版本')
  const navigation = click(button(root, '返回任务列表'))
  await nextTick()
  assert.equal(requests.length, 1)
  assert.ok(field())
  release()
  await Promise.all([firstSave, navigation])
  assert.equal(requests.length, 2)
  assert.equal(latest.draft.strengths, '本页最新版本')
  assert.ok(button(root, assignment.title))
})

test('待登录的任务深链接在登录后恢复，账号权限由目标接口校验', async t => {
  const detail = { ...assignment, phase: 'SUBMITTING', my_submission: { content_text: '服务器上的作业', content_type: 'TEXT' } }
  const { root, location } = await render(t, {
    '/api/auth/me': Response.json({ detail: '未登录' }, { status: 401 }),
    '/api/auth/login': { id: 'new', role: 'STUDENT', display_name: '学生' },
    '/api/assignments': [detail],
    '/api/assignments/assignment-1': detail
  }, 'STUDENT', '#/assignments/assignment-1')
  assert.match(renderedText(root), /登录 PeerGrade/)
  await submitForm(root)
  assert.match(renderedText(root), /服务器上的作业/)
  assert.equal(location.hash, '#/assignments/assignment-1')
})

test('学生直接打开教师看板链接被拒绝并回到工作台', async t => {
  const { root, location } = await render(t, {}, 'STUDENT', '#/assignments/assignment-1/dashboard')
  assert.match(renderedText(root), /页面不存在或当前账号无权访问/)
  assert.equal(location.hash, '#/')
  assert.match(renderedText(root), /我的学习任务/)
})

test('并发 401 清理工作区时退出登录仍锁定认证操作', async t => {
  let resolveRefresh, resolveLogout, refreshPending = false, loginCalls = 0
  const { root, refresh } = await render(t, {
    '/api/assignments': () => refreshPending ? new Promise(resolve => { resolveRefresh = resolve }) : [{ ...assignment, phase: 'SUBMITTING' }],
    '/api/auth/logout': () => new Promise(resolve => { resolveLogout = resolve }),
    '/api/auth/login': () => { loginCalls++; return { id: 'next', role: 'STUDENT', display_name: '新同学' } }
  })
  refreshPending = true
  const refreshRequest = refresh()
  await new Promise(resolve => setImmediate(resolve))
  const logoutRequest = click(descendants(root).find(el => el.tag === 'button' && el.props['aria-label'] === '退出登录'))
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(typeof resolveLogout, 'function')
  resolveRefresh(Response.json({ detail: '登录已过期' }, { status: 401 }))
  await refreshRequest
  await nextTick()
  assert.match(renderedText(root), /登录 PeerGrade/)
  assert.equal(button(root, '正在登录…').props.disabled, true)
  await submitForm(root)
  assert.equal(loginCalls, 0)
  refreshPending = false
  resolveLogout({ ok: true })
  await logoutRequest
  assert.equal(button(root, '登录 →').props.disabled, false)
  await submitForm(root)
  assert.equal(loginCalls, 1)
  assert.match(renderedText(root), /新同学/)
})

function stallUntilAbort(signal) {
  return new Promise((_, reject) => {
    if (signal.aborted) reject(signal.reason)
    else signal.addEventListener('abort', () => reject(signal.reason), { once: true })
  })
}
const settleIO = () => new Promise(resolve => setImmediate(resolve))

test('请求头未返回时 20 秒中止导航请求并恢复可重试状态', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const detail = { ...assignment, phase: 'SUBMITTING', my_submission: null }
  let calls = 0, signal
  const { root } = await render(t, {
    '/api/assignments': [detail],
    '/api/assignments/assignment-1': options => {
      if (++calls > 1) return detail
      signal = options.signal
      return stallUntilAbort(signal)
    }
  })
  const first = click(button(root, assignment.title))
  await nextTick()
  assert.equal(signal.aborted, false)
  assert.equal(button(root, assignment.title).props.disabled, true)
  t.mock.timers.tick(19_999)
  await nextTick()
  assert.equal(signal.aborted, false)
  t.mock.timers.tick(1)
  await first
  assert.equal(signal.aborted, true)
  assert.match(renderedText(root), /请求超时（20 秒）/)
  assert.equal(button(root, assignment.title).props.disabled, false)
  await click(button(root, assignment.title))
  assert.match(renderedText(root), /任务说明/)
  assert.equal(calls, 2)
})

test('响应头已返回但 JSON 响应体未完成时仍受同一 20 秒期限约束', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const detail = { ...assignment, phase: 'SUBMITTING', my_submission: null }
  let signal
  const { root } = await render(t, {
    '/api/assignments': [detail],
    '/api/assignments/assignment-1': options => {
      signal = options.signal
      return new Response(new ReadableStream({
        start(controller) {
          signal.addEventListener('abort', () => controller.error(signal.reason), { once: true })
        }
      }), { headers: { 'Content-Type': 'application/json' } })
    }
  })
  const first = click(button(root, assignment.title))
  await settleIO()
  t.mock.timers.tick(20_000)
  await first
  assert.equal(signal.aborted, true)
  assert.match(renderedText(root), /请求超时（20 秒）/)
  assert.equal(button(root, assignment.title).props.disabled, false)
  assert.doesNotMatch(renderedText(root), /任务说明/)
})

test('AI 请求尚未完成不阻塞草稿、状态刷新、导航和退出', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const task = { id: 'review-1', assignment_title: assignment.title, phase: 'PENDING', can_write: true, anonymous_code: 'A1' }
  let latest = { ...task, assignment, content_text: content, draft, draft_version: 'v1', review: null, status: 'PENDING' }
  let aiSignal, draftCalls = 0, detailCalls = 0, logouts = 0
  const { root, refresh } = await render(t, {
    '/api/review-tasks': [task],
    '/api/review-tasks/review-1': () => { detailCalls++; return latest },
    '/api/review-tasks/review-1/ai-check': options => {
      aiSignal = options.signal
      return stallUntilAbort(aiSignal)
    },
    '/api/review-tasks/review-1/draft': options => {
      draftCalls++
      assert.equal(options.headers['X-Draft-Version'], 'v1')
      latest = { ...latest, draft: JSON.parse(options.body), draft_version: 'v2' }
      return { ok: true, draft_version: 'v2' }
    },
    '/api/auth/logout': () => { logouts++; return { ok: true } }
  }, 'STUDENT', '#/review-tasks/review-1')
  const detection = click(button(root, '检测反馈质量'))
  await nextTick()
  assert.equal(aiSignal.aborted, false)
  const field = () => descendants(root).find(el => el.props.id === 'review-field-strengths')
  await enter(field(), '检测尚未结束时编辑的草稿')
  t.mock.timers.tick(1_500)
  await settleIO()
  await nextTick()
  assert.equal(draftCalls, 1)
  assert.equal(latest.draft.strengths, '检测尚未结束时编辑的草稿')
  assert.match(renderedText(root), /草稿已保存，可稍后继续/)
  const beforeRefresh = detailCalls
  await refresh()
  assert.ok(detailCalls > beforeRefresh, 'unrelated AI request must not suspend status refresh')
  await click(button(root, '返回任务列表'))
  assert.ok(button(root, assignment.title))
  const logout = descendants(root).find(el => el.tag === 'button' && el.props['aria-label'] === '退出登录')
  await click(logout)
  assert.equal(logouts, 1)
  assert.match(renderedText(root), /登录 PeerGrade/)
  t.mock.timers.tick(18_500)
  await detection
  assert.equal(aiSignal.aborted, true)
  assert.match(renderedText(root), /登录 PeerGrade/)
  assert.doesNotMatch(renderedText(root), /评价内容在检测期间被修改/)
})

test('草稿请求超时保留本页且停止后台重排，取消离开后可以手动保存', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const task = { id: 'review-1', assignment_title: assignment.title, phase: 'PENDING', can_write: true, anonymous_code: 'A1' }
  let latest = { ...task, assignment, content_text: content, draft, draft_version: 'v1', review: null, status: 'PENDING' }
  let draftCalls = 0, signal
  const { root, location, beforeUnload } = await render(t, {
    '/api/review-tasks': [task],
    '/api/review-tasks/review-1': () => latest,
    '/api/review-tasks/review-1/draft': options => {
      draftCalls++
      if (draftCalls === 1) { signal = options.signal; return stallUntilAbort(signal) }
      assert.equal(options.headers['X-Draft-Version'], 'v1')
      latest = { ...latest, draft: JSON.parse(options.body), draft_version: 'v2' }
      return { ok: true, draft_version: 'v2' }
    }
  }, 'STUDENT', '#/review-tasks/review-1')
  const field = () => descendants(root).find(el => el.props.id === 'review-field-strengths')
  await enter(field(), '暂未收到服务端确认的本页草稿')
  t.mock.timers.tick(1_500)
  await settleIO()
  assert.equal(draftCalls, 1)
  t.mock.timers.tick(20_000)
  await settleIO()
  await nextTick()
  assert.equal(signal.aborted, true)
  assert.match(renderedText(root), /草稿保存失败，本页内容仍保留/)
  assert.equal(field().value, '暂未收到服务端确认的本页草稿')
  t.mock.timers.tick(60_000)
  await settleIO()
  assert.equal(draftCalls, 1)
  let warned = false, confirmed = 0
  beforeUnload({ preventDefault() { warned = true } })
  assert.equal(warned, true)
  window.confirm = () => { confirmed++; return false }
  await click(button(root, '返回任务列表'))
  assert.equal(confirmed, 1)
  assert.equal(location.hash, '#/review-tasks/review-1')
  assert.equal(draftCalls, 1)
  assert.equal(field().value, '暂未收到服务端确认的本页草稿')
  await click(button(root, '保存草稿'))
  assert.equal(draftCalls, 2)
  assert.equal(latest.draft.strengths, '暂未收到服务端确认的本页草稿')
  assert.match(renderedText(root), /草稿已保存，可稍后继续/)
})

test('退出请求超时清理本地工作区并提示服务端状态未确认', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  let signal
  const { root } = await render(t, {
    '/api/auth/logout': options => { signal = options.signal; return stallUntilAbort(signal) },
    '/api/assignments': [{ ...assignment, phase: 'SUBMITTING' }]
  })
  const logout = descendants(root).find(el => el.tag === 'button' && el.props['aria-label'] === '退出登录')
  const request = click(logout)
  await settleIO()
  assert.equal(logout.props.disabled, true)
  t.mock.timers.tick(20_000)
  await request
  assert.equal(signal.aborted, true)
  assert.match(renderedText(root), /登录 PeerGrade/)
  assert.match(renderedText(root), /服务端尚未确认退出/)
  assert.doesNotMatch(renderedText(root), /案例任务/)
  assert.equal(button(root, '登录 →').props.disabled, false)
})
