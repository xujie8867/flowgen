#!/Users/xuhailong/flowgen/.venv/bin/python
"""
flowgen — Google Flow 生成 CLI（自研 CDP 直连版）

用法:
  flowgen credits                                 查可用 credits
  flowgen image "提示词" -o out.png               文生图
  flowgen image "提示词" -o out.png --ref a.png    图生图（参考图）
  flowgen video "提示词" -o out.mp4               文生视频
  flowgen video "提示词" --start a.png -o out.mp4  图生视频

原理: 连已登录的 Chrome (CDP 9224) → 拿 ya29 token → 页面内 fetch 调
aisandbox API（自动解决 reCAPTCHA）→ 轮询 → 下载。
无需 flow-agent / MCP / 扩展。

依赖: Chrome 9224 运行中且已登录 labs.google/fx/tools/flow
"""
import argparse
import asyncio
import base64
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

import websockets

CDP_PORT = int(os.environ.get("FLOW_CDP_PORT", "9224"))
API_BASE = "https://aisandbox-pa.googleapis.com"
# 项目 ID：优先环境变量，否则自动探测当前账号第一个项目
PROJECT_ID = os.environ.get("FLOW_PROJECT_ID", "")
SITEKEY = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
PROXY = os.environ.get("FLOW_PROXY", "http://127.0.0.1:1082")

VIDEO_ASPECTS = {"landscape": "VIDEO_ASPECT_RATIO_LANDSCAPE", "portrait": "VIDEO_ASPECT_RATIO_PORTRAIT"}
IMAGE_ASPECTS = {
    "landscape": "IMAGE_ASPECT_RATIO_LANDSCAPE",
    "portrait": "IMAGE_ASPECT_RATIO_PORTRAIT",
    "square": "IMAGE_ASPECT_RATIO_SQUARE",
    "4x3": "IMAGE_ASPECT_RATIO_4_3",
    "3x4": "IMAGE_ASPECT_RATIO_3_4",
}
IMAGE_MODELS = {"lite": "HARBOR_SEAL", "standard": "NARWHAL", "pro": "GEM_PIX_2"}
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"


def _no_proxy_env():
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        os.environ.pop(k, None)


def log(msg):
    print(msg, flush=True)


def _connect(ws_url, max_size):
    _no_proxy_env()
    return websockets.connect(ws_url, max_size=max_size)


def get_page_ws() -> str:
    r = subprocess.run(
        ["curl", "-s", "--max-time", "5", "-H", "User-Agent: Chrome/151.0.0.0",
         f"http://127.0.0.1:{CDP_PORT}/json/list"],
        capture_output=True, text=True, timeout=10,
    )
    try:
        tabs = json.loads(r.stdout)
    except Exception:
        raise RuntimeError(
            "Chrome CDP 9224 不可用。请先启动已登录 Flow 的 Chrome:\n"
            '  macOS:   "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" '
            '--remote-debugging-port=9224 --user-data-dir="$HOME/flowgen-chrome-profile" '
            "--no-first-run --proxy-server=http://127.0.0.1:1082 https://labs.google/fx/tools/flow\n"
            '  Windows: "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" '
            "--remote-debugging-port=9224 --user-data-dir=%USERPROFILE%\\flowgen-chrome-profile "
            "--no-first-run --proxy-server=http://127.0.0.1:1082 https://labs.google/fx/tools/flow"
        )
    for t in tabs:
        if t.get("type") == "page" and "labs.google" in t.get("url", ""):
            return t["webSocketDebuggerUrl"]
    for t in tabs:
        if t.get("type") == "page":
            return t["webSocketDebuggerUrl"]
    raise RuntimeError("CDP 无页面")


