#!/usr/bin/env python3
"""Radar nazionale del Dipartimento della Protezione Civile (Radar-DPC): scarico, posizione sulla mappa e movimento.

Dati: Radar-DPC, Dipartimento della Protezione Civile (https://radar.protezionecivile.it), licenza CC BY-SA 4.0:
chi pubblica immagini ricavate dal radar deve citare la fonte e usare la stessa licenza.

Prodotto usato: SRI (intensita' di pioggia in mm/h, corretta con i pluviometri), un mosaico di 1400 x 1200 pixel
da 1 km su tutta l'Italia, ogni 5 minuti, in GeoTIFF; proiezione Transverse Mercator con meridiano centrale 12,5 E e
origine a 42 N; l'angolo in alto a sinistra e' a (-600 km, +650 km). Fuori copertura il valore e' -9999.
"""
import io
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import requests

API = "https://radar-api.protezionecivile.it"
HDR = {"origin": "https://radar.protezionecivile.it"}      # richiesto dal servizio (vedi la sua documentazione)
PROJ = "+proj=tmerc +lat_0=42 +lon_0=12.5 +k=1 +x_0=0 +y_0=0 +datum=WGS84 +units=m"
X0, Y0, LATO = -600000.0, 650000.0, 1000.0
PASSO_MIN = 5

_trasf = None


def _t():
    global _trasf
    if _trasf is None:
        from pyproj import Transformer

        _trasf = Transformer.from_crs("EPSG:4326", PROJ, always_xy=True)
    return _trasf


def _richiesta(metodo, url, tentativi=4, **kw):
    ultimo = None
    for i in range(tentativi):
        try:
            r = requests.request(metodo, url, timeout=60, **kw)
            if r.status_code in (200, 404):
                return r
            ultimo = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            ultimo = str(e)[:80]
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"Radar-DPC non risponde ({ultimo})")


def ultimo_istante(tipo="SRI"):
    """Istante (millisecondi UTC) dell'ultimo prodotto pubblicato."""
    r = _richiesta("GET", f"{API}/findLastProductByType", params={"type": tipo}, headers=HDR)
    prodotti = r.json().get("lastProducts", []) if r.status_code == 200 else []
    if not prodotti:
        raise RuntimeError("Radar-DPC: nessun prodotto disponibile")
    return int(prodotti[0]["time"])


def scarica(tipo, ms):
    """Il mosaico all'istante dato (millisecondi UTC) come matrice float32 (righe da nord); NaN dove non c'e' dato.
    None se il file non esiste."""
    import tifffile

    r = _richiesta("POST", f"{API}/downloadProduct", json={"productType": tipo, "productDate": int(ms)}, headers=HDR)
    if r.status_code == 404:
        return None
    g = _richiesta("GET", r.json()["url"])
    if g.status_code != 200:
        return None
    a = tifffile.imread(io.BytesIO(g.content)).astype("float32")
    a[a < 0] = np.nan                                      # -9999: fuori dalla copertura dei radar
    return a


def serie(tipo, fine_ms, n, passo_min=PASSO_MIN, ritaglio=None):
    """n mosaici, uno ogni passo_min minuti, l'ultimo a fine_ms.
    ritaglio=(lat, lon, mezzo): di ogni mosaico si tiene solo il quadrato di 2*mezzo pixel attorno al punto
    (un mosaico intero pesa 6,7 MB, e ne servono decine). Ritorna (istanti, mosaici o None se mancano, origine
    del ritaglio come (riga, colonna) nel mosaico intero)."""
    istanti = [int(fine_ms) - (n - 1 - i) * passo_min * 60000 for i in range(n)]
    origine = [(0, 0)]

    def uno(ms):
        a = scarica(tipo, ms)
        if a is None or ritaglio is None:
            return a
        c, origine[0] = finestra(a, *ritaglio)
        return c.copy()

    with ThreadPoolExecutor(4) as ex:
        frames = list(ex.map(uno, istanti))
    return istanti, frames, origine[0]


def indici(lat, lon):
    """Posizione (riga, colonna), con decimali e riferita ai centri dei pixel, dei punti lat/lon nel mosaico."""
    x, y = _t().transform(np.asarray(lon, float), np.asarray(lat, float))
    return (Y0 - y) / LATO - 0.5, (x - X0) / LATO - 0.5


def finestra(a, lat0, lon0, mezzo=150):
    """Ritaglio di (2*mezzo) pixel attorno a un punto. Ritorna (ritaglio, (riga0, colonna0))."""
    r, c = indici(lat0, lon0)
    r0 = int(np.clip(round(float(r)) - mezzo, 0, a.shape[0] - 2 * mezzo))
    c0 = int(np.clip(round(float(c)) - mezzo, 0, a.shape[1] - 2 * mezzo))
    return a[r0:r0 + 2 * mezzo, c0:c0 + 2 * mezzo], (r0, c0)


