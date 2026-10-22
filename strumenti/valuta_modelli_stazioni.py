#!/usr/bin/env python3
"""Confronto con le stazioni di TUTTE le versioni di WRF per scadenza: temperatura, umidita', vento medio.

  valuta_modelli_stazioni.py --estratti CARTELLA --giorni 5 --out CARTELLA_OUT [--zona lazio]

Osservazioni come nella verifica giornaliera (strumenti/verifica.py): rete della Regione Lazio e SYNOP dell'Aeronautica.
Per ogni versione e scadenza (gruppi di 3 ore, da +1 a +48) si calcolano errore medio (bias) ed errore assoluto medio.
La temperatura del modello e' riportata alla quota della stazione (0,65 K ogni 100 m), come nella pagella.
L'umidita' relativa del modello si ricava da temperatura e punto di rugiada a 2 m (Magnus); nelle osservazioni viene dalla
rete della Regione (34 stazioni) e, per i SYNOP, da temperatura e punto di rugiada.
La raffica non c'e': gli estratti non la contengono e la rete della Regione non la pubblica.
"""
import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

QUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, QUI)
import verifica as V  # noqa: E402

BIN = 3
VARS = {"t2": ("Temperatura a 2 m", "°C"), "ur": ("Umidità relativa a 2 m", "%"), "vento": ("Vento medio a 10 m", "m/s"),
        "pioggia": ("Pioggia in 3 ore ai pluviometri", "mm"), "csi": ("Pioggia ≥ 1 mm in 3 ore ai pluviometri: indice di successo CSI", "")}