class Page:
    def __init__(self, ws_url):
        self.ws_url = ws_url
        self._next_id = 100

    async def _eval(self, ws, expr, timeout=180):
        self._next_id += 1
        cid = self._next_id
        await ws.send(json.dumps({"id": cid, "method": "Runtime.evaluate",
                                  "params": {"expression": expr, "returnByValue": True,
                                             "awaitPromise": True}}))
        while True:
            r = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            if r.get("id") == cid:
                res = r.get("result", {}).get("result", {})
                if res.get("subtype") == "error":
                    return {"error": str(res.get("description", "JS error"))}
                return {"value": res.get("value")}

    async def get_token(self, ws) -> str:
        js = """
        (async () => {
            if (window.__ya29 && window.__ya29Ts && (Date.now() - window.__ya29Ts) < 40*60*1000)
                return window.__ya29;
            const resp = await fetch('https://labs.google/fx/api/auth/session', {credentials: 'include'});
            const data = await resp.json();
            if (data.access_token) { window.__ya29 = data.access_token; window.__ya29Ts = Date.now(); }
            return data.access_token || '';
        })()
        """
        r = await self._eval(ws, js)
        return r.get("value", "") or ""

    async def api(self, ws, endpoint: str, body: dict, captcha_action: str = "",
                  timeout: int = 180, method: str = "POST") -> dict:
        """页面内 fetch 调 aisandbox API，需要时先解 reCAPTCHA。"""
        token = await self.get_token(ws)
        if not token:
            raise RuntimeError("无法获取 ya29 token（登录态失效？）")
        payload = body
        if captcha_action:
            captcha = await self.solve_captcha(ws, captcha_action)
            if not captcha:
                return {"status": 403, "data": "CAPTCHA_FAILED"}
            payload = json.loads(json.dumps(body))
            if payload.get("clientContext", {}).get("recaptchaContext"):
                payload["clientContext"]["recaptchaContext"]["token"] = captcha
            if payload.get("requests"):
                for req in payload["requests"]:
                    if req.get("clientContext", {}).get("recaptchaContext"):
                        req["clientContext"]["recaptchaContext"]["token"] = captcha
        js = f"""
        (async () => {{
            try {{
                const resp = await fetch({json.dumps(API_BASE + endpoint)}, {{
                    method: {json.dumps(method)},
                    headers: {{'Content-Type': 'application/json',
                               'Authorization': 'Bearer ' + window.__ya29}},
                    credentials: 'include',
                    body: {json.dumps(method)} === 'GET' ? undefined : JSON.stringify({json.dumps(payload)})
                }});
                const text = await resp.text();
                return JSON.stringify({{status: resp.status, data: text}});
            }} catch(e) {{ return JSON.stringify({{status: 0, data: 'JSERR:' + String(e)}}); }}
        }})()
        """
        r = await self._eval(ws, js, timeout=timeout)
        if "error" in r:
            return {"status": 0, "data": r["error"]}
        try:
            val = json.loads(r["value"])
        except Exception as e:
            return {"status": 0, "data": f"JS_EVAL_PARSE_FAIL: {e}, raw={r.get('value','')[:200]}"}
        try:
            val["data"] = json.loads(val["data"])
        except Exception:
            pass  # data 可能不是 JSON
        return val

    async def solve_captcha(self, ws, action: str) -> str:
        js = f"""
        (async () => {{
            if (!window.grecaptcha) {{
                await new Promise((res, rej) => {{
                    const s = document.createElement('script');
                    s.src = 'https://www.google.com/recaptcha/enterprise.js?render={SITEKEY}';
                    s.async = true; s.onload = res; s.onerror = rej;
                    document.head.appendChild(s);
                }});
            }}
            for (let i = 0; i < 30; i++) {{
                try {{
                    if (window.grecaptcha?.enterprise?.ready) {{
                        const token = await new Promise((resolve, reject) => {{
                            window.grecaptcha.enterprise.ready(async () => {{
                                try {{ resolve(await window.grecaptcha.enterprise.execute(
                                    '{SITEKEY}', {{action: {json.dumps(action)}}})); }}
                                catch(e) {{ reject(e); }}
                            }});
                        }});
                        if (token) return token;
                    }}
                }} catch(e) {{ }}
                await new Promise(r => setTimeout(r, 1000));
            }}
            return '';
        }})()
        """
        r = await self._eval(ws, js)
        return r.get("value", "") or ""

    async def upload_image(self, ws, image_path: str) -> str:
        token = await self.get_token(ws)
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        mime = "image/png" if image_path.lower().endswith(".png") else "image/jpeg"
        js = f"""
        (async () => {{
            try {{
                const resp = await fetch({json.dumps(API_BASE + "/v1/flow/uploadImage")}, {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json',
                               'Authorization': 'Bearer ' + window.__ya29}},
                    credentials: 'include',
                    body: JSON.stringify({{
                        clientContext: {{tool: 'PINHOLE', projectId: '{PROJECT_ID}'}},
                        fileName: {json.dumps(os.path.basename(image_path))},
                        imageBytes: {json.dumps(b64)},
                        isHidden: false,
                        isUserUploaded: true,
                        mimeType: {json.dumps(mime)}
                    }})
                }});
                const text = await resp.text();
                const data = JSON.parse(text);
                return data.media ? data.media.name : ('NO_MEDIA: ' + text.substring(0, 200));
            }} catch(e) {{ return 'JSERR: ' + String(e); }}
        }})()
        """
        r = await self._eval(ws, js, timeout=120)
        val = str(r.get("value", ""))
        if val.startswith("JSERR") or val.startswith("NO_MEDIA"):
            raise RuntimeError(f"上传失败: {val[:300]}")
        return val


