"""
orarie.py - una immagine per ogni ora e per ogni variabile, senza descrizioni.

Ogni immagine porta scritti solo: tipo di mappa, ora di partenza del run, ora della mappa (tutte in ora
locale). Stessa scala di colori per tutte le ore di una variabile, così si può sfogliare il tempo.

Uscita (in `out`):
  orarie/indice.json                       elenco di variabili e ore dell'ultimo ciclo
  orarie/<ciclo>/indice.json               lo stesso, per ogni ciclo (la pagina del sito sfoglia i cicli)
  orarie/<ciclo>/<variabile>/<AAAAMMGGHH>.png   AAAAMMGGHH = ora della mappa in UTC
"""
import io
import json
import sys
from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib.pyplot as plt
import numpy as np
from PIL import Image

import previsioni_lazio as pc

L, A = 6.0, 7.7            # pollici: con 100 dpi 600 x 770 pixel, leggibili sul telefono
LATO, MARG = 5.5, 0.25
DPI = 100


def _scala(f, dati):
    """Livelli e colori uguali per tutte le ore. Ritorna (livelli, colori, a_fasce_fisse)."""
    if "livelli" in f:
        liv = list(f["livelli"])
        return liv, pc.colori_fasce(f["scala"], len(liv)), True
    if f["scala"] == "arcobaleno":
        liv, cols = pc.scala_arcobaleno(dati, n=8, span_min=f.get("span", 1.5))
        return liv, cols, False
    liv, cols = pc.scala_assoluta(f["scala"], dati, f["passo"], massimo=f.get("massimo"),
                                  max_fasce=f.get("max_fasce", 12))
    return liv, cols, False


def disegna_orarie(c, z_f, lat_f, lon_f, tempi, tempi_utc, ciclo, out):
    """tempi: ore locali (naive) delle mappe; tempi_utc: le stesse in UTC; ciclo: AAAAMMGGHH (UTC) o ''."""
    out = Path(out) / "orarie"
    if ciclo:
        run_utc = pc.datetime.strptime(ciclo, "%Y%m%d%H").replace(tzinfo=timezone.utc)
        run_txt = pc.etichetta(run_utc.astimezone(ZoneInfo("Europe/Rome")).replace(tzinfo=None))
    else:
        run_txt = "?"
    chiave_ciclo = ciclo or "senza_ciclo"

    LON, LAT = np.meshgrid(lon_f, lat_f)
    variabili = []
    totale = 0
    for f in pc.definisci_fogli(c):
        if f["dati"] is None:
            print(f"Orarie: salto «{f['titolo']}»: dato non disponibile.", file=sys.stderr)
            continue
        dati = np.asarray(f["dati"], float)
        liv, cols, fisse = _scala(f, dati)
        cartella = out / chiave_ciclo / f["nome"]
        cartella.mkdir(parents=True, exist_ok=True)

        def riempi(ax, campo):
            if not fisse:
                return ax.contourf(LON, LAT, campo, levels=liv, colors=cols, zorder=1)
            if campo.max() < liv[0]:
                return None
            cs = ax.contourf(LON, LAT, campo, levels=liv, colors=cols[:-1], extend="max", zorder=1)
            cs.cmap.set_over(cols[-1])
            cs.changed()
            return cs

        frecce = f.get("frecce")

        # barra dei colori: la stessa per tutte le ore (livelli fissi per variabile)
        for k, (t_loc, t_utc) in enumerate(zip(tempi, tempi_utc)):
            fig = plt.figure(figsize=(L, A), facecolor=pc.SFONDO)
            fig.text(MARG / L, 1 - 0.30 / A, f"{f['titolo']} ({f['unita']})", fontsize=17, weight="bold",
                     color=pc.INK, va="center")
            fig.text(MARG / L, 1 - 0.68 / A, f"Run  {run_txt}", fontsize=12, color=pc.SEC, va="center")
            fig.text(MARG / L, 1 - 1.02 / A, f"Mappa  {pc.etichetta(t_loc)}", fontsize=14, weight="bold",
                     color=pc.INK, va="center")

            ax = fig.add_axes([(L - LATO) / 2 / L, 1 - (1.28 + LATO) / A, LATO / L, LATO / A])
            ax.set_facecolor(pc.SFONDO)
            ax.set_xlim(pc.LON_MIN, pc.LON_MAX)
            ax.set_ylim(pc.LAT_MIN, pc.LAT_MAX)
            ax.set_aspect(1 / np.cos(np.radians(pc.LAT0)))
            ax.set_xticks([])
            ax.set_yticks([])
            for sp_ in ax.spines.values():
                sp_.set_color(pc.LINEA)
            riempi(ax, dati[k])
            pc.decora(ax, z_f, lat_f, lon_f)
            if frecce is not None:
                pc.disegna_flusso(ax, lon_f, lat_f, frecce[0][k], frecce[1][k], densita=1.8, spessore=0.55)

            asse = fig.add_axes([0, 0, 0.01, 0.01])
            asse.set_visible(False)
            ref = np.linspace(liv[0], liv[-1] * (1.5 if fisse else 1.0), LON.size).reshape(LON.shape)
            cs = riempi(asse, ref)
            cax = fig.add_axes([(L - 5.0) / 2 / L, 0.58 / A, 5.0 / L, 0.17 / A])
            cb = fig.colorbar(cs, cax=cax, orientation="horizontal")
            tk = pc.tick_utili(liv, 14 if fisse else 10)
            cb.set_ticks(tk)
            cb.set_ticklabels([f"{t:g}" for t in tk])
            cb.ax.tick_params(labelsize=10, colors=pc.SEC, length=3)
            cb.outline.set_visible(False)

            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=DPI, facecolor=pc.SFONDO)
            plt.close(fig)
            # 64 colori bastano per mappe a fasce piatte: file 3 volte più piccoli, uguali all'occhio
            Image.open(buf).convert("RGB").quantize(colors=64, method=Image.Quantize.MEDIANCUT,
                                                    dither=Image.Dither.NONE).save(
                cartella / f"{t_utc:%Y%m%d%H}.png", optimize=True)
            totale += 1
        variabili.append({"id": f["nome"], "titolo": f["titolo"], "unita": f["unita"]})

    indice = {
        "ciclo": chiave_ciclo,
        "run": run_utc.strftime("%Y-%m-%dT%H:%M:%SZ") if ciclo else "",
        "variabili": variabili,
        "ore": [t.strftime("%Y-%m-%dT%H:00:00Z") for t in tempi_utc],
        "percorso": "{ciclo}/{variabile}/{ora}.png",      # {ora} = AAAAMMGGHH in UTC
    }
    out.mkdir(parents=True, exist_ok=True)
    testo = json.dumps(indice, ensure_ascii=False, indent=1)
    (out / "indice.json").write_text(testo, encoding="utf-8")                 # l'ultimo ciclo
    (out / chiave_ciclo / "indice.json").write_text(testo, encoding="utf-8")  # e uno per ciclo, per sfogliare i precedenti
    print(f"Orarie: {totale} immagini ({len(variabili)} variabili x {len(tempi)} ore) in {out}")
    return totale
