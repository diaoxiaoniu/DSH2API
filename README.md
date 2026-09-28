# DSH2API

把 DeepSeek Harness 封装成兼容 OpenAI 和 Anthropic Messages 的本地 API，
可用于 Cherry Studio、Chatbox 等客户端。

支持两种后端：

- 已登录的 LightVela DeepSeek Harness WebUI
- 本机安装的 DeepSeek Harness SDK

> 本项目不会绕过服务商的认证或计费。模型用量、免费额度与使用限制均以
> Harness 实例所绑定账户及服务商规则为准。

## 接口

- `GET /v1/models`
- `POST /v1/chat/completions`（OpenAI Chat Completions）
- `POST /v1/messages`（Anthropic Messages，适用于 Cherry Studio 智能体）
- Bearer API Key 与 `x-api-key` 鉴权
- 普通响应与 SSE 流式响应
- 自动过滤 Cherry Studio Agent 重复注入的 Claude Code 系统提示词

目前 Harness 会在一轮任务提交答案后，桥接器才开始输出 SSE，因此不是逐 token
实时流。Harness 发起的交互式选择题会转换成普通聊天文本，避免客户端一直等待。
普通自定义系统提示词仍会保留；只有具有明确特征的 Claude Code 自动提示词会被过滤。

## 快速开始：LightVela WebUI

需要 Python 3.11+ 和 Chrome/Chromium。

```bash
git clone https://github.com/diaoxiaoniu/DSH2API.git
cd DSH2API
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

编辑 `.env`：

```dotenv
DSH2API_KEY=请换成一串足够长的随机字符
DSH2API_HOST=127.0.0.1
DSH2API_PORT=18611
DSH_REMOTE_URL=https://你的实例.dsh.lightvela.tech
DSH_REMOTE_TIMEOUT=600
```

启动服务：

```bash
python app.py
```

### 导入浏览器登录态

1. 在 Chrome 打开 `chrome://extensions`，开启“开发者模式”。
2. 点击“加载已解压的扩展程序”，选择本项目的 `extension` 文件夹。
3. 打开并登录你的 LightVela DeepSeek Harness 页面。
4. 点击扩展 **DSH2API 登录导入**。
5. 填写本地服务地址 `http://127.0.0.1:18611` 和 `.env` 中的
   `DSH2API_KEY`，然后点击导入。

登录 Cookie 只会保存到本机 `data/remote_auth.json`，该目录已被 Git 忽略。
导入接口仅接受来自本机的请求。

## 可选：本地 Harness SDK

不设置 `DSH_REMOTE_URL` 时，DSH2API 会使用本机 Harness。配置示例：

```dotenv
DSH_HOME=/absolute/path/to/.dsh
DSH_WORKSPACE=/absolute/path/to/workspace
DSH_PROFILE=sdk
DSH_PROVIDER=deepseek-official
DSH_MODEL=deepseek-v4-flash
DSH_MAX_TOKENS=8192
```

## 客户端配置

OpenAI 兼容模式（普通聊天）：

- Base URL：`http://127.0.0.1:18611/v1`
- API Key：`.env` 中的 `DSH2API_KEY`
- Model：`deepseek-harness`

Anthropic 模式（Cherry Studio 智能体）：

- API Host：`http://127.0.0.1:18611`
- API Key：`.env` 中的 `DSH2API_KEY`
- Model：`deepseek-harness`

Anthropic 客户端通常会自动追加 `/v1/messages`，因此 API Host 一般不要添加
`/v1`。如果客户端字段明确要求以 `/v1` 结尾的 Base URL，再填写
`http://127.0.0.1:18611/v1`。

## 接口测试

```bash
curl http://127.0.0.1:18611/v1/chat/completions \
  -H 'Authorization: Bearer YOUR_DSH2API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-harness","messages":[{"role":"user","content":"你好"}],"stream":false}'
```

运行测试：

```bash
pip install -r requirements-dev.txt
PYTHONPATH=. pytest -q
```

## 安全提示

- 请保持默认监听地址 `127.0.0.1`，不要直接暴露到公网。
- 不要提交 `.env`、`data/`、Cookie、API Key 或真实实例地址。
- 如果登录态泄露，请立即在服务商页面退出登录或撤销相关会话。
- 本项目使用的是 WebUI 当前使用的网关，服务商升级后接口可能发生变化。

## License

[MIT](LICENSE)