def get_cookies() -> str:
    """从 CDP 页面拿 cookie（下载视频用）。"""
    ws_url = get_page_ws()
    result = {"cookies": ""}

    async def _run():
        async with _connect(ws_url, 50*1024*1024) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Network.getCookies",
                                      "params": {"urls": ["https://labs.google/"]}}))
            while True:
                r = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                if r.get("id") == 1:
                    cookies = r.get("result", {}).get("cookies", [])
                    result["cookies"] = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
                    return

    asyncio.run(_run())
    return result["cookies"]


async def aget_cookies() -> str:
    """从 CDP 页面拿 cookie（异步版，供 async 上下文调用）。"""
    ws_url = get_page_ws()
    async with _connect(ws_url, 50*1024*1024) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Network.getCookies",
                                  "params": {"urls": ["https://labs.google/"]}}))
        while True:
            r = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
            if r.get("id") == 1:
                cookies = r.get("result", {}).get("cookies", [])
                return "; ".join(f"{c['name']}={c['value']}" for c in cookies)


def curl_download(url: str, output: str, cookies: str, timeout=180) -> bool:
    cmd = ["curl", "-s", "--max-time", str(timeout), "-x", PROXY,
           "-H", f"User-Agent: {UA}", "-H", f"Cookie: {cookies}",
           "-L", "-o", output, url]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 30)
    return os.path.exists(output) and os.path.getsize(output) > 10000


def list_projects() -> list[dict]:
    """列出当前账号的所有 Flow 项目（TRPC project.searchUserProjects）。"""
    cookies = get_cookies()
    input_json = json.dumps({"json": {"pageSize": 20, "toolName": "PINHOLE", "cursor": ""}}, separators=(",", ":"))
    url = ("https://labs.google/fx/api/trpc/project.searchUserProjects"
           "?input=" + urllib.parse.quote(input_json, safe=""))
    cmd = ["curl", "-s", "--max-time", "30", "-x", PROXY,
           "-H", f"User-Agent: {UA}", "-H", f"Cookie: {cookies}",
           "-H", "Accept: application/json", url]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
    try:
        data = json.loads(res.stdout)
        projects = data["result"]["data"]["json"]["result"]["projects"]
    except Exception as e:
        raise RuntimeError(f"解析项目列表失败: {e}\n{res.stdout[:300]}")
    return projects


def download_video(video_id: str, output: str, cookies: str) -> bool:
    url = f"https://labs.google/fx/api/trpc/media.getMediaUrlRedirect?name={video_id}"
    return curl_download(url, output, cookies)


