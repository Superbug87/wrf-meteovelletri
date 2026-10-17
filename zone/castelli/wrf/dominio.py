"""Geometria dei tre domini annidati (9 -> 3 -> 1 km) centrati sui Castelli Romani.

Se cambi qualcosa qui, la cache dei dati statici (geogrid) viene rifatta da zero.
"""

LAT0, LON0 = 41.71, 12.73          # centro comune dei tre domini
TRUELAT1, TRUELAT2 = 39.5, 44.0    # proiezione conforme di Lambert
DX = [9000, 3000, 1000]            # metri
RATIO = [1, 3, 3]                  # rapporto con il dominio padre
E = [51, 55, 61]                   # punti per lato (e_we = e_sn): 450 km, 162 km, 60 km
E_VERT = 40                        # livelli verticali (39 di massa)


def celle(d):
    return E[d] - 1


def parent_starts():
    """Indici (da 1) del nido dentro il padre, in modo che sia al centro."""
    starts = [1]
    for d in (1, 2):
        diff = celle(d - 1) - celle(d) // RATIO[d]
        if diff % 2:
            raise ValueError(f"il dominio {d + 1} non si centra nel padre: cambia E[{d - 1}] o E[{d}]")
        starts.append(diff // 2 + 1)
    return starts


def verifica():
    """Controlli che WRF farebbe a suo modo (e male) a metà del lavoro."""
    starts = parent_starts()
    for d in (1, 2):
        if celle(d) % RATIO[d]:
            raise ValueError(f"dominio {d + 1}: (e - 1) deve essere multiplo di {RATIO[d]}")
        nido_nel_padre = celle(d) // RATIO[d]
        a_sinistra = starts[d] - 1
        a_destra = celle(d - 1) - a_sinistra - nido_nel_padre
        if min(a_sinistra, a_destra) < 5:
            raise ValueError(f"dominio {d + 1}: meno di 5 celle di margine nel padre")
    lato_km = [celle(d) * DX[d] / 1000 for d in range(3)]
    if lato_km[2] < 50:
        raise ValueError("il dominio a 1 km deve coprire almeno i 40 km delle mappe più un margine")
    return starts, lato_km


if __name__ == "__main__":
    s, km = verifica()
    print("i_parent_start / j_parent_start:", s)
    print("lato dei domini (km):", km)
