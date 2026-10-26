#!/usr/bin/env python3
"""Prova: MeteoHub ritaglia i dati ICON-2I sul server? (punto 4 dell'analisi del 26/10/2026)

Oggi ogni istante ICON-2I si scarica su tutta l'Italia (~359 MB i soli livelli del modello) e si ritaglia dopo: per questo
i bordi arrivano solo ogni 6 ore. Se MeteoHub accetta il ritaglio (post-elaborazione "grid_cropping"), ogni istante pesa
molto meno e i bordi possono scendere a 3 ore o a 1 ora senza riempire la quota dell'account (~1 GB).

Cosa fa (solo lettura, nessun effetto sui run): dal profilo stampa quote e post-elaborazioni permesse; poi chiede la sola
temperatura sui 65 livelli del modello a +6 h dell'ultimo ciclo, prima intera e poi ritagliata sull'area del Lazio e su
quella dei Castelli, e confronta peso e griglia. Cancella sempre le richieste che crea.
"""
import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "zone" / "castelli" / "wrf"))
import scarica_icon2i as S  # noqa: E402

AREE = {
    "lazio": (S.LAT_MIN, S.LAT_MAX, S.LON_MIN, S.LON_MAX),      # area che oggi si tiene dopo il download (Lazio + Castelli)
    "castelli": (39.4, 44.0, 9.6, 15.9),                        # dominio esterno dei Castelli (450 km) con un margine
}
SENSIBILI = ("email", "name", "surname", "uuid", "id", "token", "password", "first", "last", "user", "group")


def profilo(hub):
    for url in ("/auth/profile", "/api/usage", "/api/hourly"):
        try:
            r = requests.get(S.BASE + url, headers=hub.h, timeout=60)
            j = r.json()
        except Exception as e:
            print(f"{url}: {type(e).__name__}")
            continue
        if isinstance(j, dict):
            j = {k: v for k, v in j.items() if not any(s in k.lower() for s in SENSIBILI)}
        print(f"{url} (HTTP {r.status_code}): {json.dumps(j, ensure_ascii=False)[:1500]}")


def chiedi(hub, corpo, nome, attesa=1200):
    """Ritorna (esito, MB, byte) e cancella sempre la richiesta."""
    r = requests.post(S.BASE + "/api/data", json=corpo, headers=hub.h, timeout=120)
    if r.status_code != 202:
        return f"rifiutata HTTP {r.status_code}: {r.text[:400]}", None, b""
    rid = r.json()["request_id"]
    t0 = time.time()
    try:
        while time.time() - t0 < attesa:
            time.sleep(5)
            for v in requests.get(S.BASE + "/api/requests", headers=hub.h, timeout=60).json():
                if v["id"] != rid:
                    continue
                if v["status"] == "SUCCESS":
                    d = requests.get(f"{S.BASE}/api/data/{v['fileoutput']}", headers=hub.h, timeout=1800)
                    return f"riuscita in {time.time() - t0:.0f} s", (v.get("filesize") or len(d.content)) / 1e6, d.content
                if v["status"] in ("FAILURE", "REVOKED"):
                    return f"fallita: {v.get('error_message')}", None, b""
        return f"ancora in corso dopo {attesa} s", None, b""
    finally:
        hub.cancella(rid)


def griglia(dati):
    import eccodes as ec

    n, info = 0, None
    for h in S.messaggi(dati):
        n += 1
        if info is None:
            g = lambda k: ec.codes_get(h, k)
            info = (f"{g('Ni')} x {g('Nj')} punti, lat {g('latitudeOfFirstGridPointInDegrees'):.2f}..{g('latitudeOfLastGridPointInDegrees'):.2f}"
                    f", lon {g('longitudeOfFirstGridPointInDegrees'):.2f}..{g('longitudeOfLastGridPointInDegrees'):.2f}")
    return f"{n} messaggi, {info}"


def main():
    hub = S.Hub()
    profilo(hub)
    ciclo = S.ultimo_ciclo(hub)
    fm = hub.elenco(S.MODELLO)
    liv = S.scegli(fm["level"], lambda l: l.get("style") == "GRIB2D" and l.get("l1") == S.TIPO_MOD and l.get("l2") == S.TIPO_MOD
                   and l.get("value2") == l.get("value1") + 1 and 1 <= l["value1"] <= 65)
    filtri = {"product": S.scegli(fm["product"], lambda p: p["desc"] == "Temperature (K)"), "level": liv,
              "timerange": S.tempo(fm["timerange"], 6)}
    t = ciclo.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    base = {"dataset_names": [S.MODELLO], "only_reliable": False, "reftime": {"from": t, "to": t}, "filters": filtri}
    print(f"\nCiclo {ciclo:%Y%m%d%H}, +6 h, temperatura su {len(liv)} livelli del modello")

    esito, mb, dati = chiedi(hub, {**base, "request_name": "prova_intera"}, "intera")
    print(f"\n[intera] {esito}" + (f": {mb:.1f} MB; {griglia(dati)}" if dati else ""))
    mb_intera = mb

    for area, (la0, la1, lo0, lo1) in AREE.items():
        b = {"ilon": lo0, "ilat": la0, "flon": lo1, "flat": la1}
        varianti = [
            {"processor_type": "grid_cropping", "sub_type": "coord", "boundings": b},
            {"processor_type": "grid_cropping", "sub_type": "bbox", "boundings": b},
            {"processor_type": "grid_cropping", "boundings": b},
        ]
        for i, pp in enumerate(varianti):
            esito, mb, dati = chiedi(hub, {**base, "request_name": f"prova_ritaglio_{area}_{i}", "postprocessors": [pp]}, area)
            print(f"\n[ritaglio {area}, forma {i}: {json.dumps(pp)}]\n  {esito}" + (f": {mb:.1f} MB; {griglia(dati)}" if dati else ""))
            if dati:
                if mb_intera:
                    print(f"  peso rispetto all'intera: {100 * mb / mb_intera:.0f}%")
                break
    # nessuna pulizia generale qui: chiedi() cancella le sue richieste, e un run ufficiale potrebbe stare scaricando


if __name__ == "__main__":
    main()
