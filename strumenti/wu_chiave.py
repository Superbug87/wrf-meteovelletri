"""Chiave Wunderground che funziona: prima quella del segreto, se non vale una trovata nella pagina pubblica di una stazione.

chiave_funzionante(segreto) restituisce la chiave da usare per le richieste a api.weather.com:
  1. il segreto WU_API_KEY, se WU lo accetta;
  2. altrimenti le chiavi (32 caratteri esadecimali) che compaiono nella pagina pubblica di una stazione, provate una per una:
     vale la prima che WU accetta per la ricerca di stazioni e per la lettura di una stazione;
  3. se nessuna funziona restituisce il segreto com'e', e chi la usa si comporta come prima (errore 401 gestito a valle).
La chiave del sito e' di Wunderground, non nostra: puo' cambiare quando vuole, per questo si ricerca a ogni uso (poche richieste).
Se il modulo non e' raggiungibile da uno script, lo script usa il segreto e basta (vedi il try/except negli import).
"""
import json
import os
import random
import re
import sys

try:
    import requests
except ImportError:                                  # senza requests non si prova nulla: resta il segreto
    requests = None

URL_RICERCA = "https://api.weather.com/v3/location/near"
URL_LETTURA = "https://api.weather.com/v2/pws/observations/current"
PAGINA = "https://www.wunderground.com/dashboard/pws/{id}"
STAZIONI_PAGINA = ["IGENZANO3", "IVELLE79"]          # pagine di riserva, sempre provate per ultime
FILE_STAZIONI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stazioni_verifica.json")
PAGINE_MAX = 8                                       # quante pagine di stazioni provare al massimo
MAX_CANDIDATE = 15
_cache = {}


def _log(testo):
    print(testo, file=sys.stderr, flush=True)


def _stato(url, params, chiave):
    try:
        r = requests.get(url, params={**params, "apiKey": chiave}, timeout=20)
        return r.status_code
    except requests.RequestException:
        return None


def funziona(chiave):
    """True se WU accetta la chiave per la ricerca di stazioni e per la lettura di una stazione."""
    if not chiave or requests is None:
        return False
    ricerca = _stato(URL_RICERCA, {"geocode": "41.68,12.78", "product": "pws", "format": "json"}, chiave)
    if ricerca != 200:
        return False
    lettura = _stato(URL_LETTURA, {"stationId": STAZIONI_PAGINA[0], "format": "json", "units": "m"}, chiave)
    return lettura in (200, 204)                     # 204: stazione muta, ma la chiave e' valida


def stazioni_per_la_pagina():
    """ID di stazioni WU del Lazio da cui provare a leggere la pagina: quelle della verifica (sparse sulla regione) in ordine
    casuale, cosi' una stazione sparita non blocca la ricerca; in fondo le due di riserva."""
    ids = []
    try:
        with open(FILE_STAZIONI, encoding="utf-8") as f:
            ids = [w["id"].replace("WU_", "", 1) for w in json.load(f).get("wu", []) if w.get("id")]
    except (OSError, ValueError, AttributeError, KeyError):
        pass
    random.shuffle(ids)
    return (ids[:PAGINE_MAX - len(STAZIONI_PAGINA)] + STAZIONI_PAGINA)[:PAGINE_MAX]


def chiavi_dalla_pagina():
    """Le chiavi (32 esadecimali) nella pagina pubblica di una stazione, quelle vicine a 'apiKey/API_KEY' per prime."""
    if requests is None:
        return []
    for sid in stazioni_per_la_pagina():
        try:
            r = requests.get(PAGINA.format(id=sid), timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        except requests.RequestException:
            continue
        if r.status_code != 200:
            continue
        testo = r.text
        vicine = re.findall(r'api_?key\W{1,8}([0-9a-f]{32})', testo, re.IGNORECASE)
        tutte = re.findall(r'(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])', testo)
        visti, out = set(), []
        for k in vicine + tutte:
            k = k.lower()
            if k not in visti:
                visti.add(k)
                out.append(k)
        if out:
            return out[:MAX_CANDIDATE]
    return []


def chiave_funzionante(segreto):
    """Vedi la descrizione del modulo. Non solleva mai eccezioni: nel peggiore dei casi restituisce il segreto."""
    segreto = (segreto or "").strip()
    if not segreto:                                  # segreto assente: stazioni disattivate di proposito
        return segreto
    if segreto in _cache:
        return _cache[segreto]
    scelta = segreto
    try:
        if funziona(segreto):
            scelta = segreto
        else:
            _log("Chiave WU del segreto rifiutata: cerco una chiave che funzioni nella pagina pubblica di una stazione")
            for k in chiavi_dalla_pagina():
                if k != segreto and funziona(k):
                    _log(f"Uso la chiave WU trovata nella pagina pubblica ({k[:4]}...)")
                    scelta = k
                    break
            else:
                _log("Nessuna chiave WU funziona: si resta con quella del segreto (le stazioni potrebbero mancare)")
    except Exception as e:                           # mai bloccare il ciclo per questo
        _log(f"Ricerca della chiave WU non riuscita ({type(e).__name__}): resta quella del segreto")
    _cache[segreto] = scelta
    return scelta


if __name__ == "__main__":
    import os
    k = chiave_funzionante(os.environ.get("WU_API_KEY", ""))
    print("chiave valida" if funziona(k) else "nessuna chiave valida", f"({k[:4]}...)" if k else "")
