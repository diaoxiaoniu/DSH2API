const server = document.querySelector('#server')
const key = document.querySelector('#key')
const status = document.querySelector('#status')

chrome.storage.local.get(['server'], saved => {
  if (saved?.server) server.value = saved.server
})

document.querySelector('#import').addEventListener('click', async () => {
  status.textContent = '正在读取当前 DSH 登录态…'
  try {
    const [tab] = await chrome.tabs.query({active: true, currentWindow: true})
    const url = new URL(tab.url)
    if (!url.hostname.endsWith('.dsh.lightvela.tech')) throw new Error('请先打开已登录的 LightVela DSH WebUI')
    const cookies = await chrome.cookies.getAll({domain: url.hostname})
    if (!cookies.length) throw new Error('没有找到登录 Cookie，请从 LightVela 控制台重新访问 WebUI')
    const cookie = cookies.map(item => `${item.name}=${item.value}`).join('; ')
    const base = server.value.replace(/\/$/, '')
    const response = await fetch(`${base}/admin/auth/import`, {
      method: 'POST',
      headers: {'Authorization': `Bearer ${key.value}`, 'Content-Type': 'application/json'},
      body: JSON.stringify({origin: url.origin, cookie}),
    })
    const body = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(body.detail || body.error?.message || `HTTP ${response.status}`)
    chrome.storage.local.set({server: base})
    status.textContent = '导入成功。现在可以在 Cherry Studio / ChatBox 中使用。'
  } catch (error) {
    status.textContent = `导入失败：${error.message}`
  }
})
