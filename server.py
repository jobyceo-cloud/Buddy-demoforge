#!/usr/bin/env python3
"""
DemoForge Server -- Python backend for demo storage, playback, and URL capture.
Serves the frontend from ./index.html, stores demos as JSON files, and handles
server-side screenshot capture via Playwright.
"""

import json, os, uuid, urllib.request, urllib.error, html, base64
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

PORT = 9000
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

FRONTEND_HTML = os.path.join(BASE_DIR, "index.html")
with open(FRONTEND_HTML, "r", encoding="utf-8") as f:
    FRONTEND = f.read()

# ------------------------------------------------------------------
# Demo storage
# ------------------------------------------------------------------
def save_demo(payload: dict) -> str:
    demo_id = "df-" + uuid.uuid4().hex[:12]
    demo = {
        "id": demo_id,
        "title": payload.get("title", "Untitled Demo"),
        "source": payload.get("source", {}),
        "hotspots": payload.get("hotspots", []),
    }
    path = os.path.join(DATA_DIR, f"{demo_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(demo, f, indent=2)
    return demo_id

def load_demo(demo_id: str):
    path = os.path.join(DATA_DIR, f"{demo_id}.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None

def list_demos() -> list:
    demos = []
    for fn in os.listdir(DATA_DIR):
        if fn.endswith(".json"):
            path = os.path.join(DATA_DIR, fn)
            with open(path, "r", encoding="utf-8") as f:
                demos.append(json.load(f))
    return demos

# ------------------------------------------------------------------
# Screenshot capture
# ------------------------------------------------------------------
def capture_screenshot(url: str):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, "Playwright not installed. Run: pip install playwright && playwright install chromium"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(url, wait_until="networkidle", timeout=15000)
            screenshot = page.screenshot(type="png", full_page=False)
            browser.close()
        return base64.b64encode(screenshot).decode("utf-8"), None
    except Exception as e:
        return None, str(e)

# ------------------------------------------------------------------
# Request handler
# ------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{self.address_string()}] {fmt % args}")

    def send_json(self, data, code=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, text, mime="text/html", code=200):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", f"{mime}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length).decode("utf-8")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        # Frontend app
        if path in ("", "/", "/index.html"):
            self.send_text(FRONTEND)
            return

        # API: list demos
        if path == "/api/demos":
            self.send_json(list_demos())
            return

        # API: get a demo
        if path.startswith("/api/demos/"):
            demo_id = path.split("/")[-1]
            demo = load_demo(demo_id)
            if demo:
                self.send_json(demo)
            else:
                self.send_json({"error": "Demo not found"}, 404)
            return

        # API: proxy HTML for iframing (legacy)
        if path.startswith("/api/proxy"):
            url = qs.get("url", [""])[0]
            if not url.startswith(("http://", "https://")):
                self.send_json({"error": "Invalid URL"}, 400)
                return
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    ct = resp.headers.get("Content-Type", "text/html")
                    data = resp.read()
                self.send_response(200)
                self.send_header("Content-Type", ct)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        # Playback page
        if path.startswith("/play/"):
            demo_id = path.split("/")[-1]
            demo = load_demo(demo_id)
            if not demo:
                self.send_text("<h1>Demo not found</h1>", code=404)
                return
            page = self.build_player_page(demo)
            self.send_text(page)
            return

        self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/demos":
            try:
                body = json.loads(self.read_body())
                demo_id = save_demo(body)
                self.send_json({"id": demo_id, "url": f"http://localhost:{PORT}/play/{demo_id}"})
            except json.JSONDecodeError as e:
                self.send_json({"error": f"Invalid JSON: {e}"}, 400)
            return

        if path == "/api/capture":
            try:
                body = json.loads(self.read_body())
                url = body.get("url", "")
                if not url.startswith(("http://", "https://")):
                    self.send_json({"error": "Invalid URL"}, 400)
                    return
                b64, err = capture_screenshot(url)
                if err or not b64:
                    self.send_json({"error": "This site blocks screenshots. Upload a screenshot manually instead.", "detail": err}, 422)
                    return
                self.send_json({"imageData": "data:image/png;base64," + b64})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        self.send_json({"error": "Not found"}, 404)

    def build_player_page(self, demo: dict) -> str:
        demo_json = json.dumps(demo)
        title = html.escape(demo.get("title", "DemoForge Demo"))
        return (
            "<!DOCTYPE html>\n"
            "<html lang='en'>\n"
            "<head>\n"
            "<meta charset='UTF-8'>\n"
            "<meta name='viewport' content='width=device-width, initial-scale=1.0'>\n"
            "<title>" + title + " - DemoForge</title>\n"
            "<style>\n"
            "*,::before,::after { box-sizing:border-box;margin:0;padding:0 }\n"
            "body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif; background:#0b0f19;color:#e5e7eb;min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:24px }\n"
            ".stage { position:relative;background:#111827;border-radius:16px;overflow:hidden;box-shadow:0 10px 40px rgba(0,0,0,.35);max-width:95vw;max-height:70vh;width:auto;height:auto;display:flex;align-items:center;justify-content:center }\n"
            ".stage img { display:block;max-width:100%;max-height:70vh;height:auto;border:none }\n"
            ".stage iframe { display:block;max-width:100%;max-height:70vh;border:none;width:900px;height:640px }\n"
            ".bubble { position:absolute;background:#1a2332;border:1px solid rgba(255,255,255,.12);border-radius:12px;padding:16px 18px;max-width:300px;box-shadow:0 10px 40px rgba(0,0,0,.35);transition:all .4s }\n"
            ".bubble h4 { font-size:13px;color:#22d3ee;margin-bottom:4px }\n"
            ".bubble p { font-size:13px;line-height:1.45;color:#e5e7eb }\n"
            ".controls { display:flex;align-items:center;gap:16px;margin-top:18px;width:100%;max-width:500px;padding:0 10px }\n"
            ".progress { flex:1;height:4px;background:rgba(255,255,255,.1);border-radius:2px;overflow:hidden }\n"
            ".progress > div { height:100%;background:#0ea5e9;transition:width .4s }\n"
            ".btn { padding:12px 22px;border-radius:10px;background:#0ea5e9;color:#fff;border:none;font-size:15px;font-weight:600;cursor:pointer;min-width:80px }\n"
            ".btn-sec { background:#1a2332 }\n"
            ".header { margin-bottom:16px;text-align:center }\n"
            ".header h2 { font-size:18px;font-weight:700 }\n"
            ".header p { font-size:13px;color:#9ca3af;margin-top:4px }\n"
            ".spotlight { position:absolute;width:100px;height:100px;border-radius:50%;box-shadow:0 0 0 9999px rgba(0,0,0,.75);transition:all .5s ease;pointer-events:none }\n"
            ".spotlight::after { content:'';position:absolute;inset:-4px;border:2px solid #0ea5e9;border-radius:50%;animation:pulse 2s infinite }\n"
            "@keyframes pulse { 0% {opacity:1} 50% {opacity:.6} 100% {opacity:1} }\n"
            "@media (max-width:600px){ .btn { padding:14px 24px;font-size:16px } .bubble { max-width:260px;padding:14px } }\n"
            "</style>\n"
            "</head>\n"
            "<body>\n"
            "<div class='header'><h2>" + title + "</h2><p>Click Next to walk through</p></div>\n"
            "<div class='stage' id='stage'><img id='img' style='display:none'><iframe id='frame' sandbox='allow-scripts allow-same-origin allow-forms' style='display:none;width:900px;height:640px'></iframe><div class='spotlight' id='spot'></div><div class='bubble' id='bubble'><h4 id='b-step'>Step 1</h4><p id='b-text'>Loading...</p></div></div>\n"
            "<div class='controls'>\n"
            "  <button class='btn btn-sec' onclick='prev()' style='min-width:80px'>Back</button>\n"
            "  <div class='progress'><div id='prog' style='width:0%'></div></div>\n"
            "  <button class='btn' onclick='next()' style='min-width:80px'>Next</button>\n"
            "</div>\n"
            "<script>\n"
            "const demo = " + demo_json + ";\n"
            "let cur = 0;\n"
            "const hotspots = demo.hotspots || [];\n"
            "const stage = document.getElementById('stage');\n"
            "const spot = document.getElementById('spot');\n"
            "const bubble = document.getElementById('bubble');\n"
            "const bStep = document.getElementById('b-step');\n"
            "const bText = document.getElementById('b-text');\n"
            "const prog = document.getElementById('prog');\n"
            "const img = document.getElementById('img');\n"
            "const frame = document.getElementById('frame');\n"
            "function clamp(n,min,max){ return Math.max(min,Math.min(max,n)); }\n"
            "function render() {\n"
            "  const h = hotspots[cur];\n"
            "  if (!h) return;\n"
            "  const sx = h.pctX * stage.offsetWidth, sy = h.pctY * stage.offsetHeight;\n"
            "  spot.style.left = (sx - 50) + 'px';\n"
            "  spot.style.top = (sy - 50) + 'px';\n"
            "  let bx = sx + 70, by = sy;\n"
            "  const vpW = window.innerWidth, vpH = window.innerHeight;\n"
            "  const sRect = stage.getBoundingClientRect();\n"
            "  let viewBx = sRect.left + bx, viewBy = sRect.top + by;\n"
            "  if (viewBx + bubble.offsetWidth > vpW - 10) viewBx = vpW - bubble.offsetWidth - 10;\n"
            "  if (viewBx < 10) viewBx = 10;\n"
            "  if (viewBy + bubble.offsetHeight > vpH - 10) viewBy = vpH - bubble.offsetHeight - 10;\n"
            "  if (viewBy < 10) viewBy = 10;\n"
            "  bx = viewBx - sRect.left; by = viewBy - sRect.top;\n"
            "  bubble.style.left = bx + 'px';\n"
            "  bubble.style.top = by + 'px';\n"
            "  bStep.textContent = 'Step ' + (cur+1) + ' of ' + hotspots.length;\n"
            "  bText.textContent = h.text;\n"
            "  prog.style.width = (((cur+1)/hotspots.length)*100) + '%';\n"
            "}\n"
            "function next(){ if(cur < hotspots.length-1){ cur++; render(); } }\n"
            "function prev(){ if(cur > 0){ cur--; render(); } }\n"
            "if(demo.source.type === 'image'){\n"
            "  img.src = demo.source.imageData;\n"
            "  img.style.display = 'block';\n"
            "  img.style.maxWidth = '100%';\n"
            "  img.style.height = 'auto';\n"
            "  frame.style.display = 'none';\n"
            "  img.addEventListener('load', function(){ stage.style.width='auto'; stage.style.height='auto'; render(); });\n"
            "} else {\n"
            "  img.style.display = 'none';\n"
            "  frame.style.display = 'block';\n"
            "  frame.src = demo.source.url;\n"
            "  frame.addEventListener('load', render);\n"
            "}\n"
            "window.addEventListener('resize', render);\n"
            "</script>\n"
            "</body>\n"
            "</html>"
        )

if __name__ == "__main__":
    print(f"""
+----------------------------------------------+
|  DemoForge Server v0.2                       |
|  URL:   http://localhost:{PORT}                  |
|  Dir:   {DATA_DIR}        |
+----------------------------------------------+
Press Ctrl+C to stop.
""")
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