async def generate_images(args):
    page = Page(get_page_ws())
    async with _connect(page.ws_url, 100*1024*1024) as ws:
        token = await page.get_token(ws)
        if not token:
            raise RuntimeError("登录态失效")
        log(f"[flowgen] token OK ({token[:12]}...)")

        # 参考图上传
        ref_media_ids = []
        if args.ref:
            log("[flowgen] 上传参考图...")
            mid = await page.upload_image(ws, args.ref)
            ref_media_ids = [mid]
            log(f"  media_id={mid}")

        ts = int(time.time() * 1000)
        aspect = IMAGE_ASPECTS.get(args.aspect, "IMAGE_ASPECT_RATIO_LANDSCAPE")
        model = IMAGE_MODELS.get(args.model, "NARWHAL")
        req_item = {
            "clientContext": {"projectId": PROJECT_ID, "tool": "PINHOLE",
                              "userPaygateTier": "PAYGATE_TIER_ONE",
                              "sessionId": f";{ts}",
                              "recaptchaContext": {"applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB", "token": ""}},
            "seed": ts % 1000000,
            "structuredPrompt": {"parts": [{"text": args.prompt}]},
            "imageAspectRatio": aspect,
            "imageModelName": model,
        }
        if ref_media_ids:
            req_item["imageInputs"] = [{"name": mid, "imageInputType": "IMAGE_INPUT_TYPE_REFERENCE"} for mid in ref_media_ids]
        body = {
            "clientContext": {"projectId": PROJECT_ID, "tool": "PINHOLE",
                              "userPaygateTier": "PAYGATE_TIER_ONE",
                              "sessionId": f";{ts}",
                              "recaptchaContext": {"applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB", "token": ""}},
            "requests": [req_item],
        }
        if ref_media_ids:
            body["mediaGenerationContext"] = {"batchId": f"batch-{ts}"}
            body["useNewMedia"] = True

        endpoint = f"/v1/projects/{PROJECT_ID}/flowMedia:batchGenerateImages"
        log(f"[flowgen] 生成图片: \"{args.prompt[:60]}\" [{args.aspect}] model={model}")
        result = await page.api(ws, endpoint, body, captcha_action="IMAGE_GENERATION")
        status = result.get("status")
        data = result.get("data", {})
        if status != 200:
            log(f"[flowgen] 失败 ({status}): {json.dumps(data, ensure_ascii=False)[:400]}")
            return 1
        media = data.get("media", [])
        if not media:
            log(f"[flowgen] 无媒体返回: {json.dumps(data, ensure_ascii=False)[:300]}")
            return 1
        item = media[0]
        img = item.get("image", {}).get("generatedImage", {})
        fife_url = img.get("fifeUrl", "") or img.get("imageUri", "")
        media_name = item.get("name", "")
        log(f"[flowgen] 生成完成: media_id={media_name}")

        if fife_url:
            # 用 curl 下载（带 cookie + 代理）
            cookies = await aget_cookies()
            if curl_download(fife_url, args.output, cookies):
                log(f"[flowgen] ✅ 图片已保存: {args.output} ({os.path.getsize(args.output)//1024}KB)")
                return 0
            log(f"[flowgen] curl 下载失败，尝试页面内 base64...")
            # fallback: 页面内 fetch 图片转 base64
            js = f"""
            (async () => {{
                try {{
                    const resp = await fetch({json.dumps(fife_url)}, {{credentials: 'include'}});
                    const buf = await resp.arrayBuffer();
                    const bytes = new Uint8Array(buf);
                    let bin = '';
                    for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
                    return JSON.stringify({{ok: true, b64: btoa(bin)}});
                }} catch(e) {{ return JSON.stringify({{ok: false, err: String(e)}}); }}
            }})()
            """
            r = await page._eval(ws, js, timeout=120)
            val = r.get("value", "")
            try:
                jv = json.loads(val)
            except Exception:
                jv = {}
            if jv.get("ok"):
                with open(args.output, "wb") as f:
                    f.write(base64.b64decode(jv["b64"]))
                log(f"[flowgen] ✅ 图片已保存: {args.output} ({os.path.getsize(args.output)//1024}KB)")
                return 0
            else:
                log(f"[flowgen] 页面下载失败: {jv.get('err')}")
        log("[flowgen] 未能下载图片")
        return 1


