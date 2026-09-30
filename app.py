import math
import re
import unicodedata
import urllib.parse
from datetime import date, time

import pandas as pd
import requests
import streamlit as st

# -----------------------------------------------------------------------------
# CONFIGURAZIONE PAGINA
# -----------------------------------------------------------------------------
st.set_page_config(page_title="Planner Tattico ISF", layout="wide", page_icon="🗺️")
st.title("🗺️ Planner Tattico di Territorio per ISF")

GIORNI = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]
STATI = ["In Bozza", "Confermato", "Rifiutato"]

COLONNE_ANAGRAFICA = [
    "ID_Medico", "Nome", "Cognome", "Provincia", "Citta", "Indirizzo",
    "Frequenza_Target_Giorni",
]
COLONNE_DISPONIBILITA = ["ID_Medico", "Giorno_Settimana", "Ora_Inizio", "Ora_Fine"]
COLONNE_VISITE = ["ID_Medico", "Data_Ultima_Visita"]


# -----------------------------------------------------------------------------
# FUNZIONI DI SUPPORTO
# -----------------------------------------------------------------------------
def segreto(nome):
    """Legge un valore dai Secrets di Streamlit (None se assente)."""
    try:
        return st.secrets[nome]
    except Exception:
        return None


def normalizza(testo):
    """Minuscolo e senza accenti: 'Lunedì' e 'lunedi' diventano uguali."""
    testo = unicodedata.normalize("NFKD", str(testo))
    testo = "".join(c for c in testo if not unicodedata.combining(c))
    return testo.strip().lower()


def testo_pulito(valore):
    return "" if pd.isna(valore) else str(valore).strip()


def orario_in_minuti(valore):
    """Converte '09:00', '9:00', '09:00:00', '9.30' in minuti dalla mezzanotte."""
    if pd.isna(valore):
        return None
    m = re.match(r"^\s*(\d{1,2})[:.](\d{2})", str(valore))
    if not m:
        return None
    ore, minuti = int(m.group(1)), int(m.group(2))
    if ore > 23 or minuti > 59:
        return None
    return ore * 60 + minuti


def minuti_in_orario(minuti):
    minuti = int(minuti)
    return f"{minuti // 60:02d}:{minuti % 60:02d}"


def leggi_data(valore):
    """Accetta 2026-08-15 oppure 15/08/2026."""
    if pd.isna(valore):
        return None
    testo = str(valore).strip()
    try:
        m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", testo)
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = re.match(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", testo)
        if m:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None
    return None


@st.cache_data(ttl=60, show_spinner=False)
def leggi_foglio(sheet_id, nome_foglio):
    url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq"
        f"?tqx=out:csv&sheet={urllib.parse.quote(nome_foglio)}"
    )
    df = pd.read_csv(url, dtype=str)
    df.columns = [str(c).strip() for c in df.columns]
    df = df.dropna(how="all")
    for col in df.columns:
        df[col] = df[col].apply(lambda x: x.strip() if isinstance(x, str) else x)
    return df


@st.cache_data(ttl=86400, show_spinner=False)
def geocodifica(indirizzo, chiave):
    r = requests.get(
        "https://api.openrouteservice.org/geocode/search",
        params={
            "api_key": chiave,
            "text": indirizzo,
            "boundary.country": "IT",
            "size": 1,
        },
        timeout=20,
    )
    r.raise_for_status()
    feats = r.json().get("features")
    if not feats:
        return None
    lon, lat = feats[0]["geometry"]["coordinates"]
    return (lon, lat)


