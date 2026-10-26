import numpy as np, subprocess
sr=16000
out = subprocess.run(["ffmpeg","-v","quiet","-i","_full.mp3","-f","s16le","-ac","1","-ar",str(sr),"-"], capture_output=True)
x = np.frombuffer(out.stdout, dtype=np.int16).astype(np.float32)/32768.0
print("sec:", round(len(x)/sr,1))
def bc(seg, sr, nfft=2048, hop=512):
    frames=[seg[i:i+nfft]*np.hanning(nfft) for i in range(0,len(seg)-nfft,hop)]
    if len(frames)<10: return 0
    S=np.abs(np.fft.rfft(np.array(frames),axis=1))
    f=np.fft.rfftfreq(nfft,1/sr)
    env=S[:,(f>20)&(f<200)].sum(axis=1); env=env-env.mean()
    ac=np.correlate(env,env,mode="full")[len(env)-1:]; ac/=(ac[0]+1e-9)
    fps=sr/hop; lo,hi=int(fps*60/180),int(fps*60/60)
    return float(ac[lo:hi].max()) if hi>lo+2 else 0
step=int(30*sr)
print(f"{'t':>6} {'RMS':>7} {'low':>6} {'beat':>6}")
rows=[]
for i in range(0, len(x)//step):
    seg=x[i*step:(i+1)*step]
    db=20*np.log10(np.sqrt(np.mean(seg**2))+1e-9)
    S=np.abs(np.fft.rfft(seg)); f=np.fft.rfftfreq(len(seg),1/sr)
    lr=S[(f>20)&(f<250)].sum()/(S.sum()+1e-9)
    b=bc(seg,sr); rows.append((b,i*30))
    print(f"{i*30:6d} {db:7.1f} {lr:6.3f} {b:6.3f}")
rows.sort(reverse=True)
print("\n音乐性最强:", [(int(t),round(c,2)) for c,t in rows[:8]])
