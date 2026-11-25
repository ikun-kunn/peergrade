// Real UI only; the Python fixture helper advances one synthetic submission deadline.
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const { execFileSync } = require('node:child_process')
const { chromium } = require(process.env.PEERGRADE_PLAYWRIGHT || 'playwright')

async function main() {
  const origin = new URL(process.env.PEERGRADE_LOCAL_ORIGIN)
  assert.equal(origin.hostname,'127.0.0.1')
  assert.ok(['http:','https:'].includes(origin.protocol))
  const token = process.env.PEERGRADE_BROWSER_FIXTURE_TOKEN
  assert.match(token || '',/^[0-9a-f]{16}$/)
  const title = '全流程合成验收-'+token
  const className = '合成验收班级-'+token
  const teacherName = 'browser_t_'+token
  const names = [1,2,3].map(i=>'browser_s'+i+'_'+token)
  const password = 'synthetic browser fixture password'
  const quote = 'Evidence: practice results support feedback.'
  const browser = await chromium.launch({headless:true,executablePath:process.env.PEERGRADE_BROWSER})
  const desktop = {width:1280,height:800}, mobile = {width:390,height:844}
  const click = (page,name) => page.getByRole('button',{name,exact:true}).click()
  const card = page => page.locator('.assignment-card').filter({hasText:title})
  const rows = page => page.locator('.task-row').filter({hasText:title})
  const widthOK = async page => assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true)
  async function home(page, reload=false) {
    await click(page,'我的任务')
    await page.waitForURL(url=>url.hash==='#/' || url.hash==='')
    await page.getByLabel('搜索任务').waitFor()
    if (reload) { await page.reload(); await page.getByLabel('搜索任务').waitFor() }
  }
  const deadline = hours => {
    const time = new Date(Date.now()+hours*3600000)
    return new Date(time.getTime()-time.getTimezoneOffset()*60000).toISOString().slice(0,16)
  }
  async function open(viewport) {
    const context = await browser.newContext({viewport,acceptDownloads:true,ignoreHTTPSErrors:origin.protocol==='https:'})
    const page = await context.newPage()
    await page.goto(origin.href)
    await page.getByLabel('用户名',{exact:true}).waitFor()
    return page
  }
  async function login(page,name) {
    await page.getByLabel('用户名',{exact:true}).fill(name)
    await page.getByLabel('密码',{exact:true}).fill(password)
    await click(page,'登录 →')
    await page.getByLabel('搜索任务').waitFor()
  }
  async function register(page,name,index) {
    await click(page,'注册学生账号')
    await page.getByLabel('用户名',{exact:true}).fill(name)
    await page.getByLabel('显示名称',{exact:true}).fill('合成验收学生'+index)
    await page.getByLabel('密码',{exact:true}).fill(password)
    await page.getByLabel('确认密码',{exact:true}).fill(password)
    await click(page,'注册')
    await page.getByText('学生账号已注册，请登录').waitFor()
    await login(page,name)
  }
  async function selectEvidence(page) {
    await page.locator('.source-text').evaluate((root,needle)=>{
      const walker = document.createTreeWalker(root,NodeFilter.SHOW_TEXT)
      let node
      while ((node=walker.nextNode())) {
        const start = node.textContent.indexOf(needle)
        if (start<0) continue
        const range=document.createRange()
        range.setStart(node,start);range.setEnd(node,start+needle.length)
        const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range)
        root.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}))
        return
      }
      throw new Error('Source quote not found in rendered text')
    },quote)
    await click(page,'＋ 添加选中内容为证据')
    await page.getByText('“'+quote+'”',{exact:true}).waitFor()
  }
  let phase='startup'
  try {
    const teacher = await open(desktop)
    await login(teacher,teacherName)
    const students=[]
    phase='register students'
    for (let i=0;i<3;i++) {
      const page = await open(i===1?desktop:mobile)
      await register(page,names[i],i+1)
      students.push(page)
    }
    phase='class enrollment'
    await click(teacher,'我的班级')
    await teacher.getByLabel('班级名称').fill(className)
    await click(teacher,'创建班级')
    await teacher.getByRole('heading',{name:new RegExp(className)}).waitFor()
    const invite = await teacher.locator('.invite-code').innerText()
    await click(students[0],'我的班级')
    await students[0].getByLabel('班级邀请码').fill(invite)
    await click(students[0],'申请加入')
    await students[0].getByText('加入申请已提交，等待教师批准。').waitFor()
    await click(teacher,'刷新班级')
    await teacher.locator('.class-member').filter({hasText:'合成验收学生1'}).getByRole('button',{name:'批准',exact:true}).click()
    await teacher.getByLabel('学生用户名').fill(names.slice(1).join('\n'))
    await click(teacher,'批量导入')
    await teacher.getByText('已批准 3 人 · 待批准 0 人').waitFor()
    phase='create assignment'
    await click(teacher,'从本班创建任务')
    await teacher.getByLabel('任务名称').fill(title)
    await teacher.getByLabel('作业描述').fill('比较合成学习数据，说明证据及改进方向。')
    await teacher.getByLabel('提交截止').fill(deadline(1))
    await teacher.getByLabel('互评截止').fill(deadline(2))
    await teacher.getByText('已选 3 / 3 人（至少 3 人）').waitFor()
    await click(teacher,'创建任务 →')
    await card(teacher).click()
    await teacher.waitForURL(/#\/assignments\/[^/]+$/)
    const assignUrl=teacher.url(), aid=decodeURIComponent(new URL(assignUrl).hash.split('/').at(-1))
    phase='three submissions'
    const originalSources=[]
    for (let i=0;i<3;i++) {
      const page=students[i]
      await home(page,true)
      await card(page).click()
      if (i<2) {
        const text='合成案例 '+(i===0?'甲':'乙')+' 😀\n'+quote+'\n补充样本人数和比较条件。'
        await page.getByPlaceholder('在这里输入你的作业正文…').fill(text)
        await click(page,'保存文字作业')
      } else {
        await page.getByLabel('选择 PDF 文件').setInputFiles(process.env.PEERGRADE_BROWSER_FIXTURE_PDF)
        await click(page,'提取并提交')
      }
      await page.getByText(/当前已交/).waitFor()
      const original=await page.getByRole('region',{name:'当前已提交的作业正文'}).innerText()
      assert.ok(original.includes(quote))
      originalSources.push(original)
      await widthOK(page)
    }
    phase='deadline and allocation'
    await teacher.reload()
    await teacher.getByText(/有效提交 3 \/ 3 人/).waitFor()
    assert.equal(await teacher.getByRole('button',{name:'开启匿名互评 →',exact:true}).count(),0)
    execFileSync(process.env.PEERGRADE_BROWSER_PYTHON,['-m','tests.browser_fixture',aid],{stdio:'pipe',env:process.env})
    await teacher.reload()
    await click(teacher,'开启匿名互评 →')
    await teacher.getByText('已生成 6 条匿名互评任务').waitFor()
    phase='six anonymous reviews'
    const strengths='指出了原文中证据与反馈的联系。'
    const scores=[4,3,1]
    for (let i=0;i<students.length;i++) {
      const page=students[i]
      phase=`anonymous review queue for synthetic student ${i+1}`
      await home(page,true)
      await rows(page).first().waitFor()
      assert.equal(await rows(page).count(),2)
      for (let j=0;j<2;j++) {
        phase=`synthetic student ${i+1}, review ${j+1}`
        await rows(page).filter({hasText:'待评价'}).first().click()
        await page.waitForURL(/#\/review-tasks\/[^/]+$/)
        const source=await page.locator('.source-text').innerText()
        assert.notEqual(source,originalSources[i])
        assert.ok(originalSources.some((value,k)=>k!==i && source===value))
        assert.ok(names.every(name=>!source.includes(name)))
        await selectEvidence(page)
        await page.getByPlaceholder('指出值得保留的具体内容').fill(strengths)
        await page.getByPlaceholder('哪些地方还可以更清晰或完整？').fill('缺少样本人数和对照组。')
        await page.getByPlaceholder('给出可以执行的下一步').fill('补充样本人数与前后比较数据。')
        for (const select of await page.locator('.score-list select').all()) await select.selectOption(String(scores[i]))
        await page.locator('.draft-status').getByText('草稿已保存，可稍后继续',{exact:true}).waitFor()
        await page.reload()
        assert.equal(await page.getByPlaceholder('指出值得保留的具体内容').inputValue(),strengths)
        await page.getByText('“'+quote+'”',{exact:true}).waitFor()
        if (j===0) {
          await page.getByRole('button',{name:/检测反馈质量/}).click()
          await page.getByText(/AI 检测暂不可用/).waitFor()
          assert.equal(await page.getByPlaceholder('指出值得保留的具体内容').inputValue(),strengths)
        }
        await widthOK(page)
        await click(page,'提交评价 →')
        await page.getByLabel('搜索任务').waitFor()
      }
      assert.equal(await rows(page).filter({hasText:'已评价'}).count(),2)
      await rows(page).first().click()
      await page.locator('.readonly-banner').waitFor()
      assert.match(await page.locator('.readonly-banner').innerText(),/该互评已提交/)
      assert.equal(await page.getByRole('button',{name:'提交评价 →',exact:true}).count(),0)
      await home(page)
    }
    phase='publish feedback on mobile teacher view'
    await teacher.setViewportSize(mobile)
    await teacher.goto(assignUrl)
    await teacher.reload()
    await click(teacher,'发布反馈并结束任务')
    await teacher.getByText('任务已结束，学生可以查看反馈').waitFor()
    await widthOK(teacher)
    phase='published feedback and evidence jumps'
    const feedbackUrls=[]
    for (let i=0;i<3;i++) {
      const page=students[i]
      await page.reload()
      await card(page).click()
      await click(page,'查看收到的反馈 →')
      await page.waitForURL(/\/feedback$/)
      feedbackUrls.push(page.url())
      await page.getByRole('heading',{name:/匿名评审 2 · \d+ 分/}).waitFor()
      assert.equal(await page.locator('.feedback-grid article').count(),2)
      const actual = (await page.locator('.feedback-grid article h2').allTextContents()).map(text=>Number(text.match(/· (\d+) 分/)[1])).sort((a,b)=>a-b)
      assert.deepEqual(actual,scores.filter((_,k)=>k!==i).map(s=>s*3).sort((a,b)=>a-b))
      await page.getByRole('button',{name:/跳到匿名评审 1 的第 1 处原文证据/}).click()
      assert.equal(await page.locator('mark.focused-evidence').textContent(),quote)
      assert.equal(await page.locator('.source-text').innerText(),originalSources[i])
    }
    phase='adoption, revision, appeal'
    const author=students[0]
    await author.getByRole('button',{name:'记录采纳决定'}).first().click()
    await author.getByLabel('采纳理由').fill('补充前后数据。')
    await click(author,'保存采纳记录')
    await author.getByText('补充前后数据。',{exact:true}).waitFor()
    await click(author,'开始修订')
    await author.getByLabel('修订正文').fill(originalSources[0]+'\n增加合成对照数据。')
    await author.getByLabel('修订说明').fill('补充数据说明')
    await click(author,'保存新修订版本')
    await author.getByText('1 / 5 版').waitFor()
    await author.getByRole('button',{name:'申请复核'}).first().click()
    await author.getByLabel('申请理由').fill('请教师复核高分歧。')
    await click(author,'提交复核申请')
    await author.getByText('我的申请：请教师复核高分歧。').waitFor()
    phase='teacher dashboard and CSV export'
    await teacher.setViewportSize(desktop)
    await click(teacher,'查看教师看板')
    await teacher.getByText('6 / 6 条',{exact:true}).waitFor()
    await teacher.getByLabel('仅查看高分歧作业').check()
    await teacher.waitForFunction(()=>document.querySelectorAll('details.record').length===2)
    await teacher.getByLabel('仅查看高分歧作业').uncheck()
    await teacher.getByLabel('仅查看有待处理复核的作业').check()
    await teacher.waitForFunction(()=>document.querySelectorAll('details.record').length===1)
    await teacher.locator('details.record > summary').click()
    await click(teacher,'填写处理意见')
    await teacher.getByLabel('公开处理意见（不含匿名评审身份）').fill('已核对原文，保留原分数。')
    await teacher.getByLabel('我已核对公开意见不包含评审者身份').check()
    await click(teacher,'确认处理')
    await teacher.getByText('待处理 0 条 · 已处理 1 条').waitFor()
    await teacher.getByLabel('仅查看有待处理复核的作业').uncheck()
    const summary=teacher.locator('details.record > summary').filter({hasText:'合成验收学生1'})
    const record=summary.locator('..')
    if (await record.getAttribute('open')===null) await summary.click()
    await click(teacher,'查看修订轨迹（1 版）')
    await teacher.locator('.revision-list summary').click()
    await teacher.getByText('补充数据说明',{exact:true}).waitFor()
    const downloading=teacher.waitForEvent('download')
    await teacher.getByRole('link',{name:'导出统计 CSV'}).click()
    const csv=await fs.readFile(await (await downloading).path(),'utf8')
    for (const name of names) assert.ok(csv.includes(name))
    assert.ok(!csv.includes(quote) && !csv.includes(strengths))
    await teacher.locator('.submission-roster li').filter({hasText:'合成验收学生1'}).getByRole('button',{name:'查看学习档案'}).click()
    await teacher.getByRole('heading',{name:/合成验收学生1的学习档案/}).waitFor()
    phase='refresh student feedback and learning profile'
    await author.goto(feedbackUrls[0])
    await author.reload()
    await author.getByText('教师处理意见：已核对原文，保留原分数。').waitFor()
    assert.equal(await author.locator('.source-text').innerText(),originalSources[0])
    await author.getByText('1 / 5 版').waitFor()
    await click(author,'学习档案')
    await author.getByRole('heading',{name:/合成验收学生1的学习档案/}).waitFor()
    await widthOK(author)
    console.log('PASS UI registration, class, assignment, PDF/text, evidence+draft, 6 reviews, AI fallback, mobile publish, feedback, revision, appeal, profile and CSV')
  } catch (error) {
    console.error('Browser phase failed:',phase)
    throw error
  } finally { await browser.close() }
}
main().catch(error=>{console.error(error);process.exitCode=1})
