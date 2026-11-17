import os, json, time, threading
from flask import Flask, request, jsonify, Response
import requests

app = Flask(__name__)

@app.after_request
def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return resp

DS = "https://api.deepseek.com"
KEY = os.environ.get("DEEPSEEK_API_KEY", "")
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GH_REPO = os.environ.get("GITHUB_USAGE_REPO", "")   # e.g. Liu233jh/nebula-echo
LIMIT = float(os.environ.get("ECHO_LIMIT_YUAN", "9.5"))  # 累计消费上限（元），到顶锁死

# 官方单价（元）
P_SEARCH = 0.005          # /次（成功计费）
P_FETCH = 0.005           # /次
P_OCR_PAGE = 0.0015       # /页
P_ASR_SEC = 0.39 / 3600.0 # 5折 折到秒
P_TTS_CHAR = 1.5 / 10000.0# 5折 折到计费字符（汉字=2字符）
P_CHAT_OUT = 2.0 / 1e6    # 输出 token
P_CHAT_IN = 1.0 / 1e6     # 输入 token（缓存未命中，保守）

HEAD = {"Authorization": "Bearer " + KEY}

_usage = {"spent": 0.0, "calls": {}, "updated": 0}
_lock = threading.Lock()
_rate = {}  # ip -> [window_start, count]

def _load_usage():
    """启动时从 GitHub repo 恢复账本"""
    global _usage
    if not GH_REPO or not GH_TOKEN: return
    try:
        r = requests.get(
            "https://api.github.com/repos/%s/contents/usage.json" % GH_REPO,
            headers={"Authorization": "token " + GH_TOKEN,
                     "Accept": "application/vnd.github+json"}, timeout=10)
        if r.status_code == 200:
            import base64
            _usage = json.loads(base64.b64decode(r.json()["content"]).decode("utf-8"))
    except Exception:
        pass

def _save_usage():
    """账本写回 GitHub（异步，防阻塞）"""
    if not GH_REPO or not GH_TOKEN: return
    def worker():
        try:
            h = {"Authorization": "token " + GH_TOKEN,
                 "Accept": "application/vnd.github+json"}
            r = requests.get("https://api.github.com/repos/%s/contents/usage.json" % GH_REPO,
                             headers=h, timeout=10)
            body = {"message": "echo usage", "content": json.dumps(_usage)}
            if r.status_code == 200:
                body["sha"] = r.json()["sha"]
            requests.put("https://api.github.com/repos/%s/contents/usage.json" % GH_REPO,
                         headers=h, json=body, timeout=15)
        except Exception:
            pass
    threading.Thread(target=worker, daemon=True).start()

def billed(kind, amount):
    """记账：amount 为本次消耗元。返回 False 表示已超限拒绝"""
    with _lock:
        if _usage["spent"] >= LIMIT: return False
        _usage["spent"] = round(_usage["spent"] + amount, 6)
        _usage["calls"][kind] = _usage["calls"].get(kind, 0) + 1
        _usage["updated"] = int(time.time())
        if _usage["spent"] >= LIMIT * 0.98:
            threading.Thread(target=_save_usage, daemon=True).start()
    return True

def over_limit():
    with _lock: return _usage["spent"] >= LIMIT

def bill(kind, amount):
    with _lock:
        _usage["spent"] = round(_usage["spent"] + amount, 6)
        _usage["calls"][kind] = _usage["calls"].get(kind, 0) + 1
        _usage["updated"] = int(time.time())
    _save_usage()

def gate():
    """总闸 + IP 频控"""
    if not KEY: return jsonify(error="后端未配置 DEEPSEEK_API_KEY"), 503
    if over_limit(): return jsonify(error="quota", message="舱内额度（约10元）已用完，全部 AI 功能已锁死"), 429
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0]
    now = time.time()
    w = _rate.get(ip)
    if not w or now - w[0] > 60: _rate[ip] = [now, 1]
    else:
        w[1] += 1
        if w[1] > 30: return jsonify(error="rate", message="手速太快，歇一分钟"), 429
    return None

def bill_chars(text):
    """DeepSeek TTS 计费字符：非 ASCII 算 2"""
    return sum(2 if ord(c) > 127 else 1 for c in text)

@app.get("/api/usage")
def usage():
    return jsonify(spent=_usage["spent"], limit=LIMIT,
                   available=round(max(0, LIMIT - _usage["spent"]), 2),
                   calls=_usage["calls"], configured=bool(KEY))

@app.post("/api/tts")
def tts():
    g = gate()
    if g: return g
    d = request.get_json(silent=True) or {}
    text = (d.get("text") or "").strip()
    if not text: return jsonify(error="text 不能为空"), 400
    if bill_chars(text) > 4000: return jsonify(error="单次最长 4000 计费字符"), 400
    voice = d.get("voice") or "stella"
    up = {"text": text[:8000], "voice_id": voice if voice in ("stella", "tide") else "stella",
          "format": "wav", "filter_markdown": True}
    r = requests.post(DS + "/v1/tools/tts", headers=HEAD, json=up, timeout=120, stream=True)
    if r.status_code != 200:
        return jsonify(error="deepseek", status=r.status_code, message=r.text[:300]), 502
    n = bill_chars(up["text"])
    if not billed("tts", n * P_TTS_CHAR):
        r.close(); return jsonify(error="quota", message="额度刚好用完"), 429
    bill("tts", n * P_TTS_CHAR)
    return Response(r.content, mimetype="audio/wav",
                    headers={"X-Charged": "%.4f" % (n * P_TTS_CHAR)})

