#!/usr/bin/env python3
"""pulisci_meteohub.py - mostra e cancella le richieste vecchie dell'account MeteoHub (liberano lo spazio disponibile).

  MH_EMAIL=... MH_PASSWORD=... python3 wrf/pulisci_meteohub.py [--cancella]

Senza --cancella elenca soltanto. Non stampa mai le credenziali.
"""
import os
import sys

import requests

BASE = "https://meteohub.agenziaitaliameteo.it"


def main():
    r = requests.post(f"{BASE}/auth/login", json={"username": os.environ["MH_EMAIL"], "password": os.environ["MH_PASSWORD"]}, timeout=60)
    if r.status_code != 200:
        sys.exit(f"Login rifiutato (HTTP {r.status_code})")
    j = r.json()
    h = {"Authorization": f"Bearer {j if isinstance(j, str) else j.get('token')}"}

    lista = requests.get(f"{BASE}/api/requests", headers=h, timeout=60).json()
    tot = sum(v.get("filesize") or 0 for v in lista)
    print(f"{len(lista)} richieste, {tot / 1e6:.0f} MB in totale")
    for v in lista[:12]:
        print(f"  {v['id']} {v.get('name')} {v['status']} {(v.get('filesize') or 0) / 1e6:.0f} MB {v.get('submission_date')}")

    if "--cancella" in sys.argv:
        ok = 0
        for v in lista:
            x = requests.delete(f"{BASE}/api/requests/{v['id']}", headers=h, timeout=60)
            if x.status_code in (200, 202, 204):
                ok += 1
            else:
                print(f"  DELETE {v['id']}: HTTP {x.status_code} {x.text[:150]}")
        print(f"cancellate {ok} di {len(lista)}")
        lista = requests.get(f"{BASE}/api/requests", headers=h, timeout=60).json()
        print(f"restano {len(lista)} richieste, {sum(v.get('filesize') or 0 for v in lista) / 1e6:.0f} MB")


if __name__ == "__main__":
    main()
