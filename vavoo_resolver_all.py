import json
import gzip
import time
import uuid
import ssl
import os
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor, as_completed

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", 8765))

PING_URLS = [
    "https://www.vypn.net/api/app/ping",
    "https://cache.vypn.net/api/app/ping",
]
BASE_SITES = ["https://huhu.to", "https://www.huhu.to"]
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")
MEDIAURL_UA = "MediaUrl/2"

HERE = os.path.dirname(os.path.abspath(__file__))
CATALOG_FILE = os.path.join(HERE, "catalog_cache.json")
sig_cache = {"sig": None, "ts": 0}
ssl_ctx = ssl.create_default_context()

ONLINE_CACHE_TTL = 1800  # 30 minuta
ONLINE_CACHE = {"ts": 0, "channels": []}

def post_json(url, payload, headers, timeout=30):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding", "").lower() == "gzip":
            raw = gzip.decompress(raw)
        return r.status, json.loads(raw.decode("utf-8", errors="replace"))

def get_sig(force=False):
    if not force and sig_cache["sig"] and time.time() - sig_cache["ts"] < 480:
        return sig_cache["sig"]
    uid = str(uuid.uuid4())
    ts = int(time.time() * 1000)
    payload = {
        "reason": "app-focus", "locale": "en", "theme": "dark",
        "metadata": {
            "device": {"type": "desktop", "uniqueId": uid},
            "os": {"name": "win32", "version": "Windows 10 Pro", "abis": ["x64"], "host": "Lenovo"},
            "app": {"platform": "electron"},
            "version": {"package": "net.vypn.app", "binary": "3.1.0", "js": "3.1.0"},
        },
        "appFocusTime": 0, "playerActive": False, "playDuration": 0,
        "devMode": False, "hasAddon": True, "castConnected": False,
        "package": "net.vypn.app", "version": "3.1.0", "process": "app",
        "firstAppStart": ts, "lastAppStart": ts, "ipLocation": None,
        "adblockEnabled": True,
        "proxy": {"supported": ["ss"], "engine": "Mu", "enabled": False, "autoServer": True},
        "iap": {"supported": False},
    }
    headers = {
        "accept": "*/*", "user-agent": BROWSER_UA,
        "Accept-Encoding": "gzip, deflate", "Connection": "close",
        "Content-Type": "application/json",
    }
    last_error = None
    for url in PING_URLS:
        try:
            _, obj = post_json(url, payload, headers, 15)
            sig = obj.get("addonSig") or obj.get("sig") or obj.get("token")
            if sig:
                sig_cache["sig"] = sig
                sig_cache["ts"] = time.time()
                return sig
        except Exception as e:
            last_error = e
    raise RuntimeError("Signature error: %s" % last_error)

def resolve_stream(channel_url):
    last_error = None
    for attempt in range(2):
        sig = get_sig(force=(attempt == 1))
        headers = {
            "content-type": "application/json; charset=utf-8",
            "mediaurl-signature": sig or "", "user-agent": MEDIAURL_UA,
            "accept": "*/*", "Accept-Language": "en",
            "Accept-Encoding": "gzip, deflate", "Connection": "close",
        }
        payload = {"language": "de", "region": "DE", "url": channel_url, "clientVersion": "3.1.0"}
        for base in BASE_SITES:
            try:
                _, result = post_json(base + "/mediaurl-resolve.json", payload, headers, 30)
                stream = None
                if isinstance(result, list) and result:
                    stream = result[0].get("url")
                elif isinstance(result, dict):
                    stream = result.get("url") or result.get("streamUrl")
                if stream:
                    return stream
            except Exception as e:
                last_error = e
    raise RuntimeError("Resolve error: %s" % last_error)