async def generate_video(args):
    page = Page(get_page_ws())
    async with _connect(page.ws_url, 100*1024*1024) as ws:
        token = await page.get_token(ws)
        if not token:
            raise RuntimeError("登录态失效")
        log(f"[flowgen] token OK ({token[:12]}...)")

        start_media_id = None
        if args.start:
            log("[flowgen] 上传起始图...")
            start_media_id = await page.upload_image(ws, args.start)
            log(f"  media_id={start_media_id}")

        ts = int(time.time() * 1000)
        aspect = VIDEO_ASPECTS.get(args.aspect, "VIDEO_ASPECT_RATIO_LANDSCAPE")
        model_key = f"abra_t2v_{args.duration}s"
        req_item = {
            "aspectRatio": aspect,
            "textInput": {"structuredPrompt": {"parts": [{"text": args.prompt}]}},
            "videoModelKey": model_key,
            "seed": ts % 1000000,
            "metadata": {},
        }
        if start_media_id:
            req_item["startImage"] = {"mediaId": start_media_id}
            endpoint = "/v1/video:batchAsyncGenerateVideoStartImage"
        else:
            endpoint = "/v1/video:batchAsyncGenerateVideoText"
        body = {
            "mediaGenerationContext": {"batchId": f"batch-{ts}"},
            "clientContext": {"projectId": PROJECT_ID, "tool": "PINHOLE",
                              "userPaygateTier": "PAYGATE_TIER_ONE",
                              "sessionId": f";{ts}",
                              "recaptchaContext": {"applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB", "token": ""}},
            "requests": [req_item],
        }
        if start_media_id:
            body["useV2ModelConfig"] = True

        log(f"[flowgen] 生成视频: \"{args.prompt[:60]}\" [{args.aspect}] {args.duration}s start={'Y' if start_media_id else 'N'}")
        result = await page.api(ws, endpoint, body, captcha_action="VIDEO_GENERATION", timeout=300)
        status = result.get("status")
        data = result.get("data", {})
        if status != 200:
            log(f"[flowgen] 提交失败 ({status}): {json.dumps(data, ensure_ascii=False)[:500]}")
            return 1
        media = data.get("media", [])
        if not media:
            log(f"[flowgen] 无 media: {json.dumps(data, ensure_ascii=False)[:400]}")
            return 1
        video_id = media[0].get("name")
        credits = data.get("remainingCredits", "?")
        log(f"[flowgen] 已提交 video_id={video_id} credits剩余={credits}")

        # 轮询
        cookies = await aget_cookies()
        poll_body = {
            "media": [{"name": video_id, "projectId": PROJECT_ID}],
            "clientContext": {"projectId": PROJECT_ID, "tool": "PINHOLE",
                              "userPaygateTier": "PAYGATE_TIER_ONE",
                              "sessionId": f";{int(time.time()*1000)}",
                              "recaptchaContext": {"applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB", "token": ""}},
        }
        log("[flowgen] 轮询生成状态（每 10s）...")
        for attempt in range(int(args.wait / 10)):
            await asyncio.sleep(10)
            r = await page.api(ws, "/v1/video:batchCheckAsyncVideoGenerationStatus", poll_body)
            rd = r.get("data", {})
            media_list = rd.get("media", [])
            if media_list:
                st = media_list[0].get("mediaMetadata", {}).get("mediaStatus", {}).get("mediaGenerationStatus", "")
                log(f"  [{attempt*10+10}s] {st}")
                if st == "MEDIA_GENERATION_STATUS_SUCCESSFUL":
                    log(f"[flowgen] 生成成功，下载中...")
                    if download_video(video_id, args.output, cookies):
                        log(f"[flowgen] ✅ 视频已保存: {args.output} ({os.path.getsize(args.output)//1024}KB)")
                        return 0
                    log("[flowgen] 下载失败")
                    return 1
                if "FAILED" in st or "BLOCKED" in st:
                    log(f"[flowgen] 生成失败: {st}")
                    return 1
        log("[flowgen] 轮询超时")
        return 1


