#!/usr/bin/env python3
"""
diagnostiche.py - grandezze derivate da un'uscita di WRF, calcolate colonna per colonna.

Tutte le funzioni lavorano su colonne: array (n, nz) con il livello 0 in basso (come i livelli di WRF),
n = numero di colonne (celle) e nz = livelli verticali di massa. Nessuna dipendenza da WRF o da wrf-python:
servono solo numpy. Il modulo è identico nei progetti wrf-lazio e wrf-castelli.

Cosa calcola (le stime sono dichiarate nelle mappe):
  - temperatura a una pressione data (925, 850 hPa), con estrapolazione standard sotto il suolo
  - quota dello zero termico
  - copertura nuvolosa totale, bassa, media e alta (sovrapposizione massima-casuale)
  - CAPE e CIN di due parcelle: la più instabile (MU, nei primi 300 hPa) e lo strato mescolato (ML, 100 hPa)
  - altezza del livello di condensazione (LCL) della parcella ML
  - shear verticale 0-1, 0-3 e 0-6 km, elicità relativa alla tempesta (SRH) 0-1 e 0-3 km, moto Bunkers
  - indici di rischio: supercelle (SCP), tornado (STP), fulmini (da LPI e correnti ascendenti)
  - raffica "turbolenta" (metodo tipo ECMWF, Bechtold e Bidlot 2009)
  - neve fresca (cm) dalla precipitazione solida

Gli indici di rischio (SCP, STP) nascono su dati statunitensi e non sono tarati sull'Italia: dicono dove le
condizioni sono favorevoli, non danno probabilità.
"""
import numpy as np

G0, RD, CP, LV, EPS = 9.80665, 287.04, 1004.0, 2.5e6, 0.622
KAPPA = RD / CP
GAMMA_STD = 0.0065            # K/m, gradiente standard
VK = 0.4                      # costante di von Karman

# Quote (m sul suolo) a cui si leggono i venti per shear e SRH: il vento a 10 m e poi ogni 250 m fino a 6 km
QUOTE_VENTO = np.array([10.0] + [250.0 * i for i in range(1, 25)])


# ----------------------------------------------------------------------------
# Termodinamica di base
# ----------------------------------------------------------------------------
def p_sat(T):
    """Pressione di saturazione (Pa), T in K (Bolton 1980)."""
    tc = T - 273.15
    return 611.2 * np.exp(17.67 * tc / (tc + 243.5))


def r_sat(T, p):
    e = p_sat(T)
    return EPS * e / np.maximum(p - e, 1.0)


def td_da_e(e):
    """Punto di rugiada (K) dalla tensione di vapore e (Pa)."""
    g = np.log(np.maximum(e, 1e-3) / 611.2)
    return 243.5 * g / (17.67 - g) + 273.15


def t_lcl(T, Td):
    """Temperatura al livello di condensazione (K), formula di Bolton (1980)."""
    Td = np.minimum(Td, T)
    return 1.0 / (1.0 / (Td - 56.0) + np.log(T / Td) / 800.0) + 56.0


def teta_e(T, p, w):
    """Temperatura potenziale equivalente (K) di Bolton (1980, eq. 43); w = rapporto di mescolanza (kg/kg)."""
    e = w * p / (EPS + w)
    TL = t_lcl(T, td_da_e(e))
    r = w * 1000.0                                               # g/kg
    teta_l = T * (1e5 / np.maximum(p - e, 1.0)) ** KAPPA * (T / TL) ** (0.28 * w)
    return teta_l * np.exp((3.036 / TL - 0.00178) * r * (1.0 + 0.448 * w))


def salita_umida(T, p_da, p_a, passi=8):
    """Integra la pseudoadiabatica da p_da a p_a (p_a <= p_da). Tutti array della stessa forma."""
    def dT_dlnp(t, p):
        r = r_sat(t, p)
        return (RD * t + LV * r) / (CP + LV * LV * r * EPS / (RD * t * t))

    T = T.copy()
    p = p_da.copy()
    h = np.log(p_a / p_da) / passi
    for _ in range(passi):
        k1 = dT_dlnp(T, p)
        k2 = dT_dlnp(T + 0.5 * h * k1, p * np.exp(0.5 * h))
        T = T + h * k2
        p = p * np.exp(h)
    return T


