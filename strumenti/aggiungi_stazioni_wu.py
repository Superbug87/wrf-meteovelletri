#!/usr/bin/env python3
"""Aggiunge stazioni Weather Underground "a norma" alla verifica (lista "wu" di strumenti/stazioni_verifica.json).

  WU_API_KEY=... aggiungi_stazioni_wu.py IROMA123 ICASTE45 ...

Per ogni ID legge da WU posizione e quota (osservazione attuale) e li scrive nella lista. Da quel momento: gli estratti dei nuovi cicli
includono quei punti (estrai_verifica.py) e la verifica del mattino li confronta in indici a parte (t2_wu, td2_wu, vento_wu), senza
mescolarli con la rete regionale e i SYNOP. I cicli passati non hanno quei punti. Chi usa le WU per il nudging (opzione "wu") le ha gia'
viste il modello nelle prime ore: la verifica conta solo le ore dopo la fine del nudging.
"""
import json
import os
import sys

import requests

QUI = os.path.dirname(os.path.abspath(__file__))
URL = "https://api.weather.com/v2/pws/observations/current"


def main():
    chiave = os.environ.get("WU_API_KEY")
    if not chiave or len(sys.argv) < 2:
        raise SystemExit(__doc__)
    p = os.path.join(QUI, "stazioni_verifica.json")
    testo = open(p).read()
    st = json.loads(testo)
    gia = {s["id"] for s in st.get("wu", [])}
    for wid in sys.argv[1:]:
        wid = wid.strip().upper()
        if "WU_" + wid in gia:
            print(wid, "gia' presente")
            continue
        r = requests.get(URL, params={"stationId": wid, "format": "json", "units": "m", "numericPrecision": "decimal", "apiKey": chiave}, timeout=30)
        try:
            o = r.json()["observations"][0]
            elev = o.get("metric", {}).get("elev")
            st.setdefault("wu", []).append({"id": "WU_" + wid, "nome": wid, "lat": float(o["lat"]), "lon": float(o["lon"]),
                                            "quota": None if elev is None else float(elev), "fonte": "Weather Underground (a norma)"})
            print(wid, "aggiunta", o["lat"], o["lon"], elev)
        except (KeyError, IndexError, ValueError, TypeError):
            print(wid, "non trovata o senza dati (HTTP", r.status_code, ")")
    # si riscrive solo la sezione "wu", lasciando il resto del file com'e'
    i = testo.rindex('"wu"')
    nuovo = testo[:i] + '"wu": ' + json.dumps(st["wu"], ensure_ascii=False, indent=1) + "\n}\n"
    json.loads(nuovo)
    open(p, "w").write(nuovo)


if __name__ == "__main__":
    main()