@st.cache_data(ttl=3600, show_spinner=False)
def matrice_durate(coords, chiave):
    r = requests.post(
        "https://api.openrouteservice.org/v2/matrix/driving-car",
        json={"locations": [list(c) for c in coords], "metrics": ["duration"]},
        headers={"Authorization": chiave, "Content-Type": "application/json"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["durations"]


# -----------------------------------------------------------------------------
# ALGORITMO DI PIANIFICAZIONE
# -----------------------------------------------------------------------------
def pianifica(liberi, fissi, durate, inizio_giornata, fine_giornata, durata_visita, max_visite):
    """
    Costruisce la giornata passo dopo passo:
    - a ogni passo sceglie, tra i medici disponibili, quello raggiungibile per primo
      (a parità, quello più urgente) che rispetta la sua finestra oraria;
    - le visite 'fisse' (già confermate) non si spostano: le altre si incastrano nei buchi.
    Non è un ottimizzatore perfetto: è una buona euristica.
    """
    liberi = list(liberi)
    fissi = sorted(fissi, key=lambda a: a["inizio"])
    max_liberi = max(0, max_visite - len(fissi))
    n_liberi = 0
    agenda = []
    t = inizio_giornata
    pos = None

    def viaggio(a, b):
        return math.ceil(durate[a["i"]][b["i"]])

    while True:
        prossimo_fisso = fissi[0] if fissi else None
        migliore = None

        if n_liberi < max_liberi:
            for c in liberi:
                tv = 0 if pos is None else viaggio(pos, c)
                for ini, fin in c["finestre"]:
                    arrivo = max(t + tv, ini)
                    fine = arrivo + durata_visita
                    if fine > min(fin, fine_giornata):
                        continue
                    if prossimo_fisso and fine + viaggio(c, prossimo_fisso) > prossimo_fisso["inizio"]:
                        continue
                    chiave = (arrivo, -c["priorita"])
                    if migliore is None or chiave < migliore[0]:
                        migliore = (chiave, c, arrivo, tv)

        if migliore:
            _, c, arrivo, tv = migliore
            app = dict(c)
            app.update(inizio=arrivo, fine=arrivo + durata_visita, guida=tv, fisso=False)
            agenda.append(app)
            liberi.remove(c)
            n_liberi += 1
            t = arrivo + durata_visita
            pos = c
        elif prossimo_fisso:
            fissi.pop(0)
            tv = 0 if pos is None else viaggio(pos, prossimo_fisso)
            app = dict(prossimo_fisso)
            app.update(guida=tv, fisso=True)
            agenda.append(app)
            t = max(t, prossimo_fisso["fine"])
            pos = prossimo_fisso
        else:
            break

    return agenda


# -----------------------------------------------------------------------------
# CARICAMENTO DATI
# -----------------------------------------------------------------------------
sheet_id = segreto("SHEET_ID")
if not sheet_id:
    st.error("Manca SHEET_ID nei Secrets di Streamlit (vedi Fase 4 della guida).")
    st.stop()

try:
    df_anag = leggi_foglio(sheet_id, "Anagrafica")
    df_disp = leggi_foglio(sheet_id, "Disponibilita")
    df_visite = leggi_foglio(sheet_id, "Registro_Visite")
except Exception as e:
    st.error(
        "Non riesco a leggere il Google Sheet. Controlla l'ID nei Secrets, "
        "i nomi dei 3 fogli e che la condivisione sia 'Chiunque abbia il link'."
    )
    st.caption(f"Dettaglio tecnico: {e}")
    st.stop()

for nome, df, cols in [
    ("Anagrafica", df_anag, COLONNE_ANAGRAFICA),
    ("Disponibilita", df_disp, COLONNE_DISPONIBILITA),
    ("Registro_Visite", df_visite, COLONNE_VISITE),
]:
    mancanti = [c for c in cols if c not in df.columns]
    if mancanti:
        st.error(f"Nel foglio '{nome}' mancano le colonne: {', '.join(mancanti)}")
        st.stop()

# -----------------------------------------------------------------------------
# BARRA LATERALE
# -----------------------------------------------------------------------------
st.sidebar.header("⚙️ Impostazioni")

chiave_ors = segreto("ORS_API_KEY") or st.sidebar.text_input("API Key OpenRouteService", type="password")

province = sorted(df_anag["Provincia"].dropna().unique())
if not province:
    st.error("Il foglio Anagrafica non contiene province.")
    st.stop()

provincia_sel = st.sidebar.selectbox("Provincia", province)

citta_disponibili = sorted(
    df_anag[df_anag["Provincia"] == provincia_sel]["Citta"].dropna().unique()
)
citta_sel = st.sidebar.selectbox("Città", ["Tutte"] + citta_disponibili)

data_sel = st.sidebar.date_input("Giorno da pianificare", value=date.today())
giorno_sel = GIORNI[data_sel.weekday()]
st.sidebar.caption(f"Giorno della settimana: **{giorno_sel}**")

anticipo = st.sidebar.number_input("Anticipo (giorni): includi i medici in scadenza entro", 0, 60, 7)
durata_visita = st.sidebar.number_input("Durata di una visita (minuti)", 5, 120, 20)
ora_inizio = st.sidebar.time_input("Inizio giornata", value=time(9, 0))
ora_fine = st.sidebar.time_input("Fine giornata", value=time(18, 0))
max_visite = st.sidebar.number_input("Numero massimo di visite", 1, 15, 8)

# -----------------------------------------------------------------------------
# CALCOLO DELLA PROPOSTA
# -----------------------------------------------------------------------------
if st.sidebar.button("🚀 Calcola proposta agenda", type="primary"):
    if not chiave_ors:
        st.warning("Inserisci la API Key di OpenRouteService nella barra laterale.")
        st.stop()

    avvisi = []
    candidati = []

    medici = df_anag[df_anag["Provincia"] == provincia_sel]
    if citta_sel != "Tutte":
        medici = medici[medici["Citta"] == citta_sel]

    disp_giorno = df_disp[
        df_disp["Giorno_Settimana"].apply(
            lambda g: pd.notna(g) and normalizza(g) == normalizza(giorno_sel)
        )
    ]

    for _, r in medici.iterrows():
        id_m = r["ID_Medico"]

        # Finestre orarie del giorno scelto
        finestre = []
        for _, riga in disp_giorno[disp_giorno["ID_Medico"] == id_m].iterrows():
            ini = orario_in_minuti(riga["Ora_Inizio"])
            fin = orario_in_minuti(riga["Ora_Fine"])
            if ini is None or fin is None or fin <= ini:
                avvisi.append(f"Orario non valido per {id_m}: ignorata una riga di Disponibilita.")
                continue
            finestre.append((ini, fin))
        if not finestre:
            continue

        # Frequenza target del singolo medico
        freq = pd.to_numeric(r["Frequenza_Target_Giorni"], errors="coerce")
        freq = 30 if pd.isna(freq) or freq <= 0 else int(freq)

        # Ultima visita
        date_visite = [
            leggi_data(x)
            for x in df_visite[df_visite["ID_Medico"] == id_m]["Data_Ultima_Visita"]
        ]
        date_visite = [x for x in date_visite if x]
        if date_visite:
            giorni = (data_sel - max(date_visite)).days
            if giorni < 0:
                continue  # ultima visita successiva al giorno pianificato
            if giorni < freq - anticipo:
                continue  # non ancora in scadenza
            priorita = giorni / freq
        else:
            giorni = None
            priorita = 999.0  # mai visitato: massima urgenza

        nome_completo = f"{testo_pulito(r['Nome'])} {testo_pulito(r['Cognome'])}".strip()
        via = testo_pulito(r["Indirizzo"])
        citta = testo_pulito(r["Citta"])
        prov = testo_pulito(r["Provincia"])
        indirizzo_completo = ", ".join(x for x in [via, citta, prov] if x)

        candidati.append({
            "id": id_m,
            "nome": nome_completo,
            "indirizzo": indirizzo_completo,
            "finestre": finestre,
            "freq": freq,
            "giorni": giorni,
            "priorita": priorita,
        })

    # Azzera gli stati dei menu della proposta precedente
    for k in [k for k in st.session_state if k.startswith("stato_")]:
        del st.session_state[k]

    if not candidati:
        st.session_state["ctx"] = None
        st.session_state["agenda"] = []
        st.info("Nessun medico da visitare con questi criteri (provincia, città, giorno, scadenze).")
    else:
        try:
            with st.spinner("Calcolo indirizzi e tempi di guida..."):
                validi = []
                coords = []
                for c in candidati:
                    coord = geocodifica(f"{c['indirizzo']}, Italia", chiave_ors)
                    if coord is None:
                        avvisi.append(
                            f"Indirizzo non trovato per {c['nome']}: '{c['indirizzo']}'. Medico escluso."
                        )
                    else:
                        c["i"] = len(validi)
                        validi.append(c)
                        coords.append(coord)

                if not validi:
                    raise ValueError("Nessun indirizzo è stato trovato.")

                if len(validi) > 1:
                    grezze = matrice_durate(tuple(coords), chiave_ors)
                    durate = [
                        [(x if x is not None else 1800) / 60 for x in riga]
                        for riga in grezze
                    ]
                else:
                    durate = [[0.0]]
        except Exception as e:
            st.error("Errore nel calcolo dei percorsi. Controlla la API Key OpenRouteService e riprova.")
            st.caption(f"Dettaglio tecnico: {e}")
            st.stop()

        inizio_g = ora_inizio.hour * 60 + ora_inizio.minute
        fine_g = ora_fine.hour * 60 + ora_fine.minute
        st.session_state["ctx"] = {
            "data": data_sel,
            "giorno": giorno_sel,
            "provincia": provincia_sel,
            "citta": citta_sel,
            "candidati": validi,
            "durate": durate,
            "rifiutati": set(),
            "avvisi": avvisi,
            "inizio": inizio_g,
            "fine": fine_g,
            "durata_visita": int(durata_visita),
            "max_visite": int(max_visite),
        }
        st.session_state["agenda"] = pianifica(
            validi, [], durate, inizio_g, fine_g, int(durata_visita), int(max_visite)
        )

# -----------------------------------------------------------------------------
# VISUALIZZAZIONE AGENDA
# -----------------------------------------------------------------------------
ctx = st.session_state.get("ctx")
agenda = st.session_state.get("agenda", [])

if not ctx:
    st.info("Usa il pannello laterale e clicca 'Calcola proposta agenda'.")
    st.stop()

luogo = f"Provincia di {ctx['provincia']}"
if ctx["citta"] != "Tutte":
    luogo += f" — {ctx['citta']}"
st.subheader(f"📅 {ctx['giorno']} {ctx['data'].strftime('%d/%m/%Y')} — {luogo}")

for avviso in ctx["avvisi"]:
    st.warning(avviso)

if not agenda:
    st.info("Nessuna visita inseribile nella giornata con gli orari e i limiti impostati.")

for a in agenda:
    c1, c2, c3, c4 = st.columns([2, 4, 3, 2])
    c1.markdown(
        f"**{'🔒 ' if a.get('fisso') else ''}{minuti_in_orario(a['inizio'])}–{minuti_in_orario(a['fine'])}**"
    )
    c2.markdown(f"**{a['nome']}**  \n_{a['indirizzo']}_")
    if a["giorni"] is None:
        storico = "Mai visitato"
    else:
        storico = f"Ultima visita {a['giorni']} gg fa (target {a['freq']} gg)"
    c3.caption(f"{storico} · guida da tappa precedente: {a['guida']} min")
    c4.selectbox("Stato", STATI, key=f"stato_{a['id']}", label_visibility="collapsed")


def stato_di(a):
    return st.session_state.get(f"stato_{a['id']}", "In Bozza")


# -----------------------------------------------------------------------------
# RICALCOLO DOPO I RIFIUTI
# -----------------------------------------------------------------------------
st.caption("🔒 = visita confermata: l'orario resta fisso. Le altre si riorganizzano attorno a queste.")

if agenda and st.button("🔄 Ricalcola giornata (rimuove i rifiutati, mantiene i confermati)"):
    rifiutati_ora = {a["id"] for a in agenda if stato_di(a) == "Rifiutato"}
    ctx["rifiutati"] |= rifiutati_ora
    fissi = [a for a in agenda if stato_di(a) == "Confermato"]
    id_fissi = {a["id"] for a in fissi}
    liberi = [
        c for c in ctx["candidati"]
        if c["id"] not in ctx["rifiutati"] and c["id"] not in id_fissi
    ]
    st.session_state["agenda"] = pianifica(
        liberi, fissi, ctx["durate"], ctx["inizio"], ctx["fine"],
        ctx["durata_visita"], ctx["max_visite"],
    )
    st.rerun()

# Medici in scadenza non inseriti
id_in_agenda = {a["id"] for a in agenda}
esclusi = [
    c for c in ctx["candidati"]
    if c["id"] not in id_in_agenda and c["id"] not in ctx["rifiutati"]
]
if esclusi:
    with st.expander(f"Medici in scadenza NON inseriti oggi ({len(esclusi)})"):
        for c in esclusi:
            st.write(f"• {c['nome']} — manca spazio, orario compatibile o si è raggiunto il massimo di visite")

# -----------------------------------------------------------------------------
# RIEPILOGO, SALVATAGGIO E NAVIGAZIONE
# -----------------------------------------------------------------------------
attive = [a for a in agenda if stato_di(a) != "Rifiutato"]
if attive:
    st.divider()
    st.metric("Tempo totale di guida stimato", f"{sum(a['guida'] for a in attive)} min")

    confermati = [a for a in attive if stato_di(a) == "Confermato"]
    if confermati:
        df_out = pd.DataFrame({
            "ID_Medico": [a["id"] for a in confermati],
            "Data_Ultima_Visita": [ctx["data"].isoformat()] * len(confermati),
        })
        st.download_button(
            "⬇️ Scarica visite confermate (da copiare in Registro_Visite)",
            df_out.to_csv(index=False).encode("utf-8"),
            file_name=f"visite_{ctx['data'].isoformat()}.csv",
            mime="text/csv",
        )

    tappe = [urllib.parse.quote(a["indirizzo"], safe="") for a in attive]
    url_maps = "https://www.google.com/maps/dir/" + "/".join(tappe)
    st.markdown(f"👉 **[Apri il percorso su Google Maps]({url_maps})**")
    st.caption("Google Maps accetta un numero limitato di tappe (circa 10).")
