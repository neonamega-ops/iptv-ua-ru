"""Builds playlist.m3u: Ukrainian + Russian free channels from iptv-org,
drops clearly dead streams, binds EPG ids (iptvx.one, epg.pw)."""
import concurrent.futures as cf, gzip, json, re, socket, ssl, urllib.error, urllib.request

PLAYLISTS = [("https://iptv-org.github.io/iptv/countries/ua.m3u", "Україна"),
             ("https://iptv-org.github.io/iptv/countries/ru.m3u", "Россия")]
EPG = ["https://iptvx.one/EPG", "https://epg.pw/xmltv/epg_RU.xml.gz"]
CHANNELS_DB = "https://iptv-org.github.io/api/channels.json"
CTX = ssl.create_default_context(); CTX.check_hostname = False; CTX.verify_mode = ssl.CERT_NONE

def get(url, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    data = urllib.request.urlopen(req, timeout=timeout, context=CTX).read()
    return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data

def parse(text, group):
    out, lines, i = [], text.splitlines(), 0
    while i < len(lines):
        if lines[i].startswith("#EXTINF"):
            extra, j = [], i + 1
            while j < len(lines) and lines[j].startswith("#"):
                extra.append(lines[j]); j += 1
            if j < len(lines) and lines[j].strip():
                inf = re.sub(r' ?group-title="[^"]*"', "", lines[i])
                inf = inf.replace("#EXTINF:-1", f'#EXTINF:-1 group-title="{group}"', 1)
                out.append([inf, extra, lines[j].strip()])
            i = j + 1
        else:
            i += 1
    return out

def dead(ch):
    """True only for clearly dead links; geo-blocks and timeouts are kept."""
    _, extra, url = ch
    hdrs = {"User-Agent": "Mozilla/5.0"}
    for e in extra:
        if e.startswith("#EXTVLCOPT:http-user-agent="): hdrs["User-Agent"] = e.split("=", 1)[1]
        if e.startswith("#EXTVLCOPT:http-referrer="): hdrs["Referer"] = e.split("=", 1)[1]
    try:
        urllib.request.urlopen(urllib.request.Request(url, headers=hdrs), timeout=15, context=CTX).read(512)
        return False
    except urllib.error.HTTPError as e:
        return e.code in (404, 410)
    except urllib.error.URLError as e:
        return isinstance(e.reason, socket.gaierror) or isinstance(e.reason, ConnectionRefusedError)
    except Exception:
        return False

def norm(s):
    s = re.sub(r"\(.*?\)|\[.*?\]", "", s.lower())
    for w in (" hd", " fhd", " uhd", " 4k", "телеканал ", "tv channel"): s = s.replace(w, "")
    return re.sub(r"[^a-zа-яёіїєґ0-9+]", "", s.replace("плюс", "+").replace("plus", "+"))

def epg_index(url):
    try: t = get(url, 300).decode("utf-8", "ignore")
    except Exception as e:
        print("EPG failed", url, e); return {}
    have = set(re.findall(r'<programme[^>]*channel="([^"]+)"', t))
    idx = {}
    for cid, body in re.findall(r'<channel id="([^"]+)">(.*?)</channel>', t, re.S):
        if cid in have:
            for n in re.findall(r"<display-name[^>]*>([^<]+)</display-name>", body): idx.setdefault(norm(n), cid)
    return idx

chans = []
for url, group in PLAYLISTS: chans += parse(get(url).decode("utf-8"), group)
with cf.ThreadPoolExecutor(48) as ex: flags = list(ex.map(dead, chans))
chans = [c for c, d in zip(chans, flags) if not d]

db = {c["id"]: [c["name"]] + c.get("alt_names", []) for c in json.loads(get(CHANNELS_DB))}
indexes = [epg_index(u) for u in EPG]
used, bound = set(), 0
for ch in chans:
    m = re.search(r'tvg-id="([^"]*)"', ch[0]); base = (m.group(1) if m else "").split("@")[0]
    names = [ch[0].rsplit(",", 1)[1]] + db.get(base, [])
    for si, idx in enumerate(indexes):
        hit = next((idx[norm(n)] for n in names if norm(n) in idx), None)
        if hit:
            ch[0] = re.sub(r'tvg-id="[^"]*"', f'tvg-id="{hit}"', ch[0]) if m else ch[0].replace("#EXTINF:-1", f'#EXTINF:-1 tvg-id="{hit}"', 1)
            used.add(si); bound += 1; break

epg = ",".join(EPG[i] for i in sorted(used))
with open("playlist.m3u", "w", encoding="utf-8") as f:
    f.write(f'#EXTM3U url-tvg="{epg}" x-tvg-url="{epg}"\n')
    for inf, extra, url in chans: f.write(inf + "\n" + "".join(e + "\n" for e in extra) + url + "\n")
print(f"channels: {len(chans)} (dropped dead: {sum(flags)}), with EPG: {bound}")