def campiona(a, lat, lon, origine=(0, 0)):
    """Valori della matrice nei punti lat/lon (interpolazione lineare); NaN dove manca il dato.
    `origine` e' la posizione dell'angolo del ritaglio nel mosaico intero, se a e' un ritaglio."""
    from scipy.ndimage import map_coordinates

    r, c = indici(lat, lon)
    r, c = r - origine[0], c - origine[1]
    ok = np.isfinite(a)
    v = map_coordinates(np.where(ok, a, 0.0), [r, c], order=1, mode="nearest")
    m = map_coordinates(ok.astype("float32"), [r, c], order=1, mode="nearest")
    return np.where(m > 0.5, v, np.nan)


# ----------------------------------------------------------------------------
# Movimento della pioggia e previsione per estrapolazione
# ----------------------------------------------------------------------------
def a_dbz(R):
    """mm/h -> dBZ con Z = 200 R^1.6 (Marshall-Palmer); sotto 0,1 mm/h vale 0."""
    R = np.asarray(R, float)
    return np.where(R > 0.1, 10 * np.log10(200 * np.maximum(R, 0.1) ** 1.6), 0.0)


VEL_MIN = 2.0       # pixel per intervallo di 5 minuti (circa 24 km/h): sotto, la pioggia e' lasciata ferma


def _immagine(R):
    """mm/h -> immagine a 8 bit (dBZ da 0 a 50) per il calcolo del movimento."""
    return np.clip(a_dbz(np.nan_to_num(R)) * 255 / 50, 0, 255).astype(np.uint8)


def movimento(f0, f1, passi, massimo=8.0, vel_min=VEL_MIN, centro=300):
    """Spostamento della pioggia in pixel (colonne, righe) per ogni intervallo di 5 minuti, uguale su tutta l'area.
    Si seguono i punti caratteristici dell'immagine (Lucas-Kanade) dal mosaico f0 al mosaico f1, `passi` intervalli
    dopo, e si prende la mediana degli spostamenti. Si usa il quadrato centrale di `centro` pixel.

    Collaudo (README): su 76 casi in Italia, spostare la pioggia con un movimento stimato sopra circa 24 km/h
    migliora un poco l'accordo di posizione (circa +0,04), mentre con movimenti lenti peggiora (circa -0,03): sotto
    la velocita' minima il movimento e' considerato rumore e ritorna zero.
    Ritorna (vettore, testo che spiega cosa e' successo)."""
    import cv2

    def taglia(a):
        h, w = a.shape
        r0, c0 = max(0, (h - centro) // 2), max(0, (w - centro) // 2)
        return a[r0:r0 + centro, c0:c0 + centro]

    i0, i1 = _immagine(taglia(f0)), _immagine(taglia(f1))
    zero = np.zeros(2, "float32")
    punti = cv2.goodFeaturesToTrack(i0, maxCorners=400, qualityLevel=0.01, minDistance=8, blockSize=9)
    if punti is None or len(punti) < 8:
        return zero, "troppa poca pioggia per seguirla"
    nuovi, ok, _ = cv2.calcOpticalFlowPyrLK(i0, i1, punti, None, winSize=(41, 41), maxLevel=4)
    ok = ok.ravel() == 1
    if ok.sum() < 8:
        return zero, "troppa poca pioggia per seguirla"
    v = np.median((nuovi[ok] - punti[ok]).reshape(-1, 2) / max(passi, 1), axis=0).astype("float32")
    mod = float(np.hypot(*v))
    if mod < vel_min:
        return zero, "pioggia ferma o lenta: il movimento stimato è troppo incerto"
    if mod > massimo:
        v *= massimo / mod
    return v, "movimento stimato dal radar"


def estrapola(R0, flusso, k):
    """La pioggia R0 spostata di k intervalli lungo il flusso (campionata all'indietro)."""
    import cv2

    h, w = R0.shape
    gy, gx = np.mgrid[0:h, 0:w].astype("float32")
    mx = (gx - k * flusso[..., 0]).astype("float32")
    my = (gy - k * flusso[..., 1]).astype("float32")
    return cv2.remap(np.nan_to_num(R0).astype("float32"), mx, my, cv2.INTER_LINEAR,
                     borderMode=cv2.BORDER_CONSTANT, borderValue=0)


if __name__ == "__main__":
    ms = ultimo_istante("SRI")
    a = scarica("SRI", ms)
    print("ultimo mosaico:", time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ms / 1000)), a.shape,
          "pixel con pioggia:", int((a > 0.1).sum()), file=sys.stderr)
