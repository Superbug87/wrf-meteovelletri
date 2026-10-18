#!/usr/bin/env python3
"""
Collaudo senza rete di strumenti/stazioni_ufficiali.py (lo lancia wrf/prove.sh di ogni zona).

  prova_ufficiali.py --wrf wrf --geo CARTELLA_GEO --ciclo 2026101600 --out CARTELLA

Stazioni ufficiali e Wunderground inventate sui domini (anche finti) della zona, con guasti voluti:
  - un termometro ufficiale con un picco di 15 gradi (il valore deve sparire, la stazione restare);
  - un termometro ufficiale bloccato sullo stesso valore (la stazione deve sparire);
  - dieci stazioni WU che segnano 9 gradi in piu' (devono sparire quasi tutte, confrontate anche con le ufficiali;
    la soglia cresce con la distanza delle vicine fino a 8 gradi, e sul Lazio le stazioni inventate sono rade).
Poi controlla che le ufficiali non vengano toccate dal confronto con le WU, che le superosservazioni siano meno delle
stazioni WU, e che i file scritti abbiano la temperatura riportata alla quota del terreno di WRF.
I file OBS_DOMAIN* li rilegge poi prove.sh con i formati di WRF (leggi_obs.f90).
"""
import argparse
import copy
import sys
from datetime import timedelta, datetime, timezone
from pathlib import Path

import numpy as np

ap = argparse.ArgumentParser()
ap.add_argument("--wrf", required=True)
ap.add_argument("--geo", required=True)
ap.add_argument("--ciclo", required=True)
ap.add_argument("--out", required=True)
a = ap.parse_args()

sys.path.insert(0, str(Path(a.wrf).resolve()))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import stazioni_wrf as Z  # noqa: E402
import stazioni_ufficiali as SU  # noqa: E402

out = Path(a.out)
out.mkdir(parents=True, exist_ok=True)
domini = Z.carica_domini(a.geo)
t0 = datetime.strptime(a.ciclo, "%Y%m%d%H").replace(tzinfo=timezone.utc)
rng = np.random.default_rng(3)


def vero(ist, quota):
    ora = ist.hour + ist.minute / 60
    return 14.0 + 4.0 * np.sin(2 * np.pi * (ora - 9) / 24) - 0.0065 * quota


def punto(dom):
    j, k = rng.integers(10, dom.lat.shape[0] - 10, 2)
    return float(dom.lat[j, k]), float(dom.lon[j, k]), float(dom.hgt[j, k])


