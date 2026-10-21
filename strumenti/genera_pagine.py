"""genera_pagine.py - crea le due pagine del sito (Castelli e Lazio) con la scelta del modello (ECMWF / ICON-2I).

Parte dalla pagina modello (strumenti/modello_pagina.html, la pagina del Lazio del vecchio wrf-lazio) e cambia titolo,
indirizzi (i quattro rami risultati-<zona>-<modello> di wrf-meteovelletri) e nomi dei file.
Uso:  python3 strumenti/genera_pagine.py strumenti/modello_pagina.html sito
"""
import sys
from pathlib import Path

ZONE = {
    "castelli": dict(uscita="previsioni-wrf.html", nome="Castelli Romani",
                     ecmwf="https://raw.githubusercontent.com/Superbug87/wrf-meteovelletri/risultati-castelli-ecmwf/orarie/", f_ecmwf="wrf",
                     icon="https://raw.githubusercontent.com/Superbug87/wrf-meteovelletri/risultati-castelli-icon2i/orarie/", f_icon="wrf-icon2i-castelli"),
    "lazio": dict(uscita="previsioni-wrf-lazio.html", nome="Lazio",
                  ecmwf="https://raw.githubusercontent.com/Superbug87/wrf-meteovelletri/risultati-lazio-ecmwf/orarie/", f_ecmwf="wrf-lazio",
                  icon="https://raw.githubusercontent.com/Superbug87/wrf-meteovelletri/risultati-lazio-icon2i/orarie/", f_icon="wrf-icon2i-lazio"),
}


def sostituisci(testo, vecchio, nuovo):
    assert testo.count(vecchio) == 1, f"atteso 1 solo «{vecchio[:50]}», trovati {testo.count(vecchio)}"
    return testo.replace(vecchio, nuovo)


R = "https://raw.githubusercontent.com/Superbug87/wrf-meteovelletri"
FONTE_ECMWF = "Dati di partenza: ECMWF IFS (open data, CC BY 4.0), elaborati e modificati con WRF."
FONTE_ICON2I = "Dati di partenza: ICON-2I, Agenzia ItaliaMeteo in cooperation with Arpae Emilia-Romagna Idro-Meteo-Clima Service (CC BY 4.0), elaborati e modificati con WRF."
FONTE_ICONEU = "Dati di partenza: ICON-EU, Deutscher Wetterdienst (CC BY 4.0), elaborati e modificati con WRF."
FONTE_GFS = "Dati di partenza: GFS, NOAA/NCEP, elaborati e modificati con WRF."
FISICHE = {"topo2": "vento terreno 2", "mynn": "MYNN", "noahmp": "Noah-MP", "mynn-noahmp": "MYNN + Noah-MP", "urbano": "modello urbano"}
FISICHE_SOLO = {"castelli": {"radt6": "radiazione 6 min", "radt15": "radiazione 15 min"}}      # prove fatte solo in una zona


def modelli_confronto(zona):
    """Tutti i run della zona: i due ufficiali (rami risultati-*) e le prove (rami prove-*): ICON-EU, GFS e le fisiche."""
    m = [("ecmwf", "ECMWF", f"risultati-{zona}-ecmwf", FONTE_ECMWF), ("icon2i", "ICON-2I", f"risultati-{zona}-icon2i", FONTE_ICON2I),
         ("iconeu", "ICON-EU (prova)", f"prove-{zona}-iconeu", FONTE_ICONEU + " Prova, non pubblicata come previsione."),
         ("gfs", "GFS (prova)", f"prove-{zona}-gfs", FONTE_GFS + " Prova, non pubblicata come previsione.")]
    for pil, nome in (("ecmwf", "ECMWF"), ("icon2i", "ICON-2I")):
        for f, nf in {**FISICHE, **FISICHE_SOLO.get(zona, {})}.items():
            m.append((f"{pil}-{f}", f"{nome} · {nf}", f"prove-{zona}-{pil}-{f}",
                      (FONTE_ECMWF if pil == "ecmwf" else FONTE_ICON2I) + f" Prova di fisica ({nf}), non pubblicata come previsione."))
    return m


def blocco_modelli(voci, commento):
    r = f"// {commento}\nconst MODELLI = {{\n"
    for chiave, nome, ramo, fonte, *file in voci:
        r += f'  "{chiave}": {{ nome: "{nome}", base: "{R}/{ramo}/orarie/", file: "{file[0] if file else "wrf-" + chiave}", fonte: "{fonte}" }},\n'
    return r + "};\nlet modello = \"ecmwf\", BASE = MODELLI.ecmwf.base;"


