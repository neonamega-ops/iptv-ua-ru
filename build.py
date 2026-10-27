"""Builds playlist.m3u: Ukrainian + Russian free channels from iptv-org,
drops clearly dead streams, and builds a compact epg.xml.gz
(only our channels, about 3 days) from iptvx.one + epg.pw."""
import concurrent.futures as cf, datetime, gzip, json, re, socket, ssl, tempfile
import urllib.error, urllib.request, xml.etree.ElementTree as ET

PLAYLISTS = [("https://iptv-org.github.io/iptv/countries/ua.m3u", "Україна"),
             ("https://iptv-org.github.io/iptv/countries/ru.m3u", "Россия")]
EPG_SOURCES = ["https://iptvx.one/EPG", "https://epg.pw/xmltv/epg_RU.xml.gz"]
CHANNELS_DB = "https://iptv-org.github.io/api/channels.json"
EPG_URL = "https://raw.githubusercontent.com/neonamega-ops/iptv-ua-ru/main/epg.xml.gz"
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
        return isinstance(e.reason, (socket.gaierror, ConnectionRefusedError))
    except Exception:
        return False


def norm(s):
    s = re.sub(r"\(.*?\)|\[.*?\]", "", s.lower())
    for w in (" hd", " fhd", " uhd", " 4k", "телеканал ", "tv channel"):
        s = s.replace(w, "")
    return re.sub(r"[^a-zа-яёіїєґ0-9+]", "", s.replace("плюс", "+").replace("plus", "+"))


def epg_load(url):
    """Streams an XMLTV source; returns (name->id index, {id: channel xml}, {id: [programme xml]})."""
    now = datetime.datetime.now(datetime.timezone.utc)
    lo, hi = (now - datetime.timedelta(days=1)).strftime("%Y%m%d"), (now + datetime.timedelta(days=3)).strftime("%Y%m%d")
    try:
        tmp = tempfile.TemporaryFile()
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=600, context=CTX) as r:
            while chunk := r.read(1 << 20):
                tmp.write(chunk)
        tmp.seek(0); magic = tmp.read(2); tmp.seek(0)
        fh = gzip.GzipFile(fileobj=tmp) if magic == b"\x1f\x8b" else tmp
        idx, chans, progs = {}, {}, {}
        for _, el in ET.iterparse(fh, events=("end",)):
            if el.tag == "channel":
                cid = el.get("id"); chans[cid] = ET.tostring(el, encoding="unicode").strip()
                for dn in el.findall("display-name"):
                    if dn.text: idx.setdefault(norm(dn.text), cid)
                el.clear()
            elif el.tag == "programme":
                s, e = (el.get("start") or "")[:8], (el.get("stop") or "")[:8]
                if e >= lo and s <= hi:
                    progs.setdefault(el.get("channel"), []).append(ET.tostring(el, encoding="unicode").strip())
                el.clear()
        return {k: v for k, v in idx.items() if v in progs}, chans, progs
    except Exception as e:
        print("EPG failed", url, e)
        return {}, {}, {}


chans = []
for url, group in PLAYLISTS:
    chans += parse(get(url).decode("utf-8"), group)
with cf.ThreadPoolExecutor(48) as ex:
    flags = list(ex.map(dead, chans))
chans = [c for c, d in zip(chans, flags) if not d]

db = {c["id"]: [c["name"]] + c.get("alt_names", []) for c in json.loads(get(CHANNELS_DB))}
sources = [epg_load(u) for u in EPG_SOURCES]
used, bound = {}, 0
for ch in chans:
    m = re.search(r'tvg-id="([^"]*)"', ch[0])
    base = (m.group(1) if m else "").split("@")[0]
    names = [ch[0].rsplit(",", 1)[1]] + db.get(base, [])
    hit = None
    for si, (idx, _, _) in enumerate(sources):
        cid = next((idx[norm(n)] for n in names if norm(n) in idx), None)
        if cid:
            hit = f"s{si}.{cid}"; used[hit] = (si, cid); break
    ch[0] = re.sub(r' ?tvg-id="[^"]*"', "", ch[0])
    if hit:
        ch[0] = ch[0].replace("#EXTINF:-1", f'#EXTINF:-1 tvg-id="{hit}"', 1); bound += 1

with gzip.open("epg.xml.gz", "wt", encoding="utf-8") as g:
    g.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="iptv-ua-ru">\n')
    for new_id, (si, cid) in used.items():
        g.write(sources[si][1][cid].replace(f'id="{cid}"', f'id="{new_id}"', 1) + "\n")
    for new_id, (si, cid) in used.items():
        for p in sources[si][2].get(cid, []):
            g.write(p.replace(f'channel="{cid}"', f'channel="{new_id}"', 1) + "\n")
    g.write("</tv>\n")

with open("playlist.m3u", "w", encoding="utf-8") as f:
    f.write(f'#EXTM3U url-tvg="{EPG_URL}" x-tvg-url="{EPG_URL}"\n')
    for inf, extra, url in chans:
        f.write(inf + "\n" + "".join(e + "\n" for e in extra) + url + "\n")
print(f"channels: {len(chans)} (dropped dead: {sum(flags)}), with EPG: {bound}")