uff = {}
for i in range(42):
    n = 3 if i < 30 else 2
    la, lo, terreno = punto(domini[n])
    quota = max(terreno + float(rng.normal(0, 30)), 0.0)
    am = i % 7 == 0
    passo = 60 if am else 15
    blocchi = {}
    for m in range(0, 8 * 60, passo):
        ist = t0 + timedelta(minutes=m)
        T = vero(ist, quota) + float(rng.normal(0, 0.3))
        td = T - 3.0
        rh = SU.ur_da_rugiada(T, td) if am or i % 3 == 0 else None
        blocchi[m // SU.PASSO_MIN] = (ist, T, rh, 1017.0 if am else None)
    sid = f"WMO{16000 + i}" if am else f"RL{1000 + i}"
    uff[sid] = {"lat": la, "lon": lo, "quota": quota, "terreno": domini[SU.dominio_fine(domini, la, lo)].quota(la, lo),
                "dominio": n, "fonte": SU.FONTE_AM if am else SU.FONTE_REGIONE, "nome": sid, "blocchi": blocchi}
guasto, bloccato = "RL1001", "RL1002"
ist, T, rh, p = uff[guasto]["blocchi"][10]
uff[guasto]["blocchi"][10] = (ist, T + 15.0, rh, p)
uff[bloccato]["blocchi"] = {k: (v[0], 12.3, v[2], v[3]) for k, v in uff[bloccato]["blocchi"].items()}

el, sc = SU.controlla_ufficiali(uff, Z.LAT0, Z.LON0)
assert bloccato not in uff, "il termometro bloccato doveva sparire"
assert guasto in uff and uff[guasto]["blocchi"][10][1] is None, "il picco di 15 gradi doveva sparire"
buoni = sum(v[1] is not None for s in uff.values() for v in s["blocchi"].values())
assert sc <= 3, f"troppi valori buoni scartati tra le ufficiali: {sc}"
print(f"ufficiali: {len(uff)} restano, eliminate {el}, valori scartati {sc} (attesi: 1 stazione, 1 valore)")

wu = {}
for i in range(220):
    la, lo, terreno = punto(domini[3])
    quota = terreno + float(rng.normal(0, 20))
    difettosa = i < 10
    blocchi = {}
    for b in range(8 * 60 // SU.PASSO_MIN):
        ist = t0 + timedelta(minutes=SU.PASSO_MIN * b + 7)
        T = vero(ist, quota) + float(rng.normal(0, 0.6)) + (9.0 if difettosa else 0.0)
        blocchi[b] = (ist, T, float(np.clip(70 + rng.normal(0, 5), 20, 99)), None)
    wu[f"IWU{i:03d}"] = {"lat": la, "lon": lo, "quota": quota, "dominio": 3, "blocchi": blocchi}

prima = copy.deepcopy(uff)
tutte = {**{f"W:{k}": v for k, v in wu.items()}, **uff}
el, sc = SU.controlla(tutte, Z.LAT0, Z.LON0, fisse=set(uff))
wu_ok = {k[2:]: v for k, v in tutte.items() if k.startswith("W:")}
for sid in prima:
    assert tutte[sid]["blocchi"] == prima[sid]["blocchi"], f"il confronto con le WU ha toccato la stazione ufficiale {sid}"
rimaste = [s for s in wu_ok if int(s[3:]) < 10]
assert len(rimaste) <= 2, f"stazioni WU con +9 gradi rimaste: {rimaste}"
assert len(wu_ok) >= 190, f"troppe stazioni WU buone eliminate: ne restano {len(wu_ok)}"
print(f"Wunderground: {len(wu_ok)} restano su {len(wu)} (difettose rimaste {len(rimaste)} su 10)")

so, saltate = SU.superosservazioni(wu_ok, domini, uff, Z.CELLE_KM, Z.LAT0, Z.LON0)
assert 0 < len(so) < len(wu_ok), (len(so), len(wu_ok))
assert sum(s["membri"] for s in so.values()) + saltate == len(wu_ok)
print(f"superosservazioni: {len(so)} da {len(wu_ok)} stazioni WU ({saltate} saltate perche' accanto a un'ufficiale)")

stazioni = {**uff, **so}
n = SU.scrivi(stazioni, domini, t0, out, Z.RISOLUZIONI, Z.TOLLERANZA_QUOTA)
assert n > 0
fine = int((out / "obs_fdda_end.txt").read_text())
ultimo = max(v[0] for s in stazioni.values() for v in s["blocchi"].values() if v[1] is not None)
assert fine == int((ultimo - t0).total_seconds() // 60), fine
fonti = dict(x.split("=") for x in (out / "obs_fonti.txt").read_text().split())
assert set(fonti) <= {"regione", "aeronautica", "wu"} and "regione" in fonti and "wu" in fonti, fonti

# temperatura riportata al terreno del dominio fine: prima osservazione di una stazione regionale nel file del dominio 3
righe = (out / "OBS_DOMAIN301").read_text().splitlines()
sid = next(s for s in uff if s.startswith("RL") and uff[s]["dominio"] == 3 and s != guasto)
for i in range(0, len(righe), 5):
    if righe[i + 2].split()[0] == sid:
        ist = datetime.strptime(righe[i].strip(), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        k = int((ist - t0).total_seconds() // (SU.PASSO_MIN * 60))
        s = uff[sid]
        atteso = s["blocchi"][k][1] + 0.0065 * (s["quota"] - domini[3].quota(s["lat"], s["lon"])) + 273.15
        scritto = float(righe[i + 4].split()[6])
        quota_scritta = float(righe[i + 3].split()[-4])
        assert abs(scritto - atteso) < 0.002, (scritto, atteso)
        assert abs(quota_scritta - round(domini[3].quota(s["lat"], s["lon"]), 1)) < 0.11, quota_scritta
        break
else:
    raise AssertionError(f"{sid} non trovata in OBS_DOMAIN301")
print(f"file scritti: {n} osservazioni, nudging fino a +{fine} min, fonti {fonti}; temperatura riportata al terreno ok")
print("STAZIONI UFFICIALI OK")