# ----------------------------------------------------------------------------
# Livelli di pressione, zero termico, nuvole
# ----------------------------------------------------------------------------
def a_pressione(p, campo, livello_pa, t_suolo=None):
    """
    Valore di `campo` (n, nz) alla pressione `livello_pa`, interpolando in ln p tra i due livelli attorno.
    Dove il livello sta sotto il suolo (p del livello più basso < livello_pa) e `campo` è la temperatura
    (passare t_suolo = campo[:, 0]) si estrapola con il gradiente standard di 6,5 K/km.
    """
    n, nz = p.shape
    sotto = (p >= livello_pa).sum(axis=1)                       # quanti livelli stanno sotto la quota cercata
    k1 = np.clip(sotto, 1, nz - 1)[:, None]
    k0 = k1 - 1
    p0, p1 = (np.take_along_axis(p, k, 1)[:, 0] for k in (k0, k1))
    c0, c1 = (np.take_along_axis(campo, k, 1)[:, 0] for k in (k0, k1))
    f = np.clip(np.log(livello_pa / p0) / np.log(p1 / p0), 0.0, 1.0)
    out = c0 + (c1 - c0) * f
    if t_suolo is not None:
        sotto_suolo = p[:, 0] < livello_pa
        # T(p) = T0 (p/p0)^(Rd*gamma/g): il livello sta sotto il suolo, quindi l'aria "dentro la montagna"
        est = t_suolo * (livello_pa / p[:, 0]) ** (RD * GAMMA_STD / G0)
        out = np.where(sotto_suolo, est, out)
    return out


def zero_termico(tk, z):
    """
    Quota (m s.l.m.) dello zero termico: la prima salendo dal suolo in cui la temperatura scende sotto 0 °C.
    Se già il livello più basso è sotto zero, si estrapola verso il basso con 6,5 K/km (può stare sotto il suolo;
    mai sotto il livello del mare). Se non c'è zero termico (aria sempre sopra 0 °C) vale la quota del livello più alto.
    """
    n, nz = tk.shape
    sotto_zero = tk <= 273.15
    ha = sotto_zero.any(axis=1)
    k = np.where(ha, sotto_zero.argmax(axis=1), nz - 1)
    kp = np.maximum(k - 1, 0)
    z1 = np.take_along_axis(z, k[:, None], 1)[:, 0]
    z0 = np.take_along_axis(z, kp[:, None], 1)[:, 0]
    t1 = np.take_along_axis(tk, k[:, None], 1)[:, 0]
    t0 = np.take_along_axis(tk, kp[:, None], 1)[:, 0]
    dt = np.where(np.abs(t0 - t1) < 1e-6, 1e-6, t0 - t1)
    z_cross = z0 + (z1 - z0) * np.clip((t0 - 273.15) / dt, 0.0, 1.0)
    z_basso = z[:, 0] + (tk[:, 0] - 273.15) / GAMMA_STD         # livello più basso già sotto zero
    out = np.where(k == 0, z_basso, z_cross)
    out = np.where(ha, out, z[:, -1])
    return np.clip(out, 0.0, None)


def _copertura_strato(c, maschera):
    """Copertura (0-1) di uno strato con sovrapposizione massima-casuale; c: (n, nz) dal basso, maschera: bool (n, nz)."""
    n, nz = c.shape
    c = np.where(maschera, np.clip(c, 0.0, 1.0), 0.0)
    chiaro = np.ones(n)
    prec = np.zeros(n)
    for k in range(nz - 1, -1, -1):                              # dall'alto verso il basso
        ck = c[:, k]
        num = 1.0 - np.maximum(ck, prec)
        den = 1.0 - prec
        fattore = np.where(den < 1e-6, 0.0, num / np.maximum(den, 1e-6))
        chiaro = chiaro * fattore
        prec = ck
    return 1.0 - chiaro


def nuvolosita(cldfra, p, psfc):
    """Copertura nuvolosa (%) totale, bassa (sigma > 0,8), media (0,45-0,8) e alta (< 0,45) da CLDFRA."""
    sigma = p / psfc[:, None]
    ovunque = np.ones_like(sigma, bool)
    return {
        "nubi_tot": 100.0 * _copertura_strato(cldfra, ovunque),
        "nubi_bassa": 100.0 * _copertura_strato(cldfra, sigma > 0.8),
        "nubi_media": 100.0 * _copertura_strato(cldfra, (sigma <= 0.8) & (sigma > 0.45)),
        "nubi_alta": 100.0 * _copertura_strato(cldfra, sigma <= 0.45),
    }


