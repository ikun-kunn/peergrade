// Real-browser timeout acceptance using a private loopback HTTP fixture and
// the built Vue application. It delays a single ordinary draft response body;
// no live backend, user data, or external service is contacted.
const assert = require('node:assert/strict')
const http = require('node:http')
const fs = require('node:fs')
const path = require('node:path')
const { chromium } = require(process.env.PEERGRADE_PLAYWRIGHT || 'playwright')

async function main() {
  const dist = path.resolve(__dirname, '../dist')
  const staticFiles = new Map([
    ['/', { file: path.join(dist, 'index.html'), type: 'text/html; charset=utf-8' }]
  ])
  for (const entry of fs.readdirSync(path.join(dist, 'assets'), { withFileTypes: true })) {
    if (!entry.isFile()) continue
    staticFiles.set('/assets/' + entry.name, { file: path.join(dist, 'assets', entry.name),
      type: entry.name.endsWith('.js') ? 'text/javascript' :
        entry.name.endsWith('.css') ? 'text/css' : 'application/octet-stream' })
  }
  const deadline = new Date(Date.now() + 3_600_000).toISOString()
  const assignment = { id: 'timeout-assignment', title: '网络恢复合成任务', description: '隔离浏览器测试',
    rubric: ['完整性', '依据性', '可操作性'], status: 'REVIEWING', phase: 'REVIEWING',
    submit_deadline: deadline, review_deadline: deadline }
  const task = { id: 'timeout-review', assignment_id: assignment.id, assignment_title: assignment.title,
    anonymous_code: 'FIXTURE', phase: 'PENDING', status: 'PENDING', can_write: true, review_deadline: deadline }
  const initial = { scores: [3,3,3], strengths: '已保存的合成评语', weaknesses: '缺少对照',
    suggestions: '补充样本', evidence_anchors: [] }
  let detail = { ...task, assignment, content_text: '仅用于网络恢复测试的合成原文。',
    content_hash: 'fixture', draft: initial, draft_version: 'v1', review: null }
  let writes = 0, interrupted = false, firstBodyStartedAt = 0, interruptedAt = 0
  const send = (res, body, status = 200) => {
    res.writeHead(status, {'Content-Type': 'application/json'})
    res.end(JSON.stringify(body))
  }
  const server = http.createServer(async (req, res) => {
    const name = new URL(req.url, 'http://127.0.0.1').pathname
    const asset = staticFiles.get(name)
    if (asset) {
      res.writeHead(200, {'Content-Type': asset.type})
      return res.end(fs.readFileSync(asset.file))
    }
    if (name === '/api/auth/me') return send(res, { id: 'fixture-student', role: 'STUDENT', display_name: '合成学生' })
    if (name === '/api/assignments') return send(res, [assignment])
    if (name === '/api/review-tasks') return send(res, [task])
    if (name === '/api/review-tasks/' + task.id) return send(res, detail)
    if (name === '/api/review-tasks/' + task.id + '/draft' && req.method === 'POST') {
      const parts = []
      for await (const chunk of req) parts.push(chunk)
      const body = JSON.parse(Buffer.concat(parts).toString('utf8'))
      writes++
      if (writes === 1) {
        // Headers and a partial JSON result arrive normally. The remainder of
        // this one response is unavailable until the browser cancels it.
        res.writeHead(200, {'Content-Type': 'application/json'})
        res.write('{"ok":')
        firstBodyStartedAt = Date.now()
        res.on('close', () => { interrupted = !res.writableEnded; interruptedAt = Date.now() })
        return
      }
      if (req.headers['x-draft-version'] !== 'v1') return send(res, {detail:'草稿已更新'}, 409)
      detail = { ...detail, draft: body, draft_version: 'v2' }
      return send(res, {ok:true, draft_version:'v2'})
    }
    send(res, {detail:'测试夹具未定义此路径'}, 404)
  })
  let browser
  try {
    await new Promise((resolve, reject) => {
      server.once('error', reject)
      server.listen(0, '127.0.0.1', resolve)
    })
    browser = await chromium.launch({headless:true, executablePath:process.env.PEERGRADE_BROWSER})
    const page = await browser.newPage({viewport:{width:1280,height:800}})
    let leaveWarnings = 0
    page.on('dialog', async dialog => {
      if (dialog.type() === 'confirm' && dialog.message().includes('尚未保存')) leaveWarnings++
      await dialog.dismiss()
    })
    await page.goto('http://127.0.0.1:' + server.address().port)
    await page.getByLabel('搜索任务').waitFor()
    await page.locator('.task-row').filter({hasText:assignment.title}).click()
    const field = page.getByPlaceholder('指出值得保留的具体内容')
    await field.fill('本次网络中断期间尚未保存的本地评语')
    await page.getByText('正在保存草稿…', {exact:true}).waitFor()
    await page.getByRole('button',{name:/返回任务列表/}).click()
    await page.getByText('草稿保存失败，本页内容仍保留，请重试', {exact:true}).waitFor({timeout:26_000})
    await page.waitForFunction(() => !document.querySelector('fieldset')?.disabled)
    assert.equal(await field.inputValue(), '本次网络中断期间尚未保存的本地评语')
    assert.equal(leaveWarnings, 1)
    assert.equal(writes, 1)
    assert.equal(interrupted, true)
    const elapsed = interruptedAt - firstBodyStartedAt
    assert.ok(elapsed >= 18_000 && elapsed <= 25_000, elapsed)
    await page.waitForTimeout(2_000)
    assert.equal(writes, 1, 'failed write must not arrange an endless background retry')
    await page.getByRole('button',{name:'保存草稿',exact:true}).click()
    await page.getByText('草稿已保存，可稍后继续', {exact:true}).waitFor()
    assert.equal(writes, 2)
    assert.equal(detail.draft.strengths,'本次网络中断期间尚未保存的本地评语')
    await page.getByRole('button',{name:/返回任务列表/}).click()
    await page.getByLabel('搜索任务').waitFor()
    console.log(JSON.stringify({response_body_interrupted:true, cancellation_ms:elapsed,
      draft_writes:writes, confirmed_local_draft_preserved:true, navigation_recovered:true}))
  } finally {
    if (browser) await browser.close()
    server.closeAllConnections()
    await new Promise(resolve => server.close(resolve))
  }
}
main().catch(error=>{console.error(error);process.exitCode=1})
