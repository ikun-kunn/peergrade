const assert = require('node:assert/strict')
const { chromium } = require(process.env.PEERGRADE_PLAYWRIGHT || 'playwright')

async function main() {
  const origin = new URL(process.env.PEERGRADE_LOCAL_ORIGIN)
  assert.equal(origin.hostname, '127.0.0.1', 'Use only a fresh synthetic preview instance.')
  assert.equal(origin.protocol, 'http:')
  const browser = await chromium.launch({ headless:true, executablePath:process.env.PEERGRADE_BROWSER })
  const initial = 'preview123456', changed = 'synthetic new preview password'
  async function login(page, username, password=initial) {
    await page.getByLabel('用户名').fill(username)
    await page.getByLabel('密码',{exact:true}).fill(password)
    await page.getByRole('button',{name:'登录 →',exact:true}).click()
  }
  async function open(username, viewport) {
    const context = await browser.newContext({viewport})
    const page = await context.newPage()
    await page.goto(origin.href)
    await login(page,username)
    await page.getByLabel('搜索任务').waitFor()
    return page
  }
  const click = (page,name) => page.getByRole('button',{name,exact:true}).click()
  async function change(page) {
    await click(page,'账号安全')
    await page.waitForURL(/#\/account$/)
    await page.getByLabel('当前密码',{exact:true}).fill(initial)
    await page.getByLabel('新密码',{exact:true}).fill(changed)
    await page.getByLabel('确认新密码',{exact:true}).fill(changed)
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),true)
    await click(page,'修改密码并重新登录')
    await page.getByText('密码已修改，所有登录会话已失效，请使用新密码重新登录').waitFor()
    await page.getByLabel('密码',{exact:true}).waitFor()
    assert.equal(await page.getByLabel('密码',{exact:true}).inputValue(),'')
  }
  try {
    const student = await open('preview_student1',{width:390,height:844})
    const secondDevice = await open('preview_student1',{width:1280,height:800})
    const teacher = await open('preview_teacher',{width:1280,height:800})
    await change(student)
    await secondDevice.reload()
    await secondDevice.getByRole('heading',{name:'登录 PeerGrade'}).waitFor()
    await login(student,'preview_student1',initial)
    await student.getByText('用户名或密码错误',{exact:true}).waitFor()
    await login(student,'preview_student1',changed)
    await student.getByLabel('搜索任务').waitFor()
    await student.locator('.assignment-card').filter({hasText:'案例分析：反馈演示'}).waitFor()
    await teacher.getByLabel('搜索任务').waitFor() // Different account was not revoked.
    await change(teacher)
    await login(teacher,'preview_teacher',changed)
    await teacher.getByRole('heading',{name:/教学工作台/}).waitFor()
    await teacher.locator('.assignment-card').filter({hasText:'案例分析：反馈演示'}).click()
    await click(teacher,'查看教师看板')
    await teacher.waitForURL(/\/dashboard$/)
    await teacher.getByRole('heading',{name:/互评进展/}).waitFor()
    console.log('PASS mobile student and desktop teacher password change, previous credential and other session revoked, teaching records preserved')
  } finally { await browser.close() }
}
main().catch(error=>{console.error(error);process.exitCode=1})