def ur_magnus(t, td):
    a, b = 17.625, 243.04
    return 100 * np.exp(a * td / (b + td) - a * t / (b + t))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--estratti", required=True)
    ap.add_argument("--giorni", type=int, default=5)
    ap.add_argument("--zona", default="lazio", choices=["lazio", "castelli"])
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    adesso = datetime.now(timezone.utc)
    files = {}
    for f in glob.glob(os.path.join(a.estratti, f"estratto_{a.zona}-*.npz")):
        m = re.match(rf"estratto_{a.zona}-(ecmwf|icon2i|iconeu|gfs)(?:-(.+))?_(\d{{10}})\.npz$", os.path.basename(f))
        if not m:
            continue
        c = datetime.strptime(m.group(3), "%Y%m%d%H").replace(tzinfo=timezone.utc)
        if adesso - c > timedelta(days=a.giorni) or c + timedelta(hours=48) > adesso - timedelta(hours=1):
            continue
        files[(m.group(1), m.group(2) or "", m.group(3))] = f
    st = json.load(open(os.path.join(QUI, "stazioni_verifica.json")))
    quote = {s["id"]: float(s["quota"]) for s in st["regione"] + st["aeronautica"] if s.get("quota") is not None}
    dati = {k: np.load(f, allow_pickle=True) for k, f in files.items()}
    servono = {str(x) for d in dati.values() for x in d["stazioni"]}
    giorni = set()
    for (_, _, c) in files:
        t = datetime.strptime(c, "%Y%m%d%H").replace(tzinfo=timezone.utc)
        for h in range(0, 49, 6):
            giorni.add((t + timedelta(hours=h)).date())
    import pickle
    cache = os.path.join(a.out, "osservazioni.pkl")
    oss = {}
    mancanti = sorted(giorni)
    if os.path.exists(cache):
        c = pickle.load(open(cache, "rb"))
        oss, fatti = c["oss"], c["giorni"]
        mancanti = [g for g in sorted(giorni) if g not in fatti]
        print("osservazioni dalla copia locale, da scaricare:", len(mancanti), "giorni", flush=True)
    for g in mancanti:
        V.GIORNO = g
        for k, v in V.osservazioni_regione(st["regione"], g, servono).items():
            for var, serie in v.items():
                oss.setdefault(k, {}).setdefault(var, {}).update(serie)
        for k, v in V.osservazioni_synop(st["aeronautica"], g).items():
            for var, serie in v.items():
                oss.setdefault(k, {}).setdefault(var, {}).update(serie)
    pickle.dump({"oss": oss, "giorni": set(giorni) | (c["giorni"] if os.path.exists(cache) and "c" in dir() else set())}, open(cache, "wb"))
    for k, o in oss.items():                                   # umidita' dei SYNOP da T e rugiada
        if "ur" not in o and "td2" in o and "t2" in o:
            o["ur"] = {t: float(ur_magnus(o["t2"][t], td)) for t, td in o["td2"].items() if t in o["t2"]}
    acc = {}
    for (pil, var, c), d in sorted(dati.items()):
        T0 = datetime.strptime(c, "%Y%m%d%H").replace(tzinfo=timezone.utc)
        ore = [int(h) for h in d["ore"]]
        libera = max(int(d["fine_nudging"]), 0) / 60 if "fine_nudging" in d.files else 0
        for j, sid in enumerate(str(x) for x in d["stazioni"]):
            o = oss.get(sid)
            if not o:
                continue
            dz = float(d["quota_modello"][j]) - quote.get(sid, float(d["quota_modello"][j]))
            for k, h in enumerate(ore):
                if h < 1 or h <= libera:
                    continue
                t = T0 + timedelta(hours=h)
                b = (h - 1) // BIN
                tm, tdm = float(d["t2"][k, j]), float(d["td2"][k, j])
                coppie = {}
                if t in o.get("t2", {}) and -40 < o["t2"][t] < 50 and np.isfinite(tm):
                    coppie["t2"] = (tm + 0.0065 * dz, o["t2"][t])
                if t in o.get("ur", {}) and 0 <= o["ur"][t] <= 100 and np.isfinite(tm) and np.isfinite(tdm):
                    coppie["ur"] = (float(min(100, ur_magnus(tm, tdm))), o["ur"][t])
                if t in o.get("vento", {}) and 0 <= o["vento"][t] < 60 and np.isfinite(d["vento"][k, j]):
                    coppie["vento"] = (float(d["vento"][k, j]), o["vento"][t])
                for var_n, (p, ob) in coppie.items():
                    r = acc.setdefault((pil, var), {}).setdefault(var_n, {}).setdefault(b, [0, 0.0, 0.0])
                    r[0] += 1; r[1] += p - ob; r[2] += abs(p - ob)
            op = o.get("pioggia", {})
            ind = {h: k for k, h in enumerate(ore)}
            for h in range(3, 49, 3):                       # accumulo delle 3 ore che finiscono a +h
                if h - 2 <= max(libera, 0) or any((h - i) not in ind for i in range(3)):
                    continue
                ts = [T0 + timedelta(hours=h - i) for i in range(3)]
                if not all(t in op for t in ts):
                    continue
                prev = sum(float(d["pioggia"][ind[h - i], j]) for i in range(3))
                oss_p = sum(op[t] for t in ts)
                if not np.isfinite(prev):
                    continue
                b = (h - 1) // BIN
                r = acc.setdefault((pil, var), {}).setdefault("pioggia", {}).setdefault(b, [0, 0.0, 0.0])
                r[0] += 1; r[1] += prev - oss_p; r[2] += abs(prev - oss_p)
                q = acc.setdefault((pil, var), {}).setdefault("csi", {}).setdefault(b, [0, 0, 0, 0])   # n, colpi, falsi, mancati
                q[0] += 1
                if prev >= 1 and oss_p >= 1: q[1] += 1
                elif prev >= 1: q[2] += 1
                elif oss_p >= 1: q[3] += 1
        print("ciclo", c, pil, var or "-", flush=True)
    json.dump({f"{p}|{v}": {vn: {str(b): x for b, x in dd.items()} for vn, dd in al.items()} for (p, v), al in acc.items()},
              open(os.path.join(a.out, f"modelli_stazioni_{a.zona}.json"), "w"))
    for vn in VARS:
        disegna(acc, vn, a.zona, len({k[2] for k in files}), os.path.join(a.out, f"confronto_modelli_{vn}_{a.zona}.png"))