def load_channels():
    with open(CATALOG_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    rows = data.get("channels", data if isinstance(data, list) else [])
    out = {}
    for ch in rows:
        cid = str(ch.get("id", "")).strip()
        url = str(ch.get("url", "")).strip()
        if cid and url:
            out[cid] = ch
    return out

CHANNELS = load_channels()

def esc(v):
    return str(v or "").replace('"', "'").replace("\r", " ").replace("\n", " ")

def playlist():
    lines = ["#EXTM3U"]
    for cid, ch in CHANNELS.items():
        name = esc(ch.get("name") or cid)
        group = esc(ch.get("group") or ch.get("country") or "Other")
        logo = esc(ch.get("logo") or "")
        lines.append('#EXTINF:-1 tvg-name="%s" tvg-logo="%s" group-title="%s",%s' % (name, logo, group, name))
        lines.append("https://vavoo-online-resolver.onrender.com/play/%s" % urllib.parse.quote(cid, safe=""))
    return ("\n".join(lines) + "\n").encode("utf-8")

def stream_looks_online(stream_url, timeout=8):
    """
    Kontroll i lehte i stream-it pa shkarkuar videon e plote.
    HLS (.m3u8) konsiderohet online nese serveri jep pergjigje HTTP te vlefshme.
    """
    headers = {
        "User-Agent": BROWSER_UA,
        "Accept": "*/*",
        "Connection": "close",
    }

    # Provo fillimisht HEAD.
    try:
        req = urllib.request.Request(stream_url, headers=headers, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx) as r:
            if 200 <= r.status < 400:
                return True
    except Exception:
        pass

    # Disa servera HLS nuk pranojne HEAD, prandaj provojme GET te vogel.
    try:
        headers["Range"] = "bytes=0-2047"
        req = urllib.request.Request(stream_url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx) as r:
            if 200 <= r.status < 400:
                r.read(2048)
                return True
    except Exception:
        return False

    return False


def check_channel_online(item):
    cid, ch = item
    try:
        stream = resolve_stream(ch["url"])
        if stream and stream_looks_online(stream):
            return cid, ch
    except Exception as e:
        print("OFFLINE:", ch.get("name", cid), "-", e, flush=True)
    return None


def get_online_albania_channels():
    now = time.time()

    # Per 30 minuta perdor rezultatin e ruajtur, qe lista te hapet shpejt.
    if ONLINE_CACHE["channels"] and now - ONLINE_CACHE["ts"] < ONLINE_CACHE_TTL:
        return ONLINE_CACHE["channels"]

    candidates = []
    seen = set()

    for cid, ch in CHANNELS.items():
        group_raw = str(ch.get("group") or "").strip()
        country_raw = str(ch.get("country") or "").strip()
        group = group_raw.lower()
        country = country_raw.lower()

        # Kontrollo te dy fushat veçmas, qe te mos humbasim kanale shqiptare
        # qe mund te jene ne nje grup tjeter, por country=Albania/Kosovo.
        if group not in ("albania", "kosovo") and country not in ("albania", "kosovo"):
            continue

        name = esc(ch.get("name") or cid)
        key = name.casefold().strip()

        if key in seen:
            continue

        seen.add(key)
        candidates.append((cid, ch))

    print("Po kontrolloj kanalet Albania + Kosovo:", len(candidates), flush=True)

    online = []

    # Kontroll paralel qe te mos presim kanal pas kanali.
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(check_channel_online, item) for item in candidates]

        for future in as_completed(futures):
            result = future.result()
            if result:
                online.append(result)

    # Ruaj renditjen origjinale te katalogut.
    order = {cid: i for i, (cid, _) in enumerate(candidates)}
    online.sort(key=lambda item: order.get(item[0], 999999))

    ONLINE_CACHE["channels"] = online
    ONLINE_CACHE["ts"] = time.time()

    print("Kanale ONLINE:", len(online), flush=True)
    return online


def playlist_albania():
    # Playlist hapet menjehere. Kontrolli i stream-it behet kur hapet kanali
    # nga /play/<id>, jo kur ngarkohet e gjithe lista.
    lines = ["#EXTM3U"]
    seen = set()

    for cid, ch in CHANNELS.items():
        group_raw = str(ch.get("group") or "").strip()
        country_raw = str(ch.get("country") or "").strip()
        group_lower = group_raw.lower()
        country_lower = country_raw.lower()

        if group_lower not in ("albania", "kosovo") and country_lower not in ("albania", "kosovo"):
            continue

        name = esc(ch.get("name") or cid)
        dedupe_key = name.casefold().strip()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        group = esc(ch.get("group") or ch.get("country") or "Albania")
        logo = esc(ch.get("logo") or "")

        lines.append(
            '#EXTINF:-1 tvg-name="%s" tvg-logo="%s" group-title="%s",%s'
            % (name, logo, group, name)
        )
        lines.append(
            "https://vavoo-online-resolver.onrender.com/play/%s"
            % urllib.parse.quote(cid, safe="")
        )

    return ("\\n".join(lines) + "\\n").encode("utf-8")

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[HTTP]", fmt % args, flush=True)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path

        if path in ("/", "/playlist.m3u"):
            body = playlist()
            self.send_response(200)
            self.send_header("Content-Type", "audio/x-mpegurl; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/albania.m3u":
            body = playlist_albania()
            self.send_response(200)
            self.send_header("Content-Type", "audio/x-mpegurl; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if path.startswith("/play/"):
            cid = urllib.parse.unquote(path[len("/play/"):])
            ch = CHANNELS.get(cid)
            if not ch:
                self.send_error(404, "Channel not found")
                return
            try:
                print("Po krijoj link te fresket per:", ch.get("name", cid), flush=True)
                stream = resolve_stream(ch["url"])
                print("OK ->", stream, flush=True)
                self.send_response(302)
                self.send_header("Location", stream)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
            except Exception as e:
                msg = ("ERROR: " + str(e)).encode("utf-8", errors="replace")
                print(msg.decode("utf-8", errors="replace"), flush=True)
                self.send_response(502)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
            return

        self.send_error(404)

if __name__ == "__main__":
    print("=" * 62)
    print("VAVOO Online Resolver - ALL CHANNELS")
    print("Kanale ne catalog:", len(CHANNELS))
    print("Playlist: https://vavoo-online-resolver.onrender.com/playlist.m3u")
    print("Albania + Kosovo: https://vavoo-online-resolver.onrender.com/albania.m3u")
    print("=" * 62)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