# ----------------------------------------------------------------------------
# CAPE e CIN
# ----------------------------------------------------------------------------
def sale_parcella(p, tk, w, z, p0, T0, w0, k0):
    """
    Solleva una parcella che parte dal livello k0 (per colonna) con pressione p0, temperatura T0 e rapporto di
    mescolanza w0: secco-adiabatica fino al LCL e poi pseudoadiabatica. Ritorna CAPE e CIN (J/kg, entrambi >= 0)
    e il LCL (m sopra il suolo, formula di Espy: 125 m per grado di differenza tra T e Td alla partenza).

    CAPE = area positiva di galleggiamento sopra il livello di libera convezione (LFC, il primo livello sopra
    il LCL in cui la parcella è più calda dell'ambiente) fino a 100 hPa. CIN = area negativa dalla partenza al LFC;
    senza LFC la CAPE è 0 e la CIN è l'area negativa fino a 500 hPa. Temperatura virtuale per parcella e ambiente.
    """
    n, nz = p.shape
    e0 = w0 * p0 / (EPS + w0)
    Td0 = np.minimum(td_da_e(e0), T0)
    TL = t_lcl(T0, Td0)
    p_lcl = p0 * (TL / T0) ** (1.0 / KAPPA)

    Tp = tk.copy()
    wp = w.copy()
    sopra = np.zeros(n, bool)
    T_prec, p_prec = T0.copy(), p0.copy()
    for k in range(nz):
        pk = p[:, k]
        e_k = k0 == k
        T_prec = np.where(e_k, T0, T_prec)
        p_prec = np.where(e_k, p0, p_prec)
        attivo = k > k0
        if not attivo.any():
            continue
        if pk[attivo].max() < 9000.0 and k > 0:
            break
        secco = pk >= p_lcl
        T_secco = T0 * (pk / p0) ** KAPPA
        p_start = np.where(sopra, p_prec, p_lcl)
        T_start = np.where(sopra, T_prec, TL)
        T_umido = salita_umida(T_start, p_start, np.minimum(pk, p_start))
        Tk = np.where(secco, T_secco, T_umido)
        Tp[:, k] = np.where(attivo, Tk, Tp[:, k])
        wp[:, k] = np.where(attivo, np.where(secco, w0, r_sat(Tk, pk)), wp[:, k])
        sopra = np.where(attivo, ~secco, sopra)
        T_prec = np.where(attivo, Tk, T_prec)
        p_prec = np.where(attivo, pk, p_prec)

    tv_p = Tp * (1 + wp / EPS) / (1 + wp)
    tv_e = tk * (1 + w / EPS) / (1 + w)
    b = G0 * (tv_p - tv_e) / tv_e                                # galleggiamento (m/s2) a ogni livello
    liv = np.arange(nz)[None, :]
    b = np.where(liv > k0[:, None], b, 0.0)                      # sotto la partenza nessuna parcella
    b_med = 0.5 * (b[:, :-1] + b[:, 1:])
    dz = np.diff(z, axis=1)
    p_med = 0.5 * (p[:, :-1] + p[:, 1:])
    k_med = np.arange(nz - 1)[None, :]

    # LFC: primo livello sopra il LCL (e sopra la partenza) con galleggiamento positivo
    cand = (b > 0) & (p <= p_lcl[:, None]) & (liv > k0[:, None])
    ha_lfc = cand.any(axis=1)
    k_lfc = np.where(ha_lfc, cand.argmax(axis=1), nz)
    sopra_lfc = k_med >= (k_lfc[:, None] - 1)                    # il tratto che finisce nel livello LFC include l'attraversamento
    cape = np.where(sopra_lfc & (p_med > 10000.0) & (b_med > 0), b_med * dz, 0.0).sum(axis=1)
    cape = np.where(ha_lfc, cape, 0.0)

    sotto_lfc = np.where(ha_lfc[:, None], k_med < k_lfc[:, None], p_med > 50000.0)
    dopo_partenza = k_med >= k0[:, None]
    cin = -np.where(sotto_lfc & dopo_partenza & (b_med < 0), b_med * dz, 0.0).sum(axis=1)
    cin = np.minimum(cin, 1000.0)
    lcl_agl = np.clip(125.0 * (T0 - Td0), 0.0, 6000.0)
    return cape, cin, lcl_agl