def nome(p, v):
    base = {"ecmwf": "ECMWF", "icon2i": "ICON-2I", "iconeu": "ICON-EU", "gfs": "GFS"}[p]
    return base + (f" + {v}" if v else " (ufficiale)")


def disegna(acc, vn, zona, ncicli, out):
    titolo_v, unita = VARS[vn]
    famiglie = [("Famiglia ECMWF", lambda p: p == "ecmwf"), ("Famiglia ICON-2I", lambda p: p == "icon2i"),
                ("ICON-EU e GFS (prove) con gli ufficiali", lambda p: p in ("iconeu", "gfs"))]
    cm = plt.get_cmap("tab10")
    fig, assi = plt.subplots(2, 3, figsize=(18, 9))
    for col, (titolo, chi) in enumerate(famiglie):
        chiavi = sorted([k for k in acc if vn in acc[k] and (chi(k[0]) or (col == 2 and k[1] == "" and k[0] in ("ecmwf", "icon2i")))],
                        key=lambda k: (k[1] != "", k[0], k[1]))
        for i, k in enumerate(chiavi):
            uff = k[1] == ""
            colore = {"ecmwf": "#264653", "icon2i": "#6a4c93"}[k[0]] if uff and k[0] in ("ecmwf", "icon2i") else cm(i % 10)
            if vn == "csi":
                bs = sorted(b for b, q in acc[k][vn].items() if q[1] + q[2] + q[3] >= 5)
                x = [b * BIN + (BIN + 1) / 2 for b in bs]
                assi[0][col].plot(x, [acc[k][vn][b][1] / (acc[k][vn][b][1] + acc[k][vn][b][2] + acc[k][vn][b][3]) for b in bs], color=colore,
                                  lw=3 if uff else 1.4, marker="o" if uff else None, ms=4, label=nome(*k))
                assi[1][col].plot(x, [(acc[k][vn][b][1] + acc[k][vn][b][2]) / max(acc[k][vn][b][1] + acc[k][vn][b][3], 1) for b in bs],
                                  color=colore, lw=3 if uff else 1.4)
                continue
            bs = sorted(b for b, x in acc[k][vn].items() if x[0] >= 20)
            x = [b * BIN + (BIN + 1) / 2 for b in bs]
            assi[0][col].plot(x, [acc[k][vn][b][2] / acc[k][vn][b][0] for b in bs], color=colore, lw=3 if uff else 1.4,
                              marker="o" if uff else None, ms=4, label=nome(*k))
            assi[1][col].plot(x, [acc[k][vn][b][1] / acc[k][vn][b][0] for b in bs], color=colore, lw=3 if uff else 1.4)
        assi[0][col].set_title(titolo, fontsize=11)
        assi[0][col].legend(fontsize=7)
        assi[1][col].axhline(0, color="k", lw=.8)
        for r in range(2):
            assi[r][col].grid(alpha=.3); assi[r][col].set_xlabel("ore di previsione")
    if vn == "csi":
        assi[0][0].set_ylabel("CSI (1 = perfetto)")
        assi[1][0].set_ylabel("frequenza prevista / osservata (1 = giusta)")
        for col in range(3):
            assi[1][col].axhline(1, color="k", lw=.8)
        fig.suptitle(f"{titolo_v} - zona {zona} - {ncicli} cicli (linea spessa = ufficiale)", fontsize=13)
        fig.tight_layout(); fig.savefig(out, dpi=110)
        return
    assi[0][0].set_ylabel(f"errore assoluto medio ({unita}) - meno è meglio")
    assi[1][0].set_ylabel(f"errore medio con segno ({unita}) - 0 è giusto")
    fig.suptitle(f"{titolo_v}: tutte le versioni di WRF contro le stazioni - zona {zona} - {ncicli} cicli (linea spessa = ufficiale)", fontsize=13)
    fig.tight_layout()
    fig.savefig(out, dpi=110)


if __name__ == "__main__":
    main()
