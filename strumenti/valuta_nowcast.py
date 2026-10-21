#!/usr/bin/env python3
"""Verifica del nowcasting radar su un evento passato: radar fermo contro radar spostato (con e senza sfocatura), FSS a 10 km.

  valuta_nowcast.py scarica 2026-10-20T18 2026-10-21T18 evento.npz     scarica i mosaici SRI ogni 5 minuti (ritaglio di 500 km sul Lazio)
  valuta_nowcast.py valuta evento.npz                                  FSS (soglie 0,5 e 2 mm/h) a +1, +2, +3 ore, un'emissione ogni 30 minuti

Il radar storico del DPC si scarica anche per giorni passati. Serve un evento con pioggia vera: i giorni asciutti non dicono nulla.
Primo collaudo (evento del 20-21/10/2026, una fascia organizzata a circa 44 km/h): radar spostato molto meglio del radar fermo
(FSS +3 h 0,50 contro 0,35); la sfocatura che cresce con la scadenza aggiunge poco. E' UN SOLO evento, il caso piu' favorevole.
"""
import sys
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, __import__("os").path.join(__import__("os").path.dirname(__file__), "..", "zone", "lazio", "wrf"))
import radar  # noqa: E402
from scipy.ndimage import gaussian_filter, uniform_filter  # noqa: E402

LAT0, LON0 = 41.9, 12.7
SOGLIE, NB = (0.5, 2.0), 10


def scarica(ini, fine, out):
    ini = datetime.fromisoformat(ini).replace(tzinfo=timezone.utc)
    fine = datetime.fromisoformat(fine).replace(tzinfo=timezone.utc)
    n = int((fine - ini).total_seconds() / 300) + 1
    ist, fr, orig = radar.serie("SRI", int(fine.timestamp() * 1000), n, ritaglio=(LAT0, LON0, 250))
    h, w = next(f for f in fr if f is not None).shape
    arr = np.stack([f if f is not None else np.full((h, w), np.nan, np.float32) for f in fr])
    np.savez_compressed(out, ist=np.array(ist), R=arr.astype(np.float16), orig=np.array(orig))
    print(n, "mosaici,", sum(f is not None for f in fr), "presenti")


def valuta(percorso):
    d = np.load(percorso)
    R = d["R"].astype(np.float32)
    N = len(R)
    acc={}   # (variante,lead,soglia)->[num,den]
    vel=[]
    def add(var,j,s,fc,tr,mask):
        fp=uniform_filter(((fc>=s)&mask).astype(float),NB); fo=uniform_filter(((tr>=s)&mask).astype(float),NB)
        a=acc.setdefault((var,j,s),[0.0,0.0,0.0,0.0]); a[0]+=((fp-fo)**2).sum(); a[1]+=(fp**2).sum()+(fo**2).sum()
        a[2]+=(fc*mask).sum(); a[3]+=(tr*mask).sum()
    for i in range(6,N-36,6):          # un'emissione ogni 30 minuti
        R0=R[i]; 
        if not np.isfinite(R[i-6]).any() or not np.isfinite(R0).any(): continue
        R0z=np.nan_to_num(R0)
        tr_ok=True
        truth=[]
        for j in (1,2,3):
            fr=R[i+12*(j-1)+1:i+12*j+1]
            if np.isnan(fr).all(axis=(1,2)).any(): tr_ok=False;break
            truth.append(np.nanmean(np.where(np.isfinite(fr),fr,np.nan),axis=0))
        if not tr_ok: continue
        if R0z.max()<0.5 and max(np.nan_to_num(t).max() for t in truth)<0.5: continue
        v,_=radar.movimento(R[i-6],R0,6); v0,_=radar.movimento(R[i-6],R0,6,vel_min=0.0)
        vel.append(np.hypot(*v)*12)
        for j in (1,2,3):
            tr=np.nan_to_num(truth[j-1]); mask=np.isfinite(truth[j-1])
            ks=range(12*(j-1)+1,12*j+1)
            fl=np.broadcast_to(v,R0.shape+(2,)).copy(); fl0=np.broadcast_to(v0,R0.shape+(2,)).copy()
            ex=np.mean([radar.estrapola(R0z,fl,k) for k in ks],axis=0)
            ex0=np.mean([radar.estrapola(R0z,fl0,k) for k in ks],axis=0)
            for s in SOGLIE:
                add('fermo',j,s,R0z,tr,mask); add('estrapolato',j,s,ex,tr,mask); add('estr_vel0',j,s,ex0,tr,mask)
                add('estr+sfoca',j,s,gaussian_filter(ex0,4*j),tr,mask)
    print("emissioni valutate:",len(vel)," velocita' mediana km/h:",np.median(vel))
    for s in SOGLIE:
        print(f"\nFSS (10 km) soglia {s} mm/h")
        for var in ('fermo','estrapolato','estr_vel0','estr+sfoca'):
            print(f"{var:12s}",*[f"+{j}h {1-acc[(var,j,s)][0]/acc[(var,j,s)][1]:.3f}" for j in (1,2,3)], "| bias",*[f"{acc[(var,j,s)][2]/max(acc[(var,j,s)][3],1e-9):.2f}" for j in (1,2,3)])


if __name__ == "__main__":
    if sys.argv[1] == "scarica":
        scarica(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        valuta(sys.argv[2])