@app.post("/api/asr")
def asr():
    g = gate()
    if g: return g
    f = request.files.get("file")
    if not f: return jsonify(error="需要 file 字段"), 400
    data = f.read()
    if len(data) > 8 * 1024 * 1024: return jsonify(error="音频大于 8MiB"), 400
    r = requests.post(DS + "/v1/tools/asr", headers=HEAD,
                      files={"file": (f.filename or "speech.wav", data)}, timeout=180)
    if r.status_code != 200:
        return jsonify(error="deepseek", status=r.status_code, message=r.text[:300]), 502
    j = r.json()
    secs = max(1, int(j.get("duration_ms", 1000)) // 1000)
    cost = secs * P_ASR_SEC
    if not billed("asr", cost):
        return jsonify(error="quota", message="额度刚好用完"), 429
    bill("asr", cost)
    return jsonify(text=j.get("text", ""), duration_ms=j.get("duration_ms"),
                   charged=round(cost, 4))

@app.post("/api/search")
def search():
    g = gate()
    if g: return g
    d = request.get_json(silent=True) or {}
    q = (d.get("query") or "").strip()
    if not q: return jsonify(error="query 不能为空"), 400
    cnt = max(1, min(10, int(d.get("count") or 6)))
    r = requests.post(DS + "/v1/tools/search", headers=HEAD,
                      json={"query": q[:128], "count": cnt, "need_content": False}, timeout=60)
    if r.status_code != 200:
        return jsonify(error="deepseek", status=r.status_code, message=r.text[:300]), 502
    results = r.json().get("results") or []
    if results:  # 官方规则：无结果不计费
        if not billed("search", P_SEARCH):
            return jsonify(error="quota", message="额度刚好用完"), 429
        bill("search", P_SEARCH)
    return jsonify(results=results, charged=P_SEARCH if results else 0)

@app.post("/api/fetch")
def fetch_page():
    g = gate()
    if g: return g
    d = request.get_json(silent=True) or {}
    url = (d.get("url") or "").strip()
    if not url.startswith("http"): return jsonify(error="需要合法 url"), 400
    r = requests.post(DS + "/v1/tools/fetch", headers=HEAD,
                      json={"url": url[:8192]}, timeout=90)
    if r.status_code != 200:
        return jsonify(error="deepseek", status=r.status_code, message=r.text[:300]), 502
    j = r.json()
    if not billed("fetch", P_FETCH):
        return jsonify(error="quota", message="额度刚好用完"), 429
    bill("fetch", P_FETCH)
    return jsonify(title=j.get("title", ""), content=j.get("content", ""), charged=P_FETCH)

@app.post("/api/ocr")
def ocr():
    g = gate()
    if g: return g
    f = request.files.get("file")
    if not f: return jsonify(error="需要 file 字段"), 400
    r = requests.post(DS + "/v1/tools/ocr", headers=HEAD,
                      files={"file": (f.filename or "image.png", f.read())},
                      data={"purpose": "text_recognition"}, timeout=200)
    if r.status_code != 200:
        return jsonify(error="deepseek", status=r.status_code, message=r.text[:300]), 502
    # 响应体前导可能有换行符，直接 json 解析
    try:
        j = json.loads(r.text)
    except Exception:
        return jsonify(error="deepseek", message="响应解析失败"), 502
    pages = j.get("page_count", 1)
    cost = pages * P_OCR_PAGE
    if not billed("ocr", cost):
        return jsonify(error="quota", message="额度刚好用完"), 429
    bill("ocr", cost)
    text = "\n\n".join(p.get("text", "") for p in j.get("pages", []))
    return jsonify(text=text, pages=pages, charged=round(cost, 4))

@app.post("/api/chat")
def chat():
    g = gate()
    if g: return g
    d = request.get_json(silent=True) or {}
    msgs = d.get("messages") or []
    if not msgs: return jsonify(error="messages 不能为空"), 400
    if len(msgs) > 30: return jsonify(error="对话轮数过多"), 400
    up = {"model": "deepseek-flash", "messages": msgs[-16:], "stream": True,
          "max_tokens": 2000, "stream_options": {"include_usage": True}}
    if d.get("web_search"):
        up["tools"] = [{"type": "deepseek_web_search"}]
    r = requests.post(DS + "/chat/completions", headers={**HEAD, "Content-Type": "application/json",
                      "Accept": "text/event-stream"}, json=up, timeout=180, stream=True)
    if r.status_code != 200:
        return jsonify(error="deepseek", status=r.status_code, message=r.text[:300]), 502

    def gen():
        in_tok, out_tok = 0, 0
        # 必须按字节迭代再手动 utf-8 解码：decode_unicode=True 会猜成
        # ISO-8859-1，把中文切成乱码（ç¨æ· = 用户）
        for raw in r.iter_lines():
            if not raw:
                continue
            line = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else raw
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                yield "data: [DONE]\n\n"
                cost = in_tok * P_CHAT_IN + out_tok * P_CHAT_OUT
                if cost > 0: bill("chat", cost)
                break
            try:
                j = json.loads(payload)
            except Exception:
                yield "data: " + json.dumps({"_raw": line[:200]}) + "\n\n"
                continue
            u = j.get("usage")
            if u:
                in_tok = u.get("prompt_tokens", in_tok)
                out_tok = u.get("completion_tokens", out_tok)
            yield "data: " + json.dumps(j, ensure_ascii=False) + "\n\n"
    return Response(gen(), mimetype="text/event-stream")

@app.get("/")
def index():
    return jsonify(name="nebula-echo", status="ok", limit=LIMIT, spent=_usage["spent"])

_load_usage()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
