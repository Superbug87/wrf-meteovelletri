#!/usr/bin/env python3
"""Scarica i file GFS 0,25° (condizioni iniziali e al contorno) dal bucket pubblico di NOAA su AWS.

  scarica_gfs.py --ore 48 --solo-ciclo                 stampa l'ultimo ciclo completo (AAAAMMGGHH)
  scarica_gfs.py --ciclo 2026101600 --ore 48 --out gfs scarica f000, f003 ... f048
"""
import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"


def url(ciclo, h):
    return f"{BASE}/gfs.{ciclo[:8]}/{ciclo[8:]}/atmos/gfs.t{ciclo[8:]}z.pgrb2.0p25.f{h:03d}"


def esiste(u):
    try:
        return requests.head(u, timeout=30).status_code == 200
    except requests.RequestException:
        return False


def ultimo_ciclo(ore):
    """Il ciclo più recente (00/06/12/18 UTC) di cui esiste già l'ultima ora che serve."""
    adesso = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    c = adesso.replace(hour=adesso.hour - adesso.hour % 6)
    for _ in range(8):
        testo = c.strftime("%Y%m%d%H")
        if esiste(url(testo, ore)):
            return testo
        c -= timedelta(hours=6)
    sys.exit("Nessun ciclo GFS completo trovato nelle ultime 48 ore.")


def scarica(u, destinazione, tentativi=6):
    destinazione = Path(destinazione)
    for i in range(tentativi):
        try:
            with requests.get(u, stream=True, timeout=120) as r:
                r.raise_for_status()
                atteso = int(r.headers.get("Content-Length", 0))
                tmp = destinazione.with_suffix(".parte")
                with open(tmp, "wb") as f:
                    for blocco in r.iter_content(1 << 20):
                        f.write(blocco)
                if atteso and tmp.stat().st_size != atteso:
                    raise IOError(f"scaricati {tmp.stat().st_size} byte su {atteso}")
                tmp.rename(destinazione)
                return destinazione.name
        except (requests.RequestException, IOError) as e:
            print(f"{destinazione.name}: {e}; riprovo ({i + 1}/{tentativi})", file=sys.stderr)
            time.sleep(10 * (i + 1))
    sys.exit(f"Impossibile scaricare {u}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ciclo", default="", help="AAAAMMGGHH; vuoto = l'ultimo completo")
    ap.add_argument("--ore", type=int, default=48)
    ap.add_argument("--out", default="gfs")
    ap.add_argument("--solo-ciclo", action="store_true")
    a = ap.parse_args()

    ciclo = a.ciclo or ultimo_ciclo(a.ore)
    if a.solo_ciclo:
        print(ciclo)
        return
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    ore = list(range(0, a.ore + 1, 3))
    with ThreadPoolExecutor(4) as ex:
        for nome in ex.map(lambda h: scarica(url(ciclo, h), out / f"gfs.t{ciclo[8:]}z.pgrb2.0p25.f{h:03d}"), ore):
            print("ok", nome, flush=True)


if __name__ == "__main__":
    main()
