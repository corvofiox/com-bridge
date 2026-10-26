import asyncio, json
from shazamio import Shazam
async def main():
    sh = Shazam()
    for s in [890,900,910]:
        try:
            r = await sh.recognize(f"_c{s}.wav")
            tr = r.get("track") or {}
            m = (r.get("matches") or [{}])[0]
            print(json.dumps({"t": s, "title": tr.get("title"), "artist": tr.get("subtitle"), "off": round(m.get("offset",0),1)}, ensure_ascii=False), flush=True)
        except Exception as e:
            print(json.dumps({"t": s, "error": repr(e)[:60]}, ensure_ascii=False), flush=True)
asyncio.run(main())
