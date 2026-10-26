import asyncio, json, re, glob
from shazamio import Shazam
def key(p): return int(re.search(r"_r(\d+)\.wav", p).group(1))
async def main():
    sh = Shazam()
    for f in sorted(glob.glob("_r[0-9]*.wav"), key=key):
        t = key(f)
        try:
            r = await sh.recognize(f)
            tr = r.get("track") or {}
            m = (r.get("matches") or [{}])[0]
            print(json.dumps({"t": t, "title": tr.get("title"), "artist": tr.get("subtitle"), "off": round(m.get("offset",0),1)}, ensure_ascii=False), flush=True)
        except Exception as e:
            print(json.dumps({"t": t, "error": repr(e)[:60]}, ensure_ascii=False), flush=True)
asyncio.run(main())