def credits():
    page = Page(get_page_ws())
    async def _run():
        async with _connect(page.ws_url, 50*1024*1024) as ws:
            return await page.api(ws, "/v1/credits", {"clientContext": {"tool": "PINHOLE",
                                                                         "projectId": PROJECT_ID}},
                                  method="GET")
    result = asyncio.run(_run())
    data = result.get("data", {})
    if isinstance(data, dict):
        credits = data.get("credits", data.get("remainingCredits", "?"))
        tier = data.get("userPaygateTier", data.get("paygateTier", ""))
        log(f"[flowgen] credits = {credits}  tier = {tier}")
    else:
        log(f"[flowgen] 响应: {json.dumps(data, ensure_ascii=False)[:300]}")
    return 0


def main():
    parser = argparse.ArgumentParser(prog="flowgen", description="Google Flow 生成 CLI（CDP 直连）")
    parser.add_argument("--project", help="Flow 项目 ID（默认自动探测当前账号项目；切账号后用 flowgen projects 查）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_img = sub.add_parser("image", help="文生图/图生图")
    p_img.add_argument("prompt")
    p_img.add_argument("-o", "--output", default="flowgen_image.png")
    p_img.add_argument("--aspect", choices=list(IMAGE_ASPECTS.keys()), default="landscape")
    p_img.add_argument("--model", choices=list(IMAGE_MODELS.keys()), default="standard")
    p_img.add_argument("--ref", help="参考图路径（图生图）")

    p_vid = sub.add_parser("video", help="文生视频/图生视频")
    p_vid.add_argument("prompt")
    p_vid.add_argument("-o", "--output", default="flowgen_video.mp4")
    p_vid.add_argument("--start", help="起始图路径（图生视频）")
    p_vid.add_argument("--duration", type=int, default=8, choices=[4, 6, 8, 10])
    p_vid.add_argument("--aspect", choices=list(VIDEO_ASPECTS.keys()), default="landscape")
    p_vid.add_argument("--wait", type=int, default=600, help="轮询超时秒数")

    p_cred = sub.add_parser("credits", help="查 credits")
    p_proj = sub.add_parser("projects", help="列出当前账号的所有 Flow 项目")

    args = parser.parse_args()
    if getattr(args, "project", None):
        globals()["PROJECT_ID"] = args.project
    if not PROJECT_ID and args.cmd in ("image", "video", "credits"):
        # 自动探测当前账号第一个项目
        projects = list_projects()
        if projects:
            globals()["PROJECT_ID"] = projects[0].get("projectId", "")
            log(f"[flowgen] 自动探测项目: {PROJECT_ID}")
        else:
            raise RuntimeError("未找到项目，请先访问 labs.google/fx/tools/flow 创建项目，或用 --project 指定")
    try:
        if args.cmd == "image":
            rc = asyncio.run(generate_images(args))
        elif args.cmd == "video":
            rc = asyncio.run(generate_video(args))
        elif args.cmd == "credits":
            rc = credits()
        elif args.cmd == "projects":
            projects = list_projects()
            log(f"[flowgen] 当前账号共 {len(projects)} 个项目：")
            for p in projects:
                info = p.get("projectInfo", {})
                log(f"  {p.get('projectId')}  标题={info.get('projectTitle', '')}")
            rc = 0
        sys.exit(rc)
    except Exception as e:
        log(f"[flowgen] 错误: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
