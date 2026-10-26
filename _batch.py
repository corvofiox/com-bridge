import subprocess, os, asyncio, json
from audio_separator.separator import Separator
from shazamio import Shazam

chunks = [(0,70),(200,70),(440,70),(680,70),(860,70),(907,70)]
os.makedirs("chunks", exist_ok=True)
sep = Separator(model_file_dir="models", output_dir="sep_out", output_format="WAV")
sep.load_model(model_filename="UVR-MDX-NET-Inst_HQ_5.onnx")

async def main():
    sh = Shazam()
    for ss, t in chunks:
        src = f"chunks/c{ss}.wav"
        subprocess.run(["ffmpeg","-v","quiet","-ss",str(ss),"-t",str(t),"-i","_full.mp3",
                        "-ac","1","-ar","44100","-c:a","pcm_s16le",src,"-y"], check=True)
        outs = sep.separate(src)
        inst = [o for o in outs if "Instrumental" in o]
        if not inst:
            print(json.dumps({"chunk": ss, "err": "no inst", "outs": outs}, ensure_ascii=False), flush=True); continue
        path = os.path.join("sep_out", os.path.basename(inst[0]))
        for off in (5, 25, 45):
            seg = f"chunks/s_{ss}_{off}.wav"
            subprocess.run(["ffmpeg","-v","quiet","-ss",str(off),"-t","14","-i",path,
                            "-af","loudnorm=I=-12:TP=-1","-ac","1","-ar","44100",seg,"-y"], check=True)
            try:
                r = await sh.recognize(seg)
                tr = r.get("track") or {}
                m = (r.get("matches") or [{}])[0]
                print(json.dumps({"at": ss+off, "title": tr.get("title"), "artist": tr.get("subtitle"),
                                  "off": round(m.get("offset",0),1),
                                  "genre": (tr.get("genres") or {}).get("primary")}, ensure_ascii=False), flush=True)
            except Exception as e:
                print(json.dumps({"at": ss+off, "error": repr(e)[:60]}, ensure_ascii=False), flush=True)

asyncio.run(main())
