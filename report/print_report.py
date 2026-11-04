"""CDP 打印管线：页眉=报告简称，页脚=页码/总页数 + 调研日期。

替代原 Chrome CLI --print-to-pdf（CLI 无法自定义页眉页脚）。
用法：python print_report.py（输出 report_new.pdf，由调用方 mv 覆盖正式文件）
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

import websocket

CHROME = r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
HTML_URL = "file:///D:/workspace/agent-tools-survey/report/report.html"
OUT_PDF = r"D:\workspace\agent-tools-survey\report\report_new.pdf"
USER_DATA = tempfile.mkdtemp(prefix="chrome-print-profile-")
DATE = "2026-11-03"

proc = subprocess.Popen(
    [CHROME, "--headless=new", "--disable-gpu", "--no-first-run",
     "--remote-debugging-port=0", "--remote-allow-origins=*",
     f"--user-data-dir={USER_DATA}", "about:blank"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

port = None
for _ in range(100):
    try:
        with open(os.path.join(USER_DATA, "DevToolsActivePort")) as f:
            port, _ = f.read().split()
        break
    except (FileNotFoundError, ValueError):
        time.sleep(0.1)
if port is None:
    print("FAIL: DevToolsActivePort not found", file=sys.stderr)
    proc.terminate()
    sys.exit(1)

req = urllib.request.Request(
    f"http://127.0.0.1:{port}/json/new?{urllib.parse.quote(HTML_URL)}", method="PUT")
with urllib.request.urlopen(req, timeout=5) as r:
    target = json.load(r)

ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=120)
_msg_id = [0]

def call(method, params=None):
    _msg_id[0] += 1
    ws.send(json.dumps({"id": _msg_id[0], "method": method, "params": params or {}}))
    while True:
        m = json.loads(ws.recv())
        if m.get("id") == _msg_id[0]:
            if "error" in m:
                raise RuntimeError(m["error"])
            return m.get("result", {})

call("Page.enable")
call("Page.navigate", {"url": HTML_URL})
while True:
    m = json.loads(ws.recv())
    if m.get("method") == "Page.loadEventFired":
        break
time.sleep(1.0)  # 等渲染稳定

FONT = "font-family:'Microsoft YaHei',sans-serif;"
header = ('<div style="width:100%;font-size:8pt;color:#8a94a0;text-align:center;' + FONT
          + '">Agent 工具调研报告</div>')
footer = ('<div style="width:100%;font-size:8pt;color:#8a94a0;text-align:center;' + FONT
          + '">第 <span class="pageNumber"></span> / <span class="totalPages"></span> 页'
          + f'　·　调研日期 {DATE}</div>')

result = call("Page.printToPDF", {
    "printBackground": True,
    "preferCSSPageSize": True,
    "displayHeaderFooter": True,
    "headerTemplate": header,
    "footerTemplate": footer,
})
with open(OUT_PDF, "wb") as f:
    f.write(base64.b64decode(result["data"]))
print(f"OK bytes={len(result['data'])}")
ws.close()
proc.kill()