def parcelle(p, tk, w, z):
    """CAPE/CIN della parcella più instabile (MU) e di quella dello strato mescolato (ML); LCL della ML."""
    n, nz = p.shape
    ps = p[:, 0]
    # --- più instabile: massimo di teta-e nei primi 300 hPa sopra il suolo
    te = teta_e(tk, p, w)
    nei_300 = p >= (ps - 30000.0)[:, None]
    k_mu = np.where(nei_300, te, -np.inf).argmax(axis=1)
    sel = lambda a: np.take_along_axis(a, k_mu[:, None], 1)[:, 0]
    cape_mu, cin_mu, _ = sale_parcella(p, tk, w, z, sel(p), sel(tk), sel(w), k_mu)
    # --- strato mescolato: medie (pesate con lo spessore in pressione) di teta e w nei primi 100 hPa
    nei_100 = p >= (ps - 10000.0)[:, None]
    dp = np.abs(np.gradient(p, axis=1))
    peso = np.where(nei_100, dp, 0.0)
    somma = np.maximum(peso.sum(axis=1), 1.0)
    teta = tk * (1e5 / p) ** KAPPA
    teta_m = (teta * peso).sum(axis=1) / somma
    w_m = (w * peso).sum(axis=1) / somma
    T0 = teta_m * (ps / 1e5) ** KAPPA
    cape_ml, cin_ml, lcl_ml = sale_parcella(p, tk, w, z, ps, T0, w_m, np.zeros(n, int))
    return {"cape_mu": cape_mu, "cin_mu": cin_mu, "cape_ml": cape_ml, "cin_ml": cin_ml, "lcl_ml": lcl_ml}


# ----------------------------------------------------------------------------
# Vento in quota, shear, SRH
# ----------------------------------------------------------------------------
def vento_a_quote(u, v, zagl, u10, v10, quote=QUOTE_VENTO):
    """Vento a quote fisse sopra il suolo. u, v, zagl: (n, nz); ritorna due array (nq, n). La prima quota è il vento a 10 m."""
    n, nz = zagl.shape
    U = np.empty((len(quote), n))
    V = np.empty((len(quote), n))
    U[0], V[0] = u10, v10
    for i, h in enumerate(quote[1:], start=1):
        sotto = (zagl < h).sum(axis=1)
        k1 = np.clip(sotto, 1, nz - 1)[:, None]
        k0 = k1 - 1
        z0, z1 = (np.take_along_axis(zagl, k, 1)[:, 0] for k in (k0, k1))
        f = np.clip((h - z0) / np.maximum(z1 - z0, 1e-3), 0.0, 1.0)
        for A, a in ((U, u), (V, v)):
            a0, a1 = (np.take_along_axis(a, k, 1)[:, 0] for k in (k0, k1))
            A[i] = a0 + (a1 - a0) * f
    return U, V


def shear_srh(U, V, quote=QUOTE_VENTO):
    """
    Shear verticale (m/s) 0-1, 0-3 e 0-6 km come differenza di vettori tra la quota e il vento a 10 m, e SRH
    (m2/s2) 0-1 e 0-3 km per una tempesta che si muove come il "right-mover" di Bunkers et al. (2000) (versione
    semplice: vento medio 0-6 km, shear tra la media 0-0,5 km e la media 5,5-6 km, deviazione di 7,5 m/s).
    """
    i = lambda h: int(np.argmin(np.abs(quote - h)))
    out = {}
    for nome, h in (("shear_01", 1000), ("shear_03", 3000), ("shear_06", 6000)):
        out[nome] = np.hypot(U[i(h)] - U[0], V[i(h)] - V[0])
    s6 = (quote <= 6000.0 + 1e-6)
    um, vm = U[s6].mean(axis=0), V[s6].mean(axis=0)
    basso = quote <= 500.0 + 1e-6
    alto = (quote >= 5500.0 - 1e-6) & (quote <= 6000.0 + 1e-6)
    sx = U[alto].mean(axis=0) - U[basso].mean(axis=0)
    sy = V[alto].mean(axis=0) - V[basso].mean(axis=0)
    mod = np.hypot(sx, sy)
    ok = mod > 0.1
    cx = um + np.where(ok, 7.5 * sy / np.where(ok, mod, 1.0), 0.0)     # right-mover: a destra dello shear
    cy = vm - np.where(ok, 7.5 * sx / np.where(ok, mod, 1.0), 0.0)
    for nome, h in (("srh_01", 1000), ("srh_03", 3000)):
        k = i(h)
        srh = np.zeros_like(um)
        for j in range(k):
            srh += (U[j + 1] - cx) * (V[j] - cy) - (U[j] - cx) * (V[j + 1] - cy)
        out[nome] = srh
    out["moto_x"], out["moto_y"] = cx, cy
    return out


