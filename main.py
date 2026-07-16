#!/usr/bin/env python3
"""
极简聊天沙盒 - 内建 API 代理，解决 CORS 限制
多轮对话，流式界面，适配 OpenAI 兼容 API
"""
import http.server
import json
import urllib.request
import urllib.error
import sys
import socket
import logging

# --- 配置 ---
LISTEN_PORT = 8080
TIMEOUT = 30  # API 请求超时秒数

# --- 前端页面 (单文件，所有对话状态保存在前端内存) ---
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=yes">
<title>API 聊天沙盒</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, sans-serif; max-width: 720px; margin: 20px auto; padding: 0 16px; }
  h2 { margin-bottom: 8px; }
  #config, #chat-section { margin-bottom: 12px; }
  label { display: block; font-size: 0.9em; margin-top: 8px; font-weight: 600; }
  input, textarea { width: 100%; padding: 10px; margin: 4px 0; box-sizing: border-box; font-size: 16px; border: 1px solid #888; border-radius: 6px; }
  #chat-box { height: 400px; overflow-y: auto; border: 1px solid #aaa; border-radius: 8px; padding: 12px; background: #f8f9fa; margin-bottom: 8px; }
  .msg { margin-bottom: 10px; white-space: pre-wrap; word-break: break-word; }
  .user { color: #0b57d0; font-weight: 500; }
  .assistant { color: #0d652d; }
  .error { color: #b30000; }
  button { padding: 10px 18px; margin: 5px 4px 0 0; font-size: 16px; border-radius: 6px; border: none; background: #1a73e8; color: white; cursor: pointer; }
  button:active { opacity: 0.8; }
  .flex { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  .flex input { flex: 1; min-width: 200px; }
  small { color: #666; display: block; margin-top: 12px; }
  @media (prefers-color-scheme: dark) {
    body { background: #1e1e1e; color: #ddd; }
    #chat-box { background: #2a2a2a; border-color: #555; }
    input, textarea { background: #333; color: #eee; border-color: #555; }
    .user { color: #8ab4f8; } .assistant { color: #81c784; } .error { color: #f28b82; }
  }
</style>
</head>
<body>
<h2>🧪 本地聊天沙盒</h2>
<small style="margin:0 0 12px">请求通过本地代理转发，无 CORS，对话历史保存在当前页面</small>

<div id="config">
  <label>API 地址 (OpenAI 兼容)</label>
  <input type="text" id="apiBase" value="https://api.openai.com/v1" placeholder="https://api.openai.com/v1">
  <label>API Key</label>
  <input type="password" id="apiKey" placeholder="sk-...">
  <label>模型</label>
  <div class="flex">
    <input type="text" id="model" value="gpt-3.5-turbo" placeholder="模型名">
    <button onclick="clearChat()" style="background:#6c757d">清空对话</button>
  </div>
</div>

<div id="chat-box" id="chatBox"></div>
<div class="flex">
  <input type="text" id="userInput" placeholder="输入消息，按回车发送" autofocus />
  <button onclick="send()">发送</button>
</div>
<small>⚠️ API Key 仅用于本次会话，刷新后丢失。</small>

<script>
// 前端对话历史（多轮上下文）
let messages = [];

const chatBox = document.getElementById('chat-box');
const userInput = document.getElementById('userInput');

userInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') send();
});

function appendMessage(role, content, isError = false) {
  const div = document.createElement('div');
  div.className = `msg ${role}${isError ? ' error' : ''}`;
  const prefix = role === 'user' ? '🧑 ' : '🤖 ';
  div.textContent = prefix + content;
  chatBox.appendChild(div);
  chatBox.scrollTop = chatBox.scrollHeight;
}

async function send() {
  const text = userInput.value.trim();
  if (!text) return;

  // 显示用户消息
  appendMessage('user', text);
  messages.push({ role: 'user', content: text });
  userInput.value = '';

  const base = document.getElementById('apiBase').value.trim().replace(/\/+$/, '');
  const key = document.getElementById('apiKey').value.trim();
  const model = document.getElementById('model').value.trim() || 'gpt-3.5-turbo';

  if (!base || !key) {
    appendMessage('assistant', '请先填写 API 地址和 Key', true);
    return;
  }

  try {
    const response = await fetch('/proxy', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ base, key, model, messages })
    });

    if (!response.ok) {
      const errData = await response.json().catch(() => ({}));
      throw new Error(errData.error || `HTTP ${response.status}`);
    }

    const data = await response.json();
    const reply = data.content;
    appendMessage('assistant', reply);
    messages.push({ role: 'assistant', content: reply });
  } catch (err) {
    appendMessage('assistant', `❌ 错误: ${err.message}`, true);
  }
}

function clearChat() {
  messages = [];
  chatBox.innerHTML = '';
  appendMessage('assistant', '对话已清空，可重新开始。');
}
</script>
</body>
</html>"""

# --- 后端代理处理器 ---
class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/' or self.path == '/index.html':
            self._serve_html()
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == '/proxy':
            self._handle_proxy()
        else:
            self.send_error(404)

    def _serve_html(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(HTML_PAGE.encode('utf-8'))

    def _handle_proxy(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length))
        except Exception as e:
            self._respond_json({'error': f'请求解析失败: {str(e)}'}, 400)
            return

        base = body.get('base', '').rstrip('/')
        key = body.get('key', '')
        model = body.get('model', 'gpt-3.5-turbo')
        messages = body.get('messages', [])

        if not base or not key:
            self._respond_json({'error': '缺少 API 地址或 Key'}, 400)
            return
        if not messages:
            self._respond_json({'error': '消息列表不能为空'}, 400)
            return

        url = f"{base}/chat/completions"
        req_data = json.dumps({
            "model": model,
            "messages": messages,
            "temperature": 0.7
        }).encode('utf-8')

        request = urllib.request.Request(
            url,
            data=req_data,
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {key}',
                'User-Agent': 'LocalChatSandbox/1.0'
            }
        )

        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as resp:
                resp_body = resp.read().decode('utf-8')
                data = json.loads(resp_body)
                content = data['choices'][0]['message']['content']
                self._respond_json({'content': content})
        except urllib.error.HTTPError as e:
            err_body = e.read().decode('utf-8', errors='replace')[:500]
            logging.error(f"API HTTP {e.code}: {err_body}")
            self._respond_json({'error': f'API 返回错误 [{e.code}]: {err_body}'}, e.code)
        except urllib.error.URLError as e:
            logging.error(f"连接错误: {e.reason}")
            self._respond_json({'error': f'无法连接到 API: {e.reason}'}, 502)
        except socket.timeout:
            self._respond_json({'error': 'API 请求超时'}, 504)
        except Exception as e:
            logging.exception("未知代理错误")
            self._respond_json({'error': f'代理内部错误: {str(e)}'}, 500)

    def _respond_json(self, data, code=200):
        resp = json.dumps(data).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, format, *args):
        # 简化日志，仅输出到 stderr
        sys.stderr.write(f"[{self.address_string()}] {format % args}\n")


# --- 启动入口 ---
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    port = int(sys.argv[1]) if len(sys.argv) > 1 else LISTEN_PORT
    server = http.server.HTTPServer(('0.0.0.0', port), ProxyHandler)
    print(f"✨ 沙盒已启动 → http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 沙盒已停止")
        server.server_close()