def genera(base, z, confronto=False, chiave=""):
    t = base
    titolo = "Confronto modelli WRF" if confronto else "Previsioni WRF"
    CHIAVE_LS = "wrf_modello_confronto" if confronto else "wrf_modello"      # la scelta di una pagina non cambia l'altra
    t = sostituisci(t, "<title>Previsioni WRF Lazio</title>", f"<title>{titolo} {z['nome']}</title>")
    t = sostituisci(t, "<h1>Previsioni WRF · Lazio</h1>",
                    f"<h1>{titolo} · {z['nome']}</h1>\n  <div id=\"modelli\" class=\"righe\" role=\"group\" aria-label=\"Modello di partenza\"></div>")
    t = sostituisci(t, "  small { color: var(--sec);", "  #modelli { margin: 0 0 10px; }\n" + ("  #modelli { flex-wrap: wrap; overflow: visible; }\n" if confronto else "") + "  small { color: var(--sec);")
    t = sostituisci(
        t, '<small>Esperimento personale,',
        '<small id="fonte"></small>\n  <small>Esperimento personale,')
    if confronto:
        blocco = blocco_modelli(modelli_confronto(chiave), "Tutti i run: ufficiali (ECMWF, ICON-2I) e prove (ICON-EU, GFS, fisiche diverse). Le prove non sono previsioni pubblicate.")
    else:
        blocco = blocco_modelli([("ecmwf", "ECMWF", f"risultati-{chiave}-ecmwf", FONTE_ECMWF, z["f_ecmwf"]), ("icon2i", "ICON-2I", f"risultati-{chiave}-icon2i", FONTE_ICON2I, z["f_icon"])],
                                "Due modelli di partenza per lo stesso WRF: ECMWF IFS e ICON-2I (Agenzia ItaliaMeteo). Ognuno pubblica nel suo ramo.")
    t = sostituisci(t, 'const BASE = "https://raw.githubusercontent.com/Superbug87/wrf-lazio/risultati/orarie/";', blocco)
    t = sostituisci(
        t, "const cache = new Map();\n",
        "const cache = new Map();\n")
    t = sostituisci(
        t, "const url = (v, i) =>",
        "{                                             // modello: prima l'indirizzo (?modello=icon2i), poi l'ultimo scelto\n"
        "  const q = new URLSearchParams(location.search).get(\"modello\"), s = leggi(\"" + CHIAVE_LS + "\");\n"
        "  modello = [q, s].find(m => m && MODELLI[m]) || \"ecmwf\"; BASE = MODELLI[modello].base;\n"
        "}\n"
        "const url = (v, i) =>")
    t = sostituisci(t, "`wrf-lazio_${variabile}_${indice.ciclo}_${indice.ore[i]", "`${MODELLI[modello].file}_${variabile}_${indice.ciclo}_${indice.ore[i]")
    t = sostituisci(t, "`wrf-lazio_${variabile}_${indice.ciclo}.gif`", "`${MODELLI[modello].file}_${variabile}_${indice.ciclo}.gif`")
    t = sostituisci(
        t, "el(\"run\").onchange =",
        "function disegnaModelli() {\n"
        "  const box = el(\"modelli\"); box.textContent = \"\";\n"
        "  for (const [id, m] of Object.entries(MODELLI)) {\n"
        "    const b = document.createElement(\"button\");\n"
        "    b.type = \"button\"; b.textContent = m.nome; b.dataset.m = id; b.setAttribute(\"aria-pressed\", id === modello);\n"
        "    b.onclick = () => cambiaModello(id);\n"
        "    box.appendChild(b);\n"
        "  }\n"
        "  el(\"fonte\").textContent = MODELLI[modello].fonte;\n"
        "}\n"
        "async function cambiaModello(id) {\n"
        "  if (id === modello) return;\n"
        "  stop(); modello = id; salva(\"" + CHIAVE_LS + "\", id); BASE = MODELLI[id].base;\n"
        "  indice = null; cicli = []; sulUltimo = true; cache.clear();\n"
        "  el(\"runbox\").hidden = true; el(\"img\").removeAttribute(\"src\");\n"
        "  el(\"msg\").textContent = \"Carico…\"; el(\"msg\").style.display = \"grid\";\n"
        "  disegnaModelli();\n"
        "  await carica(true);\n"
        "}\n"
        "el(\"run\").onchange =")
    t = sostituisci(t, "carica(true);\nsetInterval", "disegnaModelli();\ncarica(true);\nsetInterval")
    return t


if __name__ == "__main__":
    base = Path(sys.argv[1]).read_text(encoding="utf-8")
    out = Path(sys.argv[2]); out.mkdir(parents=True, exist_ok=True)
    for chiave, z in ZONE.items():
        (out / z["uscita"]).write_text(genera(base, z, False, chiave), encoding="utf-8")
        print("scritta", out / z["uscita"])
        (out / z["uscita"].replace("previsioni-wrf", "confronto-wrf")).write_text(genera(base, z, True, chiave), encoding="utf-8")
        print("scritta", out / z["uscita"].replace("previsioni-wrf", "confronto-wrf"))