# ----------------------------------------------------------------------------
# Indici di rischio (non tarati sull'Italia)
# ----------------------------------------------------------------------------
def indice_tornado(cape_ml, cin_ml, lcl_ml, srh_01, shear_06):
    """Significant Tornado Parameter a strato fisso (Thompson et al. 2012), con la parcella ML. Valori >= 1: ambiente favorevole."""
    t_cape = cape_ml / 1500.0
    t_lcl = np.clip((2000.0 - lcl_ml) / 1000.0, 0.0, 1.0)
    t_srh = srh_01 / 150.0
    t_shear = np.where(shear_06 < 12.5, 0.0, np.minimum(shear_06, 30.0) / 20.0)
    t_cin = np.clip((200.0 - cin_ml) / 150.0, 0.0, 1.0)
    return np.clip(t_cape * t_lcl * t_srh * t_shear * t_cin, 0.0, None)


def indice_supercella(cape_mu, cin_mu, srh_03, shear_06):
    """Supercell Composite Parameter (Thompson et al. 2004) con strati fissi. Valori >= 1: ambiente favorevole alle supercelle."""
    t_cape = cape_mu / 1000.0
    t_srh = srh_03 / 50.0
    t_shear = np.where(shear_06 < 10.0, 0.0, np.minimum(shear_06, 20.0) / 20.0)
    t_cin = np.where(cin_mu > 40.0, 40.0 / np.maximum(cin_mu, 1.0), 1.0)
    return np.clip(t_cape * t_srh * t_shear * t_cin, 0.0, None)


def indice_fulmini(w_max=None, graupel=None, lpi=None):
    """
    Indice di rischio fulmini (1 = fulmini probabili): il maggiore tra LPI/2 (Lightning Potential Index di WRF,
    J/kg) e (correnti ascendenti massime / 10 m/s) x (graupel in colonna / 1,5 kg/m2), cioe' il meccanismo
    ghiaccio-graupel che carica le nubi. Gli ingredienti sono i massimi dell'ora, che WRF scrive da solo.
    """
    parti = []
    if w_max is not None and graupel is not None:
        parti.append(np.clip(w_max, 0.0, None) / 10.0 * np.clip(graupel, 0.0, None) / 1.5)
    if lpi is not None:
        parti.append(np.clip(lpi, 0.0, None) / 2.0)
    if not parti:
        return None
    return np.maximum.reduce(parti)


# ----------------------------------------------------------------------------
# Raffiche e neve
# ----------------------------------------------------------------------------
def raffica_turbolenta(u_medio, ust, pblh, hfx, t2, psfc, fattore=1.0):
    """
    Raffica a 10 m (m/s) = vento medio + 7,71 u* f, con f = [1 + 0,5 (zi / (-k L))^(2/3)]^(1/3) se l'aria e'
    instabile (L < 0, lunghezza di Monin-Obukhov dal flusso di calore sensibile) e f = 1 altrimenti
    (Bechtold e Bidlot 2009, come nel modello dell'ECMWF). `fattore` tara l'intensita' del termine turbolento.
    """
    rho = psfc / (RD * t2)
    wt = hfx / (rho * CP)                                        # flusso cinematico di calore (K m/s)
    ust = np.maximum(ust, 0.01)
    instabile = wt > 1e-4
    L = -ust ** 3 * t2 / (VK * G0 * np.where(instabile, wt, 1.0))      # negativo se instabile
    zi = np.clip(pblh, 50.0, 3000.0)
    rapporto = np.where(instabile, zi / (-VK * L), 0.0)
    f = np.where(instabile, (1.0 + 0.5 * np.minimum(rapporto, 200.0) ** (2.0 / 3.0)) ** (1.0 / 3.0), 1.0)
    f = np.minimum(f, 2.0)
    return u_medio + fattore * 7.71 * ust * f


def neve_cm(solida_mm, t2_k):
    """
    Neve fresca (cm) dalla precipitazione solida oraria (mm di acqua), con densita' della neve fresca che cresce
    con la temperatura (Hedstrom e Pomeroy 1998): rho = 67,9 + 51,3 exp(T/2,6) kg/m3. Sopra 3 °C non si accumula.
    """
    tc = np.clip(t2_k - 273.15, -25.0, 3.0)
    rho = 67.9 + 51.3 * np.exp(tc / 2.6)
    cm = solida_mm * 100.0 / rho                                 # 1 mm di acqua su 1 m2 = 1 kg; spessore = kg / rho (m) x 100
    return np.where(t2_k - 273.15 > 3.0, 0.0, cm)
