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


def genera(base, z):
    t = base
    t = sostituisci(t, "<title>Previsioni WRF Lazio</title>", f"<title>Previsioni WRF {z['nome']}</title>")
    t = sostituisci(t, "<h1>Previsioni WRF · Lazio</h1>",
                    f"<h1>Previsioni WRF · {z['nome']}</h1>\n  <div id=\"modelli\" class=\"righe\" role=\"group\" aria-label=\"Modello di partenza\"></div>")
    t = sostituisci(t, "  small { color: var(--sec);", "  #modelli { margin: 0 0 10px; }\n  small { color: var(--sec);")
    t = sostituisci(
        t, '<small>Esperimento personale,',
        '<small id="fonte"></small>\n  <small>Esperimento personale,')
    t = sostituisci(
        t, 'const BASE = "https://raw.githubusercontent.com/Superbug87/wrf-lazio/risultati/orarie/";',
        "// Due modelli di partenza per lo stesso WRF: ECMWF IFS e ICON-2I (Agenzia ItaliaMeteo). Ognuno pubblica nel suo repository.\n"
        "const MODELLI = {\n"
        f"  ecmwf: {{ nome: \"ECMWF\", base: \"{z['ecmwf']}\", file: \"{z['f_ecmwf']}\",\n"
        "           fonte: \"Dati di partenza: ECMWF IFS (open data, CC BY 4.0), elaborati e modificati con WRF.\" },\n"
        f"  icon2i: {{ nome: \"ICON-2I\", base: \"{z['icon']}\", file: \"{z['f_icon']}\",\n"
        "            fonte: \"Dati di partenza: ICON-2I, Agenzia ItaliaMeteo in cooperation with Arpae Emilia-Romagna Idro-Meteo-Clima Service (CC BY 4.0), elaborati e modificati con WRF.\" },\n"
        "};\n"
        "let modello = \"ecmwf\", BASE = MODELLI.ecmwf.base;")
    t = sostituisci(
        t, "const cache = new Map();\n",
        "const cache = new Map();\n")
    t = sostituisci(
        t, "const url = (v, i) =>",
        "{                                             // modello: prima l'indirizzo (?modello=icon2i), poi l'ultimo scelto\n"
        "  const q = new URLSearchParams(location.search).get(\"modello\"), s = leggi(\"wrf_modello\");\n"
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
        "  stop(); modello = id; salva(\"wrf_modello\", id); BASE = MODELLI[id].base;\n"
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
    for z in ZONE.values():
        (out / z["uscita"]).write_text(genera(base, z), encoding="utf-8")
        print("scritta", out / z["uscita"])
