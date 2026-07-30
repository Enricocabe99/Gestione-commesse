import streamlit as st
import sqlalchemy
from sqlalchemy import create_engine, text
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from datetime import datetime, timedelta
import hashlib
import io
import json
import os

# ==================== CONFIG ====================
def get_database_url():
    """Legge l'URL del database: prima dai secrets Streamlit, poi dall'ambiente."""
    try:
        return st.secrets["DATABASE_URL"]
    except (FileNotFoundError, KeyError):
        return os.getenv(
            "DATABASE_URL",
            "postgresql://postgres:password@localhost:5432/commesse"
        )

def get_engine():
    url = get_database_url()
    # Aggiunge SSL se è un URL di Supabase e non lo ha già
    if "supabase.co" in url and "sslmode" not in url:
        url += "?sslmode=require"
    return create_engine(url, pool_pre_ping=True)

# ==================== FUNZIONI DATABASE ====================
def init_db(engine):
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS utenti (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                nome TEXT NOT NULL,
                cognome TEXT NOT NULL,
                attivo INTEGER DEFAULT 1,
                is_admin INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS anni (
                id SERIAL PRIMARY KEY,
                anno INTEGER UNIQUE NOT NULL
            );

            CREATE TABLE IF NOT EXISTS commesse (
                id SERIAL PRIMARY KEY,
                anno_id INTEGER NOT NULL REFERENCES anni(id) ON DELETE CASCADE,
                nome TEXT NOT NULL,
                numero_identificativo TEXT NOT NULL,
                descrizione TEXT,
                data_inizio DATE,
                data_fine_prevista DATE,
                data_fine_effettiva DATE,
                note TEXT
            );

            CREATE TABLE IF NOT EXISTS sottolavori (
                id SERIAL PRIMARY KEY,
                commessa_id INTEGER NOT NULL REFERENCES commesse(id) ON DELETE CASCADE,
                nome TEXT NOT NULL,
                ingegnere_assegnato TEXT,
                stato TEXT DEFAULT 'In corso',
                data_inizio DATE,
                data_fine_prevista DATE,
                data_fine_effettiva DATE,
                note TEXT
            );

            CREATE TABLE IF NOT EXISTS attivita_personali (
                id SERIAL PRIMARY KEY,
                ingegnere TEXT NOT NULL,
                descrizione TEXT NOT NULL,
                stato TEXT DEFAULT 'In corso',
                data_inizio DATE,
                data_fine_prevista DATE,
                data_fine_effettiva DATE,
                note TEXT
            );

            CREATE TABLE IF NOT EXISTS cronologia (
                id SERIAL PRIMARY KEY,
                entita_tipo TEXT NOT NULL,
                entita_id INTEGER NOT NULL,
                data_modifica TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                dati_json TEXT NOT NULL
            );
        """))
        conn.commit()

    # Indice unico
    with engine.connect() as conn:
        try:
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS idx_commesse_numero ON commesse(numero_identificativo)"))
            conn.commit()
        except:
            pass  # già presente o ignorato

    # Admin predefinito
    with engine.connect() as conn:
        result = conn.execute(text("SELECT COUNT(*) FROM utenti")).fetchone()
        if result[0] == 0:
            pw_hash = hashlib.sha256("admin".encode()).hexdigest()
            conn.execute(text("INSERT INTO utenti (username, password_hash, nome, cognome, attivo, is_admin) VALUES (:u, :pw, :n, :c, 1, 1)"),
                         {"u": "admin", "pw": pw_hash, "n": "Admin", "c": "Sistema"})
            conn.commit()

# ==================== LOGIN / UTENTI ====================
def verifica_login(engine, username, password):
    pw_hash = hashlib.sha256(password.encode()).hexdigest()
    with engine.connect() as conn:
        user = conn.execute(text("SELECT * FROM utenti WHERE username=:u AND password_hash=:pw AND attivo=1"),
                            {"u": username, "pw": pw_hash}).fetchone()
    return user

def ottieni_utenti_attivi(engine):
    return pd.read_sql_query("SELECT username, nome, cognome FROM utenti WHERE attivo=1", engine)

def ottieni_tutti_utenti(engine):
    return pd.read_sql_query("SELECT * FROM utenti", engine)

def aggiungi_utente(engine, username, password, nome, cognome, is_admin=0):
    pw_hash = hashlib.sha256(password.encode()).hexdigest()
    with engine.connect() as conn:
        try:
            conn.execute(text("INSERT INTO utenti (username, password_hash, nome, cognome, attivo, is_admin) VALUES (:u, :pw, :n, :c, 1, :adm)"),
                         {"u": username, "pw": pw_hash, "n": nome, "c": cognome, "adm": is_admin})
            conn.commit()
            return True, "Utente creato."
        except sqlalchemy.exc.IntegrityError:
            return False, "Username già esistente."

def aggiorna_stato_utente(engine, username, attivo):
    with engine.connect() as conn:
        conn.execute(text("UPDATE utenti SET attivo=:att WHERE username=:u"),
                     {"att": attivo, "u": username})
        conn.commit()

# ==================== CRONOLOGIA ====================
def salva_cronologia(engine, entita_tipo, entita_id, dati_dict):
    with engine.connect() as conn:
        conn.execute(text("INSERT INTO cronologia (entita_tipo, entita_id, dati_json) VALUES (:tipo, :id, :json)"),
                     {"tipo": entita_tipo, "id": entita_id, "json": json.dumps(dati_dict, default=str)})
        conn.commit()

# ==================== CRUD ANNI ====================
def ottieni_o_crea_anno(engine, anno):
    try:
        anno_int = int(anno)
    except ValueError:
        return None
    with engine.connect() as conn:
        row = conn.execute(text("SELECT id FROM anni WHERE anno=:a"), {"a": anno_int}).fetchone()
        if row:
            return row[0]
        new_id = conn.execute(text("INSERT INTO anni (anno) VALUES (:a) RETURNING id"), {"a": anno_int}).fetchone()[0]
        conn.commit()
        return new_id

def leggi_anni(engine):
    return pd.read_sql_query("SELECT * FROM anni ORDER BY anno DESC", engine)

# ==================== CRUD COMMESSE ====================
def aggiungi_commessa(engine, anno_id, nome, numero_id, data_inizio, data_fine_prevista):
    with engine.connect() as conn:
        try:
            new_id = conn.execute(text("""
                INSERT INTO commesse (anno_id, nome, numero_identificativo, data_inizio, data_fine_prevista)
                VALUES (:aid, :nome, :num, :di, :df)
                RETURNING id
            """), {"aid": anno_id, "nome": nome, "num": numero_id, "di": data_inizio, "df": data_fine_prevista}).fetchone()[0]
            conn.commit()
            dati = {"anno_id": anno_id, "nome": nome, "numero_identificativo": numero_id, "data_inizio": str(data_inizio)}
            salva_cronologia(engine, "commessa", new_id, dati)
            return True, "Commessa aggiunta."
        except sqlalchemy.exc.IntegrityError:
            return False, "Numero identificativo già esistente."

def aggiorna_commessa(engine, commessa_id, **campi):
    campi_validi = ["nome", "numero_identificativo", "descrizione", "data_inizio", "data_fine_prevista", "data_fine_effettiva", "note"]
    set_clause = ", ".join(f"{k}=:{k}" for k in campi if k in campi_validi and k in campi)
    if not set_clause:
        return False, "Nessun campo valido."
    params = {k: v for k, v in campi.items() if k in campi_validi}
    params["cid"] = commessa_id
    with engine.connect() as conn:
        conn.execute(text(f"UPDATE commesse SET {set_clause} WHERE id=:cid"), params)
        conn.commit()
        row = conn.execute(text("SELECT * FROM commesse WHERE id=:cid"), {"cid": commessa_id}).fetchone()
        if row:
            dati = {"id": row[0], "nome": row[2], "numero_identificativo": row[3]}
            salva_cronologia(engine, "commessa", commessa_id, dati)
    return True, "Commessa aggiornata."

def elimina_commessa(engine, commessa_id):
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM commesse WHERE id=:cid"), {"cid": commessa_id})
        conn.commit()

def leggi_tutte_commesse(engine):
    return pd.read_sql_query("""
        SELECT c.*, a.anno FROM commesse c
        LEFT JOIN anni a ON c.anno_id = a.id
        ORDER BY a.anno DESC, c.id
    """, engine)

# ==================== CRUD SOTTOLAVORI ====================
def aggiungi_sottolavoro(engine, commessa_id, nome, ingegnere, data_inizio, data_fine_prevista, note, stato="In corso"):
    with engine.connect() as conn:
        new_id = conn.execute(text("""
            INSERT INTO sottolavori (commessa_id, nome, ingegnere_assegnato, stato, data_inizio, data_fine_prevista, note)
            VALUES (:cid, :nome, :ing, :stato, :di, :df, :note)
            RETURNING id
        """), {"cid": commessa_id, "nome": nome, "ing": ingegnere, "stato": stato, "di": data_inizio, "df": data_fine_prevista, "note": note}).fetchone()[0]
        conn.commit()
        dati = {"commessa_id": commessa_id, "nome": nome, "ingegnere": ingegnere, "stato": stato}
        salva_cronologia(engine, "sottolavoro", new_id, dati)
        return True, "Sottolavoro aggiunto."

def aggiorna_sottolavoro(engine, sottolavoro_id, **campi):
    campi_validi = ["nome", "ingegnere_assegnato", "stato", "data_inizio", "data_fine_prevista", "data_fine_effettiva", "note"]
    set_clause = ", ".join(f"{k}=:{k}" for k in campi if k in campi_validi and k in campi)
    if not set_clause:
        return False, "Nessun campo valido."
    params = {k: v for k, v in campi.items() if k in campi_validi}
    params["sid"] = sottolavoro_id
    with engine.connect() as conn:
        conn.execute(text(f"UPDATE sottolavori SET {set_clause} WHERE id=:sid"), params)
        conn.commit()
        row = conn.execute(text("SELECT * FROM sottolavori WHERE id=:sid"), {"sid": sottolavoro_id}).fetchone()
        if row:
            dati = {"id": row[0], "nome": row[2], "ingegnere": row[3], "stato": row[4]}
            salva_cronologia(engine, "sottolavoro", sottolavoro_id, dati)
    return True, "Sottolavoro aggiornato."

def elimina_sottolavoro(engine, sottolavoro_id):
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM sottolavori WHERE id=:sid"), {"sid": sottolavoro_id})
        conn.commit()

def leggi_sottolavori_per_commessa(engine, commessa_id):
    return pd.read_sql_query("SELECT * FROM sottolavori WHERE commessa_id=:cid ORDER BY id", engine, params={"cid": commessa_id})

def leggi_tutti_sottolavori(engine):
    return pd.read_sql_query("""
        SELECT s.*, c.nome as commessa_nome, c.numero_identificativo, a.anno
        FROM sottolavori s
        JOIN commesse c ON s.commessa_id = c.id
        LEFT JOIN anni a ON c.anno_id = a.id
        ORDER BY a.anno DESC, c.nome, s.nome
    """, engine)

# ==================== ATTIVITÀ PERSONALI ====================
def aggiungi_attivita_personale(engine, ingegnere, descrizione, data_inizio, data_fine_prevista, note, stato="In corso"):
    with engine.connect() as conn:
        new_id = conn.execute(text("""
            INSERT INTO attivita_personali (ingegnere, descrizione, stato, data_inizio, data_fine_prevista, note)
            VALUES (:ing, :descr, :st, :di, :df, :note)
            RETURNING id
        """), {"ing": ingegnere, "descr": descrizione, "st": stato, "di": data_inizio, "df": data_fine_prevista, "note": note}).fetchone()[0]
        conn.commit()
        dati = {"ingegnere": ingegnere, "descrizione": descrizione, "stato": stato}
        salva_cronologia(engine, "personale", new_id, dati)
        return True, "Attività personale aggiunta."

def aggiorna_attivita_personale(engine, att_id, **campi):
    campi_validi = ["descrizione", "stato", "data_inizio", "data_fine_prevista", "data_fine_effettiva", "note", "ingegnere"]
    set_clause = ", ".join(f"{k}=:{k}" for k in campi if k in campi_validi and k in campi)
    if not set_clause:
        return False, "Nessun campo valido."
    params = {k: v for k, v in campi.items() if k in campi_validi}
    params["aid"] = att_id
    with engine.connect() as conn:
        conn.execute(text(f"UPDATE attivita_personali SET {set_clause} WHERE id=:aid"), params)
        conn.commit()
    return True, "Aggiornato."

def elimina_attivita_personale(engine, att_id):
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM attivita_personali WHERE id=:aid"), {"aid": att_id})
        conn.commit()

def leggi_attivita_personali(engine, ingegnere=None):
    if ingegnere:
        return pd.read_sql_query("SELECT * FROM attivita_personali WHERE ingegnere=:ing ORDER BY id", engine, params={"ing": ingegnere})
    return pd.read_sql_query("SELECT * FROM attivita_personali ORDER BY id", engine)

# ==================== STORICO ====================
def stato_alla_data(engine, entita_tipo, entita_id, data_limite):
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT dati_json FROM cronologia
            WHERE entita_tipo = :tipo AND entita_id = :eid AND data_modifica <= :limite
            ORDER BY data_modifica DESC LIMIT 1
        """), {"tipo": entita_tipo, "eid": entita_id, "limite": data_limite}).fetchone()
        if row:
            return json.loads(row[0])
    return None

