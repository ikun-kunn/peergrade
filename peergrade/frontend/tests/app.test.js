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
    window: { scrollTo() {}, getSelection: () => null },
    Document: class {},
    ShadowRoot: class {},
    fetch: async path => {
      assert.ok(Object.hasOwn(responses, path), 'Unexpected request ' + path)
      const route = responses[path]
      const result = typeof route === 'function' ? route() : route
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
