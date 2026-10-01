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
    "ID_Medico", "ID_Sede", "Nome", "Cognome", "Provincia", "Citta", "Indirizzo",
    "Frequenza_Target_Giorni",
]
COLONNE_DISPONIBILITA = ["ID_Medico", "ID_Sede", "Giorno_Settimana", "Ora_Inizio", "Ora_Fine"]
COLONNE_VISITE = ["ID_Medico", "Data_Ultima_Visita"]
COLONNE_APPUNTAMENTI = ["ID_Medico", "ID_Sede", "Data_Appuntamento", "Ora_Appuntamento"]


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


def sede_norm(valore):
    """Sede normalizzata: 'A', 'a ' -> 'a'. Vuoto resta vuoto."""
    return normalizza(testo_pulito(valore))


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


def minuti_in_time(minuti):
    minuti = max(0, min(int(minuti), 23 * 60 + 59))
    return time(minuti // 60, minuti % 60)


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
    - a ogni passo sceglie, tra i candidati disponibili, quello raggiungibile per primo
      (a parità, quello più urgente) che rispetta la sua finestra oraria;
    - le visite 'fisse' (confermate o già prese, con l'orario deciso dall'utente)
      non si spostano: le altre si incastrano nei buchi;
    - un medico viene inserito al massimo una volta al giorno, anche se ha più sedi.
    Non è un ottimizzatore perfetto: è una buona euristica.
    """
    liberi = list(liberi)
    fissi = sorted(fissi, key=lambda a: a["inizio"])
    ids_usati = {f["id"] for f in fissi}
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
                if c["id"] in ids_usati:
                    continue
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
            ids_usati.add(c["id"])
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

# Colonne di servizio per il confronto delle sedi
df_anag = df_anag.copy()
df_disp = df_disp.copy()
df_anag["_sede"] = df_anag["ID_Sede"].apply(sede_norm)
df_disp["_sede"] = df_disp["ID_Sede"].apply(sede_norm)

# Controlli di coerenza sui dati
avvisi_dati = []

duplicati = df_anag.duplicated(subset=["ID_Medico", "_sede"], keep="first")
if duplicati.any():
    for _, r in df_anag[duplicati].iterrows():
        avvisi_dati.append(
            f"Anagrafica: la coppia ID_Medico '{r['ID_Medico']}' + ID_Sede '{testo_pulito(r['ID_Sede'])}' "
            f"compare più volte. Viene usata solo la prima riga."
        )
    df_anag = df_anag[~duplicati]

sedi_valide = set(zip(df_anag["ID_Medico"], df_anag["_sede"]))
for _, r in df_disp.iterrows():
    if pd.isna(r["ID_Medico"]):
        continue
    if (r["ID_Medico"], r["_sede"]) not in sedi_valide:
        avvisi_dati.append(
            f"Disponibilita: la riga di {r['ID_Medico']} (sede '{testo_pulito(r['ID_Sede'])}', "
            f"{testo_pulito(r['Giorno_Settimana'])}) non corrisponde a nessuna sede in Anagrafica. "
            f"Riga ignorata."
        )

# Appuntamenti già presi (foglio facoltativo)
appuntamenti = []
try:
    df_app = leggi_foglio(sheet_id, "Appuntamenti")
    if all(c in df_app.columns for c in COLONNE_APPUNTAMENTI):
        for _, r in df_app.iterrows():
            if pd.isna(r["ID_Medico"]):
                continue
            d_app = leggi_data(r["Data_Appuntamento"])
            o_app = orario_in_minuti(r["Ora_Appuntamento"])
            if d_app is None or o_app is None:
                avvisi_dati.append(
                    f"Appuntamenti: la riga di {r['ID_Medico']} ha data o ora non valida. Riga ignorata."
                )
                continue
            appuntamenti.append({
                "id": r["ID_Medico"],
                "sede": sede_norm(r["ID_Sede"]),
                "data": d_app,
                "ora": o_app,
            })
    else:
        st.warning(
            "Foglio 'Appuntamenti' non trovato o con intestazioni diverse da "
            "ID_Medico, ID_Sede, Data_Appuntamento, Ora_Appuntamento: "
            "gli appuntamenti già presi non vengono considerati."
        )
except Exception:
    st.warning(
        "Foglio 'Appuntamenti' non leggibile: gli appuntamenti già presi non vengono considerati."
    )

if avvisi_dati:
    with st.expander(f"⚠️ Controllo dati: {len(avvisi_dati)} segnalazioni", expanded=False):
        for a_ in avvisi_dati:
            st.write(f"• {a_}")


def trova_sede(id_m, sede):
    """Riga di Anagrafica della sede indicata (o l'unica sede, se non specificata)."""
    righe = df_anag[df_anag["ID_Medico"] == id_m]
    if righe.empty:
        return None
    if sede:
        r2 = righe[righe["_sede"] == sede]
        return None if r2.empty else r2.iloc[0]
    if len(righe) == 1:
        return righe.iloc[0]
    return None


def giorni_da_ultima_visita(id_m, data_rif):
    ds = [
        leggi_data(x)
        for x in df_visite[df_visite["ID_Medico"] == id_m]["Data_Ultima_Visita"]
    ]
    ds = [x for x in ds if x]
    return (data_rif - max(ds)).days if ds else None


def nome_di(r):
    return f"{testo_pulito(r['Nome'])} {testo_pulito(r['Cognome'])}".strip()


def indirizzo_di(r):
    parti = [testo_pulito(r["Indirizzo"]), testo_pulito(r["Citta"]), testo_pulito(r["Provincia"])]
    return ", ".join(x for x in parti if x)


def frequenza_di(r):
    f = pd.to_numeric(r["Frequenza_Target_Giorni"], errors="coerce")
    return 30 if pd.isna(f) or f <= 0 else int(f)


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
citta_sel = st.sidebar.multiselect(
    "Città (nessuna selezionata = tutte)",
    citta_disponibili,
)

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
    durata_int = int(durata_visita)

    # Appuntamenti già presi: quelli di oggi diventano visite fisse,
    # quelli futuri bloccano il medico (non viene proposto prima)
    app_giorno = [x for x in appuntamenti if x["data"] == data_sel]
    ids_app_giorno = {x["id"] for x in app_giorno}
    ids_bloccati = {x["id"] for x in appuntamenti if x["data"] > data_sel}

    # Visite già prese per il giorno pianificato
    gia_inseriti = set()
    for x in app_giorno:
        if x["id"] in gia_inseriti:
            continue
        r = trova_sede(x["id"], x["sede"])
        if r is None:
            avvisi.append(
                f"Appuntamento di {x['id']} del giorno non inserito: sede '{x['sede']}' "
                f"non trovata in Anagrafica."
            )
            continue
        if r["Provincia"] != provincia_sel or (citta_sel and r["Citta"] not in citta_sel):
            avvisi.append(
                f"ℹ️ {nome_di(r)} ha un appuntamento alle {minuti_in_orario(x['ora'])} "
                f"a {testo_pulito(r['Citta'])}, ma è fuori dai filtri di provincia/città selezionati."
            )
            gia_inseriti.add(x["id"])
            continue
        gia_inseriti.add(x["id"])
        candidati.append({
            "id": x["id"],
            "key": f"{x['id']}|{r['_sede']}",
            "sede": testo_pulito(r["ID_Sede"]),
            "nome": nome_di(r),
            "indirizzo": indirizzo_di(r),
            "finestre": [(x["ora"], x["ora"] + durata_int)],
            "freq": frequenza_di(r),
            "giorni": giorni_da_ultima_visita(x["id"], data_sel),
            "priorita": 0.0,
            "preso": True,
            "inizio": x["ora"],
            "fine": x["ora"] + durata_int,
            "fisso": True,
        })

    # Proposte normali
    sedi = df_anag[df_anag["Provincia"] == provincia_sel]
    if citta_sel:
        sedi = sedi[sedi["Citta"].isin(citta_sel)]

    disp_giorno = df_disp[
        df_disp["Giorno_Settimana"].apply(
            lambda g: pd.notna(g) and normalizza(g) == normalizza(giorno_sel)
        )
    ]

    for _, r in sedi.iterrows():
        id_m = r["ID_Medico"]
        sede = r["_sede"]

        # Medico con appuntamento già preso (oggi o più avanti): non si propone
        if id_m in ids_bloccati or id_m in ids_app_giorno:
            continue

        # Finestre orarie del giorno scelto, solo per questa sede
        finestre = []
        righe_disp = disp_giorno[
            (disp_giorno["ID_Medico"] == id_m) & (disp_giorno["_sede"] == sede)
        ]
        for _, riga in righe_disp.iterrows():
            ini = orario_in_minuti(riga["Ora_Inizio"])
            fin = orario_in_minuti(riga["Ora_Fine"])
            if ini is None or fin is None or fin <= ini:
                avvisi.append(
                    f"Orario non valido per {id_m} (sede '{testo_pulito(r['ID_Sede'])}'): "
                    f"ignorata una riga di Disponibilita."
                )
                continue
            finestre.append((ini, fin))
        if not finestre:
            continue

        freq = frequenza_di(r)

        # Ultima visita: condivisa tra tutte le sedi del medico
        giorni = giorni_da_ultima_visita(id_m, data_sel)
        if giorni is not None:
            if giorni < 0:
                continue  # ultima visita successiva al giorno pianificato
            if giorni < freq - anticipo:
                continue  # non ancora in scadenza
            priorita = giorni / freq
        else:
            priorita = 999.0  # mai visitato: massima urgenza

        candidati.append({
            "id": id_m,
            "key": f"{id_m}|{sede}",
            "sede": testo_pulito(r["ID_Sede"]),
            "nome": nome_di(r),
            "indirizzo": indirizzo_di(r),
            "finestre": finestre,
            "freq": freq,
            "giorni": giorni,
            "priorita": priorita,
        })

    # Azzera stati e orari della proposta precedente
    for k in [k for k in st.session_state if k.startswith(("stato_", "ora_"))]:
        del st.session_state[k]

    if not candidati:
        st.session_state["ctx"] = None
        st.session_state["agenda"] = []
        st.info("Nessun medico da visitare con questi criteri (provincia, città, giorno, scadenze).")
        for avviso in avvisi:
            st.warning(avviso)
    else:
        try:
            with st.spinner("Calcolo indirizzi e tempi di guida..."):
                validi = []
                coords = []
                for c in candidati:
                    coord = geocodifica(f"{c['indirizzo']}, Italia", chiave_ors)
                    if coord is None:
                        avvisi.append(
                            f"Indirizzo non trovato per {c['nome']}: '{c['indirizzo']}'. Sede esclusa."
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

        fissi_iniziali = [c for c in validi if c.get("preso")]
        liberi_iniziali = [c for c in validi if not c.get("preso")]

        # Gli appuntamenti già presi partono come "Confermato"
        for c in fissi_iniziali:
            st.session_state[f"stato_{c['key']}"] = "Confermato"

        st.session_state["ctx"] = {
            "data": data_sel,
            "giorno": giorno_sel,
            "provincia": provincia_sel,
            "citta": list(citta_sel),  # lista vuota = tutte le città
            "candidati": validi,
            "durate": durate,
            "rifiutati": set(),  # ID dei medici rifiutati (valgono per tutte le sedi)
            "avvisi": avvisi,
            "inizio": inizio_g,
            "fine": fine_g,
            "durata_visita": durata_int,
            "max_visite": int(max_visite),
            "ver": 0,  # cambia a ogni ricalcolo: rinnova i campi orario
        }
        st.session_state["agenda"] = pianifica(
            liberi_iniziali, fissi_iniziali, durate, inizio_g, fine_g,
            durata_int, int(max_visite),
        )

# -----------------------------------------------------------------------------
# VISUALIZZAZIONE AGENDA
# -----------------------------------------------------------------------------
ctx = st.session_state.get("ctx")
agenda = st.session_state.get("agenda", [])

if not ctx:
    st.info("Usa il pannello laterale e clicca 'Calcola proposta agenda'.")
    st.stop()


def stato_di(a):
    return st.session_state.get(f"stato_{a['key']}", "In Bozza")


def chiave_ora(a):
    return f"ora_{a['key']}_{ctx['ver']}"


def inizio_scelto(a):
    """Orario di inizio attualmente impostato (in minuti)."""
    v = st.session_state.get(chiave_ora(a))
    if isinstance(v, time):
        return v.hour * 60 + v.minute
    return a["inizio"]


luogo = f"Provincia di {ctx['provincia']}"
if ctx["citta"]:
    luogo += " — " + ", ".join(ctx["citta"])
st.subheader(f"📅 {ctx['giorno']} {ctx['data'].strftime('%d/%m/%Y')} — {luogo}")

for avviso in ctx["avvisi"]:
    st.warning(avviso)

if not agenda:
    st.info("Nessuna visita inseribile nella giornata con gli orari e i limiti impostati.")

for a in agenda:
    c1, c2, c3, c4 = st.columns([2, 4, 3, 2])
    bloccato = stato_di(a) == "Confermato"
    c1.time_input(
        "Orario",
        value=minuti_in_time(a["inizio"]),
        step=60,
        key=chiave_ora(a),
        disabled=bloccato,
        label_visibility="collapsed",
    )
    if bloccato:
        c1.caption("🔒 confermato")
    etichetta_sede = f" · Sede {a['sede']}" if a.get("sede") else ""
    c2.markdown(f"**{a['nome']}**{etichetta_sede}  \n_{a['indirizzo']}_")
    if a.get("preso"):
        storico = "📌 Appuntamento già preso"
    elif a["giorni"] is None:
        storico = "Mai visitato"
    else:
        storico = f"Ultima visita {a['giorni']} gg fa (target {a['freq']} gg)"
    c3.caption(f"{storico} · guida da tappa precedente: {a['guida']} min")
    c4.selectbox("Stato", STATI, key=f"stato_{a['key']}", label_visibility="collapsed")

# -----------------------------------------------------------------------------
# CONTROLLI SUGLI ORARI IMPOSTATI
# -----------------------------------------------------------------------------
durata = ctx["durata_visita"]
attive = [a for a in agenda if stato_di(a) != "Rifiutato"]
attive.sort(key=inizio_scelto)

for a in attive:
    ini = inizio_scelto(a)
    if not any(ini >= f_ini and ini + durata <= f_fin for f_ini, f_fin in a["finestre"]):
        fasce = ", ".join(f"{minuti_in_orario(x)}–{minuti_in_orario(y)}" for x, y in a["finestre"])
        st.warning(
            f"⏰ {a['nome']}: l'orario {minuti_in_orario(ini)}–{minuti_in_orario(ini + durata)} "
            f"è fuori dalla disponibilità registrata ({fasce})."
        )

for prima, dopo in zip(attive, attive[1:]):
    if inizio_scelto(dopo) < inizio_scelto(prima) + durata:
        st.warning(
            f"⚠️ Sovrapposizione: {prima['nome']} ({minuti_in_orario(inizio_scelto(prima))}) "
            f"e {dopo['nome']} ({minuti_in_orario(inizio_scelto(dopo))})."
        )

# -----------------------------------------------------------------------------
# RICALCOLO
# -----------------------------------------------------------------------------
st.caption(
    "Modifica l'orario finché il medico non conferma. Con **Confermato** l'orario si blocca (🔒). "
    "**Ricalcola giornata** mantiene i confermati all'orario impostato, toglie i rifiutati "
    "e ricalcola le visite in bozza. Non ricaricare la pagina del browser: si perderebbe l'agenda."
)

if agenda and st.button("🔄 Ricalcola giornata"):
    ctx["rifiutati"] |= {a["id"] for a in agenda if stato_di(a) == "Rifiutato"}

    fissi = []
    for a in agenda:
        if stato_di(a) == "Confermato":
            ini = inizio_scelto(a)
            f = dict(a)
            f.update(inizio=ini, fine=ini + durata, fisso=True)
            fissi.append(f)

    id_fissi = {a["id"] for a in fissi}
    liberi = [
        c for c in ctx["candidati"]
        if c["id"] not in ctx["rifiutati"] and c["id"] not in id_fissi
    ]
    nuova = pianifica(
        liberi, fissi, ctx["durate"], ctx["inizio"], ctx["fine"],
        ctx["durata_visita"], ctx["max_visite"],
    )

    # Rinnova i campi orario: quelli vecchi vengono eliminati
    for k in [k for k in st.session_state if k.startswith("ora_")]:
        del st.session_state[k]
    ctx["ver"] += 1
    st.session_state["agenda"] = nuova
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
            sede_txt = f" (sede {c['sede']})" if c.get("sede") else ""
            st.write(
                f"• {c['nome']}{sede_txt} — manca spazio, orario compatibile "
                f"o si è raggiunto il massimo di visite"
            )

# -----------------------------------------------------------------------------
# RIEPILOGO, SALVATAGGIO E NAVIGAZIONE
# -----------------------------------------------------------------------------
if attive:
    st.divider()
    st.metric("Tempo totale di guida stimato (da ultima proposta)", f"{sum(a['guida'] for a in attive)} min")

    confermati = [a for a in attive if stato_di(a) == "Confermato"]
    if confermati:
        # Una riga per medico (la data di ultima visita è condivisa tra le sedi)
        id_confermati = list(dict.fromkeys(a["id"] for a in confermati))
        df_out = pd.DataFrame({
            "ID_Medico": id_confermati,
            "Data_Ultima_Visita": [ctx["data"].isoformat()] * len(id_confermati),
        })
        st.download_button(
            "⬇️ Scarica visite confermate (da copiare in Registro_Visite)",
            df_out.to_csv(index=False).encode("utf-8"),
            file_name=f"visite_{ctx['data'].isoformat()}.csv",
            mime="text/csv",
        )

    # Percorso nell'ordine degli orari impostati
    tappe = [urllib.parse.quote(a["indirizzo"], safe="") for a in attive]
    url_maps = "https://www.google.com/maps/dir/" + "/".join(tappe)
    st.markdown(f"👉 **[Apri il percorso su Google Maps]({url_maps})**")
    st.caption("Google Maps accetta un numero limitato di tappe (circa 10).")