def tutte_entita_alla_data(engine, data_limite):
    commesse = []
    for comm in leggi_tutte_commesse(engine).to_dict('records'):
        stato_prec = stato_alla_data(engine, "commessa", comm['id'], data_limite)
        if stato_prec:
            commesse.append(stato_prec)
    sottolavori = []
    for sott in leggi_tutti_sottolavori(engine).to_dict('records'):
        stato_prec = stato_alla_data(engine, "sottolavoro", sott['id'], data_limite)
        if stato_prec:
            sottolavori.append(stato_prec)
    att_pers = []
    for att in leggi_attivita_personali(engine).to_dict('records'):
        stato_prec = stato_alla_data(engine, "personale", att['id'], data_limite)
        if stato_prec:
            att_pers.append(stato_prec)
    return commesse, sottolavori, att_pers

# ==================== BACKUP ====================
def esporta_excel(engine):
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        leggi_anni(engine).to_excel(writer, sheet_name='Anni', index=False)
        leggi_tutte_commesse(engine).to_excel(writer, sheet_name='Commesse', index=False)
        leggi_tutti_sottolavori(engine).to_excel(writer, sheet_name='Sottolavori', index=False)
        leggi_attivita_personali(engine).to_excel(writer, sheet_name='Attività_Personali', index=False)
        pd.read_sql_query("SELECT * FROM cronologia", engine).to_excel(writer, sheet_name='Cronologia', index=False)
        ottieni_tutti_utenti(engine).to_excel(writer, sheet_name='Utenti', index=False)
    return output.getvalue()

