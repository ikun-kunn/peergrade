const assert = require('node:assert/strict')
const { chromium } = require(process.env.PEERGRADE_PLAYWRIGHT || 'playwright')

async function main() {
  const origin = new URL(process.env.PEERGRADE_LOCAL_ORIGIN)
  assert.equal(origin.hostname, '127.0.0.1')
  assert.equal(origin.protocol, 'https:')
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PEERGRADE_BROWSER })
  const password = 'local proxy synthetic password'
  async function login(username, viewport) {
    const context = await browser.newContext({ viewport, ignoreHTTPSErrors: true })
    const page = await context.newPage()
    await page.goto(origin.href)
    await page.getByLabel('用户名').fill(username)
    await page.getByLabel('密码').fill(password)
    await page.getByRole('button', { name: '登录 →' }).click()
    await page.locator('.assignment-card').filter({ hasText: '生产模拟：课程互评' }).click()
    return page
  }
  try {
    const student = await login('proxy_student_0', { width: 390, height: 844 })
    await student.getByRole('button', { name: '查看收到的反馈' }).click()
    await student.getByRole('heading', { name: '匿名评审 1 · 12 分' }).waitFor()
    await student.getByRole('heading', { name: '匿名评审 2 · 12 分' }).waitFor()
    await student.getByRole('button', { name: /跳到匿名评审 1 的第 1 处原文证据/ }).click()
    assert.equal(await student.locator('mark.focused-evidence').textContent(), '原文证据：有效学习需要及时反馈。')
    assert.equal(await student.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
    const teacher = await login('proxy_teacher', { width: 1280, height: 800 })
    await teacher.getByRole('button', { name: '查看教师看板' }).click()
    await teacher.getByText('6 / 6 条', { exact: true }).waitFor()
    await teacher.getByText('3 / 3 人', { exact: true }).waitFor()
    assert.equal(await teacher.locator('.submission-roster li').count(), 3)
    console.log('PASS: real browser desktop dashboard and mobile published feedback through local Caddy')
  } finally {
    await browser.close()
  }
}

main().catch(error => { console.error(error); process.exitCode = 1 })
