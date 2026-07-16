cat > sandbox.py << 'EOF'
import http.server, json, urllib.request, urllib.error, sys

HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>沙盒聊天</title>
<style>
body{font-family:system-ui;max-width:650px;margin:30px auto;padding:0 15px}
#chat{border:1px solid #ccc;height:400px;overflow-y:auto;padding:10px;margin-bottom:8px}
.user{color:#06c} .assistant{color:#090}
input,textarea,button{width:100%;padding:8px;margin:4px 0;box-sizing:border-box}
</style>
</head>
<body>
<h2>🧪 本地聊天沙盒（无 CORS 烦恼）</h2>
<label>API 地址</label>
<input id=base value="https://api.openai.com/v1" placeholder="https://api.openai.com/v1">
<label>API Key</label>
<input id=key type=password placeholder="sk-...">
<label>模型</label>
<input id=model value="gpt-3.5-turbo">
<div id=chat></div>
<input id=msg placeholder="按回车发送" autofocus>
<button onclick=send()>发送</button>
<hr><small style=color:#888>请求通过本地代理转发，不经过第三方。</small>
<script>
const c=document.getElementById('chat'),i=document.getElementById('msg');
i.addEventListener('keydown',e=>{if(e.key=='Enter')send()});
function app(role,t){const d=document.createElement('div');d.className=role;d.textContent=(role=='user'?'🧑 ':'🤖 ')+t;c.appendChild(d);c.scrollTop=c.scrollHeight}
async function send(){
  const msg=i.value.trim();if(!msg)return;
  app('user',msg);i.value='';
  const base=document.getElementById('base').value,key=document.getElementById('key').value,model=document.getElementById('model').value;
  try{
    const r=await fetch('/proxy',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({base,key,model,messages:[{role:'user',content:msg}]})
    });
    if(!r.ok)throw new Error((await r.json()).error||r.status);
    const d=await r.json();
    app('assistant',d.content);
  }catch(e){app('assistant','❌ '+e.message)}
}
</script>
</body>
</html>"""

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/' or self.path == '/index.html':
            self.send_response(200)
            self.send_header('Content-type','text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(HTML.encode())
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == '/proxy':
            length = int(self.headers['Content-Length'])
            body = json.loads(self.rfile.read(length))
            base = body['base'].rstrip('/')
            key = body['key']
            model = body['model']
            messages = body['messages']
            url = f"{base}/chat/completions"
            req = urllib.request.Request(url,
                data=json.dumps({"model":model,"messages":messages}).encode(),
                headers={'Content-Type':'application/json','Authorization':f'Bearer {key}'})
            try:
                with urllib.request.urlopen(req) as resp:
                    data = json.loads(resp.read())
                    content = data['choices'][0]['message']['content']
                    self.send_response(200)
                    self.send_header('Content-type','application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({"content":content}).encode())
            except urllib.error.HTTPError as e:
                msg = e.read().decode()[:300]
                self.send_response(e.code)
                self.end_headers()
                self.wfile.write(json.dumps({"error":f"API error {e.code}: {msg}"}).encode())
        else:
            self.send_error(404)

if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv)>1 else 8080
    print(f"✨ 沙盒已启动 → http://localhost:{port}")
    http.server.HTTPServer(('', port), Handler).serve_forever()
EOF