# ==================== INTERFACCIA ====================
def pagina_login(engine):
    st.title("🔐 Accesso")
    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        if st.form_submit_button("Accedi"):
            user = verifica_login(engine, username, password)
            if user:
                st.session_state['autenticato'] = True
                st.session_state['username'] = user[1]
                st.session_state['nome_completo'] = f"{user[3]} {user[4]}"
                st.session_state['is_admin'] = bool(user[6])
                st.rerun()
            else:
                st.error("Credenziali errate o account disattivato.")

def main():
    st.set_page_config(page_title="Commesse Strutture", layout="wide", initial_sidebar_state="expanded")
    st.markdown("""
    <style>
    .metric-card {
        background: #f8f9fa; border-radius: 10px; padding: 20px; text-align: center;
        box-shadow: 0 2px 4px rgba(0,0,0,0.1);
    }
    </style>
    """, unsafe_allow_html=True)

    engine = get_engine()
    init_db(engine)

    if 'autenticato' not in st.session_state or not st.session_state['autenticato']:
        pagina_login(engine)
        return

    st.sidebar.title(f"👤 {st.session_state['nome_completo']}")
    if st.sidebar.button("Logout"):
        st.session_state.clear()
        st.rerun()
    st.sidebar.markdown("---")

    menu_options = ["Commesse", "Attività Personali", "Resoconto", "Backup"]
    if st.session_state['is_admin']:
        menu_options.append("Amministrazione Utenti")
    scelta = st.sidebar.radio("Naviga", menu_options)

    # -------------------- SEZIONE COMMESSE --------------------
    if scelta == "Commesse":
        st.header("📁 Gestione Commesse")
        ingegneri_attivi = ottieni_utenti_attivi(engine)['username'].tolist()

        if 'selected_commessa_id' not in st.session_state:
            st.session_state.selected_commessa_id = None
        if 'show_delete_dialog' not in st.session_state:
            st.session_state.show_delete_dialog = None

        # Box di conferma eliminazione
        if st.session_state.show_delete_dialog is not None:
            with st.container():
                st.markdown("---")
                st.warning("⚠️ Vuoi davvero eliminare questa commessa? Tutti i suoi sottolavori andranno persi.")
                col_confirm, col_cancel = st.columns(2)
                if col_confirm.button("🗑️ Elimina", key="exec_delete_commessa"):
                    elimina_commessa(engine, st.session_state.show_delete_dialog)
                    if st.session_state.selected_commessa_id == st.session_state.show_delete_dialog:
                        st.session_state.selected_commessa_id = None
                    st.session_state.show_delete_dialog = None
                    st.rerun()
                if col_cancel.button("Annulla", key="cancel_delete_commessa"):
                    st.session_state.show_delete_dialog = None
                    st.rerun()

        # Tab
        tab_albero, tab_riepilogo, tab_ingegneri = st.tabs(["🛠️ Lavori", "📋 Riepilogo", "👷 Ingegneri"])

        # ==================== TAB ALBERO ====================
        with tab_albero:
            with st.expander("➕ Nuova Commessa", expanded=False):
                with st.form("crea_commessa_form", clear_on_submit=True):
                    col1, col2, col3, col4, col5 = st.columns(5)
                    with col1:
                        anno_input = st.text_input("Anno *", value=str(datetime.today().year))
                    with col2:
                        num_id = st.text_input("Numero identificativo *")
                    with col3:
                        nome_comm = st.text_input("Nome commessa *")
                    with col4:
                        data_in = st.date_input("Data inizio", datetime.today())
                    with col5:
                        data_fine = st.date_input("Data fine prevista")
                    submitted = st.form_submit_button("Crea commessa")
                    if submitted:
                        if not nome_comm or not num_id or not anno_input.strip():
                            st.error("I campi contrassegnati con * sono obbligatori.")
                        else:
                            anno_id = ottieni_o_crea_anno(engine, anno_input.strip())
                            if anno_id is None:
                                st.error("L'anno deve essere un numero valido.")
                            else:
                                ok, msg = aggiungi_commessa(engine, anno_id, nome_comm, num_id, data_in, data_fine)
                                if ok:
                                    st.success(msg)
                                    st.rerun()
                                else:
                                    st.error(msg)

            st.markdown("---")
            col_albero, col_dettaglio = st.columns([1, 3])

            with col_albero:
                st.subheader("🗂️ Commesse")
                search_query = st.text_input("🔍 Cerca commessa", placeholder="Nome o numero...", key="search_albero")

                col_espandi, col_collassa = st.columns(2)
                with col_espandi:
                    if st.button("📂 Espandi tutti", key="expand_all"):
                        tutte = leggi_tutte_commesse(engine)
                        for _, row in tutte.iterrows():
                            st.session_state[f"exp_comm_{row['id']}"] = True
                        st.rerun()
                with col_collassa:
                    if st.button("📁 Collassa tutti", key="collapse_all"):
                        tutte = leggi_tutte_commesse(engine)
                        for _, row in tutte.iterrows():
                            st.session_state[f"exp_comm_{row['id']}"] = False
                        st.rerun()

                tutte_commesse = leggi_tutte_commesse(engine)
                if not tutte_commesse.empty:
                    if search_query.strip():
                        query = search_query.strip().lower()
                        filtered = tutte_commesse[
                            tutte_commesse['nome'].str.lower().str.contains(query) |
                            tutte_commesse['numero_identificativo'].str.lower().str.contains(query)
                        ]
                    else:
                        filtered = tutte_commesse

                    if filtered.empty:
                        st.info("Nessuna commessa trovata.")
                    else:
                        for _, comm_row in filtered.iterrows():
                            comm_id = comm_row['id']
                            sott_comm = leggi_sottolavori_per_commessa(engine, comm_id)
                            totale = len(sott_comm)
                            completati = len(sott_comm[sott_comm['stato'] == 'Completato']) if totale > 0 else 0
                            pallino = "🟢" if totale > 0 and completati == totale else "🔴"
                            anno_str = f" ({int(comm_row['anno'])})" if pd.notna(comm_row['anno']) else ""
                            label_comm = f"{pallino} {comm_row['numero_identificativo']} - {comm_row['nome']}{anno_str}"

                            if f"exp_comm_{comm_id}" not in st.session_state:
                                st.session_state[f"exp_comm_{comm_id}"] = False

                            col_toggle, col_name, col_del = st.columns([0.08, 0.82, 0.1])
                            with col_toggle:
                                if st.button("▾" if st.session_state[f"exp_comm_{comm_id}"] else "▸",
                                             key=f"tog_comm_{comm_id}"):
                                    st.session_state[f"exp_comm_{comm_id}"] = not st.session_state[f"exp_comm_{comm_id}"]
                                    st.rerun()
                            with col_name:
                                if st.button(label_comm, key=f"sel_{comm_id}"):
                                    st.session_state.selected_commessa_id = comm_id
                                    st.rerun()
                            with col_del:
                                if st.button("🗑️", key=f"del_comm_{comm_id}"):
                                    st.session_state.show_delete_dialog = comm_id
                                    st.rerun()

                            if st.session_state[f"exp_comm_{comm_id}"] and not sott_comm.empty:
                                tree_html = '<div style="margin-left: 40px; border-left: 1px solid #aaa; padding-left: 15px;">'
                                for j, (_, sott_row) in enumerate(sott_comm.iterrows()):
                                    pallino_sl = "🟢" if sott_row['stato'] == 'Completato' else "🔴"
                                    stato_str = sott_row['stato']
                                    ing_str = sott_row['ingegnere_assegnato']
                                    nome_sl = sott_row['nome']
                                    ramo = "└─" if j == len(sott_comm)-1 else "├─"
                                    tree_html += f"<div style='margin:2px 0;'><span style='font-family: monospace;'>{ramo} {pallino_sl} {nome_sl} ({stato_str}) - Ing. {ing_str}</span></div>"
                                tree_html += '</div>'
                                st.markdown(tree_html, unsafe_allow_html=True)
                else:
                    st.info("Nessuna commessa presente. Creane una con il pulsante sopra.")

            with col_dettaglio:
                if st.session_state.selected_commessa_id is None:
                    st.info("Clicca sul nome di una commessa nell'albero per visualizzare/modificare i sottolavori.")
                else:
                    comm_id = st.session_state.selected_commessa_id
                    df_comm = pd.read_sql_query("SELECT c.*, a.anno FROM commesse c LEFT JOIN anni a ON c.anno_id=a.id WHERE c.id=:cid", engine, params={"cid": comm_id})
                    if df_comm.empty:
                        st.warning("Commessa non trovata.")
                        st.session_state.selected_commessa_id = None
                        st.rerun()

                    comm_row = df_comm.iloc[0]

                    if f"edit_commessa_{comm_id}" not in st.session_state:
                        st.session_state[f"edit_commessa_{comm_id}"] = False

                    if st.session_state[f"edit_commessa_{comm_id}"]:
                        with st.form(key=f"mod_commessa_{comm_id}"):
                            col1, col2 = st.columns(2)
                            with col1:
                                nuovo_numero = st.text_input("Numero identificativo", value=comm_row['numero_identificativo'])
                            with col2:
                                nuovo_nome = st.text_input("Nome commessa", value=comm_row['nome'])
                            anno_corrente = int(comm_row['anno']) if pd.notna(comm_row['anno']) else datetime.today().year
                            nuovo_anno = st.text_input("Anno", value=str(anno_corrente))
                            note_correnti = comm_row['note'] if comm_row['note'] else ""
                            nuove_note = st.text_area("Note", value=note_correnti, height=100)
                            col_salva, col_annulla = st.columns(2)
                            with col_salva:
                                if st.form_submit_button("💾 Salva modifiche"):
                                    if not nuovo_numero or not nuovo_nome or not nuovo_anno.strip():
                                        st.error("I campi numero, nome e anno sono obbligatori.")
                                    else:
                                        anno_id = ottieni_o_crea_anno(engine, nuovo_anno.strip())
                                        if anno_id is None:
                                            st.error("L'anno deve essere un numero valido.")
                                        else:
                                            aggiorna_commessa(engine, comm_id,
                                                nome=nuovo_nome,
                                                numero_identificativo=nuovo_numero,
                                                note=nuove_note)
                                            with engine.connect() as conn:
                                                conn.execute(text("UPDATE commesse SET anno_id = :aid WHERE id = :cid"), {"aid": anno_id, "cid": comm_id})
                                                conn.commit()
                                            st.success("Commessa aggiornata")
                                            st.session_state[f"edit_commessa_{comm_id}"] = False
                                            st.rerun()
                            with col_annulla:
                                if st.form_submit_button("Annulla"):
                                    st.session_state[f"edit_commessa_{comm_id}"] = False
                                    st.rerun()
                    else:
                        col_titolo, col_edit = st.columns([0.95, 0.05])
                        with col_titolo:
                            st.subheader(f"📁 {comm_row['numero_identificativo']} - {comm_row['nome']}")
                        with col_edit:
                            if st.button("✏️", key=f"edit_btn_{comm_id}"):
                                st.session_state[f"edit_commessa_{comm_id}"] = True
                                st.rerun()

                    anno_str = f"Anno: {int(comm_row['anno'])}" if pd.notna(comm_row['anno']) else "Anno: n/d"
                    st.caption(anno_str)
                    if comm_row['note']:
                        st.caption(f"Note: {comm_row['note']}")

                    sott_df = leggi_sottolavori_per_commessa(engine, comm_id)
                    if sott_df.empty:
                        sott_df = pd.DataFrame(columns=['id', 'nome', 'ingegnere_assegnato', 'stato', 'data_inizio', 'data_fine_prevista', 'note'])
                    else:
                        sott_df = sott_df.copy()
                    sott_df['id'] = sott_df['id'].astype(int)
                    editor_df = sott_df[['id', 'nome', 'ingegnere_assegnato', 'stato', 'data_fine_prevista', 'note']].copy()
                    editor_df.columns = ['id', 'Nome', 'Ingegnere', 'Stato', 'Scadenza', 'Note']
                    editor_df['Scadenza'] = editor_df['Scadenza'].astype(str)
                    stato_options = ["In corso", "Completato", "In attesa"]

                    st.markdown("**Sottolavori (modifica direttamente, aggiungi righe in fondo)**")
                    edited_df = st.data_editor(
                        editor_df,
                        num_rows="dynamic",
                        column_config={
                            "id": None,
                            "Stato": st.column_config.SelectboxColumn("Stato", options=stato_options, default="In corso"),
                            "Ingegnere": st.column_config.SelectboxColumn("Ingegnere", options=ingegneri_attivi, default=ingegneri_attivi[0] if ingegneri_attivi else None),
                            "Scadenza": st.column_config.TextColumn("Scadenza"),
                            "Note": st.column_config.TextColumn("Note"),
                        },
                        use_container_width=True,
                        hide_index=True,
                    )

                    if st.button("💾 Salva modifiche sottolavori"):
                        edited_df['id'] = pd.to_numeric(edited_df['id'], errors='coerce').fillna(0).astype(int)
                        original_ids = set(sott_df['id'].tolist()) if not sott_df.empty else set()
                        edited_ids = set()
                        for _, row in edited_df.iterrows():
                            rid = row['id']
                            if rid != 0:
                                edited_ids.add(rid)
                        to_delete = original_ids - edited_ids
                        for del_id in to_delete:
                            elimina_sottolavoro(engine, int(del_id))
                        for _, row in edited_df.iterrows():
                            rid = row['id']
                            nome = row['Nome'] if not pd.isna(row['Nome']) else None
                            ing = row['Ingegnere']
                            stato = row['Stato']
                            scadenza = row['Scadenza'] if pd.notna(row['Scadenza']) and row['Scadenza'] != '' else None
                            note = row['Note'] if pd.notna(row['Note']) else None
                            if rid == 0:
                                if nome:
                                    aggiungi_sottolavoro(engine, comm_id, nome, ing, datetime.today().strftime("%Y-%m-%d"), scadenza, note, stato)
                            else:
                                if nome:
                                    aggiorna_sottolavoro(engine, int(rid), nome=nome, ingegnere_assegnato=ing, stato=stato,
                                                        data_fine_prevista=scadenza, note=note,
                                                        data_fine_effettiva=datetime.today().strftime("%Y-%m-%d") if stato == "Completato" else None)
                        st.success("Sottolavori aggiornati.")
                        st.rerun()

        # ==================== TAB RIEPILOGO ====================
        with tab_riepilogo:
            st.subheader("📋 Riepilogo globale sottolavori")
            riepilogo_df = leggi_tutti_sottolavori(engine)
            if not riepilogo_df.empty:
                df_rip = riepilogo_df[['anno', 'commessa_nome', 'numero_identificativo', 'nome', 'ingegnere_assegnato', 'stato', 'note']].copy()
                df_rip.columns = ['Anno', 'Commessa', 'ID Commessa', 'Sottolavoro', 'Ingegnere', 'Stato', 'Note']
                df_rip['_gruppo'] = df_rip['ID Commessa'].astype(str) + " - " + df_rip['Commessa']
                df_rip = df_rip.sort_values(by=['Anno', '_gruppo'])
                df_display = df_rip.drop(columns=['_gruppo'])

                def color_stato(val):
                    colors = {
                        "Completato": "background-color: #d4edda; color: #155724",
                        "In corso": "background-color: #f8d7da; color: #721c24",
                        "In attesa": "background-color: #fff3cd; color: #856404"
                    }
                    return colors.get(val, "")

                styled_df = df_display.style.map(color_stato, subset=['Stato'])

                col_tabella, col_grafico = st.columns([2, 1])

                with col_tabella:
                    st.markdown("""
                    <style>
                    .dataframe-container table td { padding: 2px 4px !important; font-size: 0.8rem !important; }
                    .dataframe-container table th:nth-child(1), .dataframe-container table td:nth-child(1) { width: 30px !important; max-width: 30px !important; }
                    .dataframe-container table th:not(:last-child), .dataframe-container table td:not(:last-child) { max-width: 80px; width: 80px; }
                    .dataframe-container table th:last-child, .dataframe-container table td:last-child { min-width: 200px; }
                    </style>
                    """, unsafe_allow_html=True)
                    st.dataframe(styled_df, width='stretch', height=800, hide_index=True)

                with col_grafico:
                    st.subheader("📊 Stato lavori")
                    conteggi_stato = df_rip['Stato'].value_counts()
                    for stato in ["Completato", "In corso", "In attesa"]:
                        if stato not in conteggi_stato:
                            conteggi_stato[stato] = 0
                    conteggi_stato = conteggi_stato.reindex(["Completato", "In corso", "In attesa"])
                    fig1 = go.Figure(data=[go.Pie(labels=conteggi_stato.index, values=conteggi_stato.values, hole=0.3, marker=dict(colors=['#28a745', '#dc3545', '#ffc107']))])
                    fig1.update_layout(margin=dict(t=0, b=0, l=0, r=0), height=300, legend=dict(orientation="v", yanchor="middle", y=0.5, xanchor="left", x=1.05))
                    st.plotly_chart(fig1, use_container_width=True)

                    st.subheader("👷‍♂️ Lavori per ingegnere")
                    conteggi_ing = df_rip['Ingegnere'].value_counts()
                    fig2 = go.Figure(data=[go.Pie(labels=conteggi_ing.index, values=conteggi_ing.values, hole=0.3, marker=dict(colors=px.colors.qualitative.Set2))])
                    fig2.update_layout(margin=dict(t=0, b=0, l=0, r=0), height=300, legend=dict(orientation="v", yanchor="middle", y=0.5, xanchor="left", x=1.05))
                    st.plotly_chart(fig2, use_container_width=True)
            else:
                st.info("Nessun sottolavoro presente.")

        # ==================== TAB INGEGNERI ====================
        with tab_ingegneri:
            st.subheader("👷 Mansioni per ingegnere")
            utente_corrente = st.session_state['username']
            is_admin = st.session_state['is_admin']
            if is_admin:
                utenti_attivi = ottieni_utenti_attivi(engine)['username'].tolist()
                ingegnere_selezionato = st.selectbox("Seleziona ingegnere", utenti_attivi)
            else:
                ingegnere_selezionato = utente_corrente

            sott_df = pd.read_sql_query("""
                SELECT s.id, c.nome as commessa_nome, c.numero_identificativo,
                       s.nome as sottolavoro_nome, s.ingegnere_assegnato, s.stato, s.note
                FROM sottolavori s
                JOIN commesse c ON s.commessa_id = c.id
                WHERE s.ingegnere_assegnato = :ing
                ORDER BY c.nome, s.nome
            """, engine, params={"ing": ingegnere_selezionato})

            pers_df = leggi_attivita_personali(engine, ingegnere_selezionato)
            if not pers_df.empty:
                pers_df = pers_df.rename(columns={'descrizione': 'sottolavoro_nome'})
                pers_df['commessa_nome'] = ""
                pers_df['numero_identificativo'] = ""
            else:
                pers_df = pd.DataFrame(columns=['id', 'ingegnere', 'sottolavoro_nome', 'stato', 'note',
                                                'data_inizio', 'data_fine_prevista', 'data_fine_effettiva',
                                                'commessa_nome', 'numero_identificativo'])

            colonne = ['id', 'tipo', 'Commessa', 'ID Commessa', 'Mansione', 'Ingegnere', 'Stato', 'Note']
            righe = []
            for _, row in sott_df.iterrows():
                righe.append({
                    'id': row['id'], 'tipo': 'sottolavoro', 'Commessa': row['commessa_nome'],
                    'ID Commessa': row['numero_identificativo'], 'Mansione': row['sottolavoro_nome'],
                    'Ingegnere': row['ingegnere_assegnato'], 'Stato': row['stato'],
                    'Note': row['note'] if row['note'] else ""
                })
            for _, row in pers_df.iterrows():
                righe.append({
                    'id': row['id'], 'tipo': 'personale', 'Commessa': "", 'ID Commessa': "",
                    'Mansione': row['sottolavoro_nome'],
                    'Ingegnere': row['ingegnere'] if 'ingegnere' in row else ingegnere_selezionato,
                    'Stato': row['stato'], 'Note': row['note'] if row['note'] else ""
                })

            df_ing = pd.DataFrame(righe, columns=colonne) if righe else pd.DataFrame(columns=colonne)

            col_tabella, col_torta = st.columns([2, 1])
            with col_tabella:
                st.markdown(f"**Compiti di {ingegnere_selezionato}**")
                edited_ing = st.data_editor(
                    df_ing, num_rows="fixed",
                    column_config={
                        "id": None, "tipo": None,
                        "Commessa": st.column_config.TextColumn(disabled=True),
                        "ID Commessa": st.column_config.TextColumn(disabled=True),
                        "Mansione": st.column_config.TextColumn(disabled=True),
                        "Ingegnere": st.column_config.TextColumn(disabled=True),
                        "Stato": st.column_config.SelectboxColumn(options=["In corso", "Completato", "In attesa"], default="In corso"),
                        "Note": st.column_config.TextColumn()
                    },
                    use_container_width=True, hide_index=True, height=600
                )
                if st.button("💾 Salva modifiche ingegnere", key=f"save_ing_{ingegnere_selezionato}"):
                    for _, row in edited_ing.iterrows():
                        rid = row['id']
                        tipo = row['tipo']
                        nuovo_stato = row['Stato']
                        nuove_note = row['Note'] if row['Note'] else None
                        if tipo == 'sottolavoro':
                            aggiorna_sottolavoro(engine, int(rid), stato=nuovo_stato, note=nuove_note,
                                                 data_fine_effettiva=datetime.today().strftime("%Y-%m-%d") if nuovo_stato == "Completato" else None)
                        elif tipo == 'personale':
                            aggiorna_attivita_personale(engine, int(rid), stato=nuovo_stato, note=nuove_note,
                                                        data_fine_effettiva=datetime.today().strftime("%Y-%m-%d") if nuovo_stato == "Completato" else None)
                    st.success(f"Compiti di {ingegnere_selezionato} aggiornati.")
                    st.rerun()

            with col_torta:
                st.subheader("📊 Stato lavori")
                if not df_ing.empty:
                    conteggi = df_ing['Stato'].value_counts()
                    for stato in ["Completato", "In corso", "In attesa"]:
                        if stato not in conteggi:
                            conteggi[stato] = 0
                    conteggi = conteggi.reindex(["Completato", "In corso", "In attesa"])
                    fig = go.Figure(data=[go.Pie(labels=conteggi.index, values=conteggi.values, hole=0.3, marker=dict(colors=['#28a745', '#dc3545', '#ffc107']))])
                    fig.update_layout(margin=dict(t=0, b=0, l=0, r=0), height=300, legend=dict(orientation="v", yanchor="middle", y=0.5, xanchor="left", x=1.05))
                    st.plotly_chart(fig, use_container_width=True)
                else:
                    st.info("Nessun dato")

            with st.expander("➕ Nuova attività personale"):
                with st.form(key=f"nuova_att_pers_{ingegnere_selezionato}", clear_on_submit=True):
                    desc = st.text_input("Descrizione *")
                    col_data1, col_data2 = st.columns(2)
                    with col_data1:
                        data_in = st.date_input("Data inizio", datetime.today())
                    with col_data2:
                        data_fine = st.date_input("Scadenza", datetime.today() + timedelta(days=30))
                    note = st.text_area("Note")
                    stato = st.selectbox("Stato", ["In corso", "In attesa"])
                    if st.form_submit_button("Aggiungi attività"):
                        if desc:
                            aggiungi_attivita_personale(engine, ingegnere_selezionato, desc, data_in, data_fine, note, stato)
                            st.success("Attività personale aggiunta.")
                            st.rerun()
                        else:
                            st.error("La descrizione è obbligatoria.")

    # -------------------- ATTIVITÀ PERSONALI --------------------
    elif scelta == "Attività Personali":
        st.header("👤 Attività Personali")
        ing_corrente = st.session_state['username']
        with st.expander("➕ Nuova attività", expanded=False):
            with st.form("nuova_att_pers"):
                desc = st.text_input("Descrizione*")
                data_in = st.date_input("Data inizio", datetime.today())
                data_fine = st.date_input("Scadenza")
                note = st.text_area("Note")
                stato = st.selectbox("Stato", ["In corso", "In attesa"])
                if st.form_submit_button("Aggiungi"):
                    if desc:
                        aggiungi_attivita_personale(engine, ing_corrente, desc, data_in, data_fine, note, stato)
                        st.success("Aggiunta.")
                        st.rerun()
                    else:
                        st.error("Descrizione obbligatoria.")

        att_df = leggi_attivita_personali(engine, ing_corrente)
        if att_df.empty:
            st.info("Nessuna attività personale registrata.")
        else:
            for _, att in att_df.iterrows():
                with st.expander(f"📝 {att['descrizione']} – {att['stato']}"):
                    st.write(f"Inizio: {att['data_inizio']} | Scadenza: {att['data_fine_prevista']} | Fine: {att['data_fine_effettiva'] or '---'}")
                    nuovo_stato = st.selectbox("Stato", ["In corso", "Completato", "In attesa"],
                                               index=["In corso", "Completato", "In attesa"].index(att['stato']),
                                               key=f"stato_p_{att['id']}")
                    nuova_note = st.text_area("Note", att['note'] or "", key=f"note_p_{att['id']}")
                    colA, colB = st.columns(2)
                    if colA.button("Aggiorna", key=f"upd_p_{att['id']}"):
                        aggiorna_attivita_personale(engine, att['id'], stato=nuovo_stato, note=nuova_note)
                        st.rerun()
                    if colB.button("Elimina", key=f"del_p_{att['id']}"):
                        elimina_attivita_personale(engine, att['id'])
                        st.warning("Eliminata.")
                        st.rerun()

    # -------------------- RESOCONTO --------------------
    elif scelta == "Resoconto":
        st.header("📋 Cosa sta facendo ogni ingegnere")
        ingegneri = ottieni_utenti_attivi(engine)['username'].tolist()
        ing_sel = st.selectbox("Scegli ingegnere", ingegneri)
        st.markdown(f"### {ing_sel}")

        sott_in_corso = pd.read_sql_query("""
            SELECT s.*, c.nome as commessa_nome, c.numero_identificativo, a.anno
            FROM sottolavori s
            JOIN commesse c ON s.commessa_id = c.id
            LEFT JOIN anni a ON c.anno_id = a.id
            WHERE s.ingegnere_assegnato = :ing AND s.stato = 'In corso'
        """, engine, params={"ing": ing_sel})
        if not sott_in_corso.empty:
            st.markdown("**Sottolavori in corso:**")
            for _, r in sott_in_corso.iterrows():
                st.write(f"🔸 {r['nome']} (Commessa {r['numero_identificativo']} - {r['commessa_nome']}, anno {int(r['anno']) if pd.notna(r['anno']) else 'n/d'}) – scadenza {r['data_fine_prevista']}")

        pers_in_corso = pd.read_sql_query("SELECT * FROM attivita_personali WHERE ingegnere=:ing AND stato='In corso'", engine, params={"ing": ing_sel})
        if not pers_in_corso.empty:
            st.markdown("**Attività personali in corso:**")
            for _, r in pers_in_corso.iterrows():
                st.write(f"👤 {r['descrizione']} (scadenza {r['data_fine_prevista']})")

        if sott_in_corso.empty and pers_in_corso.empty:
            st.success("Nessun incarico in corso 🎉")

    # -------------------- STORICO --------------------
    elif scelta == "Storico":
        st.header("🕰️ Situazione a una data passata")
        data_scelta = st.date_input("Data di riferimento", datetime.today() - timedelta(weeks=4))
        data_limite = datetime.combine(data_scelta, datetime.max.time())
        comm, sott, att = tutte_entita_alla_data(engine, data_limite)
        tab1, tab2, tab3 = st.tabs(["Commesse", "Sottolavori", "Attività Personali"])
        with tab1:
            if comm:
                st.dataframe(pd.DataFrame(comm))
            else:
                st.info("Nessuna commessa.")
        with tab2:
            if sott:
                st.dataframe(pd.DataFrame(sott))
            else:
                st.info("Nessun sottolavoro.")
        with tab3:
            if att:
                st.dataframe(pd.DataFrame(att))
            else:
                st.info("Nessuna attività personale.")

    # -------------------- BACKUP --------------------
    elif scelta == "Backup":
        st.header("💾 Backup dati")
        if st.button("Scarica backup completo (Excel)"):
            excel_data = esporta_excel(engine)
            st.download_button(
                label="📥 Clicca qui per scaricare il file Excel",
                data=excel_data,
                file_name=f"backup_commesse_{datetime.today().strftime('%Y%m%d')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
        st.markdown("I dati sono salvati su PostgreSQL (Supabase).")

    # -------------------- AMMINISTRAZIONE UTENTI --------------------
    elif scelta == "Amministrazione Utenti" and st.session_state['is_admin']:
        st.header("👥 Gestione Ingegneri")
        utenti = ottieni_tutti_utenti(engine)
        st.dataframe(utenti[['username', 'nome', 'cognome', 'attivo', 'is_admin']], use_container_width=True)

        with st.expander("➕ Nuovo utente"):
            with st.form("nuovo_utente"):
                new_user = st.text_input("Username")
                new_pw = st.text_input("Password", type="password")
                new_nome = st.text_input("Nome")
                new_cogn = st.text_input("Cognome")
                admin_flag = st.checkbox("Amministratore")
                if st.form_submit_button("Crea"):
                    if new_user and new_pw and new_nome and new_cogn:
                        ok, msg = aggiungi_utente(engine, new_user, new_pw, new_nome, new_cogn, admin_flag)
                        st.success(msg) if ok else st.error(msg)
                        st.rerun()
                    else:
                        st.error("Tutti i campi obbligatori.")

        with st.expander("🔧 Attiva/Disattiva utente"):
            username_sel = st.selectbox("Utente", utenti['username'].tolist())
            attuale_stato = utenti[utenti['username'] == username_sel]['attivo'].values[0]
            nuovo_stato = st.checkbox("Attivo", value=bool(attuale_stato))
            if st.button("Aggiorna stato"):
                aggiorna_stato_utente(engine, username_sel, int(nuovo_stato))
                st.success(f"Stato di {username_sel} aggiornato.")
                st.rerun()

if __name__ == "__main__":
    main()
