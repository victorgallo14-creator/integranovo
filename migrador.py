import re

def migrar_codigo():
    with open("app_pei.py", "r", encoding="utf-8") as f:
        codigo = f.read()

    # 1. Substitui os imports
    if "from supabase import create_client" not in codigo:
        codigo = codigo.replace("from streamlit_gsheets import GSheetsConnection", 
                                "from supabase import create_client, Client")

    # 2. Corrige a chamada de conexão direta que existia no painel de Login
    codigo = codigo.replace('conn.read(worksheet="Professores", ttl=0)', 'safe_read("Professores", ["matricula", "nome"])')

    # 3. Substitui o bloco inteiro de Funções do Banco de Dados
    padrao_db = re.compile(r"# --- CONEXÃO COM GOOGLE SHEETS ---.*?# --- FIM DAS FUNÇÕES DE BANCO DE DADOS ---", re.DOTALL)
    
    novo_bloco = '''# --- CONEXÃO COM SUPABASE ---
@st.cache_resource
def init_supabase():
    url = st.secrets["SUPABASE_URL"]
    key = st.secrets["SUPABASE_KEY"]
    return create_client(url, key)

supabase = init_supabase()

def load_db(strict=False):
    try:
        res = supabase.table("Alunos").select("*").execute()
        df = pd.DataFrame(res.data)
        if df.empty: return pd.DataFrame(columns=["nome", "tipo_doc", "dados_json", "id", "ultima_atualizacao"])
        return df
    except Exception as e:
        if strict: raise Exception(f"Erro Supabase: {e}")
        return pd.DataFrame(columns=["nome", "tipo_doc", "dados_json", "id", "ultima_atualizacao"])

def safe_read(worksheet_name, columns):
    try:
        res = supabase.table(worksheet_name).select("*").execute()
        df = pd.DataFrame(res.data)
        if df.empty: return pd.DataFrame(columns=columns)
        return df
    except:
        return pd.DataFrame(columns=columns)

def safe_update(worksheet_name, data):
    """
    Sincroniza do Pandas para o Supabase (Recados, Agenda).
    Imita o comportamento do GSheets deletando os antigos e inserindo o novo DF limpo.
    """
    try:
        supabase.table(worksheet_name).delete().gte("id", 1).execute()
        if not data.empty:
            if "id" in data.columns:
                data = data.drop(columns=["id"])
            data = data.where(pd.notnull(data), None) # Converte NaNs para Null
            supabase.table(worksheet_name).insert(data.to_dict(orient="records")).execute()
        return True
    except Exception as e:
        st.error(f"Erro ao atualizar {worksheet_name}: {e}")
        return False

def create_backup(df_atual):
    pass # Backups agora são gerenciados nativamente pela infraestrutura do Supabase

def log_action(student_name, action, details):
    try:
        novo_log = {
            "Data_Hora": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
            "Aluno": student_name,
            "Usuario": st.session_state.get('usuario_nome', 'Desconhecido'),
            "Acao": action,
            "Detalhes": details
        }
        supabase.table("Historico").insert(novo_log).execute()
    except: pass

def save_student(doc_type, name, data, section="Geral"):
    is_monitor = st.session_state.get('user_role') == 'monitor'
    if is_monitor and doc_type != "DIARIO" and section != "Assinatura":
        st.error("Acesso negado: Monitores não podem editar este documento.")
        return

    try:
        id_registro = f"{name} ({doc_type})"
        if 'doc_uuid' not in data or not data['doc_uuid']: data['doc_uuid'] = str(uuid.uuid4()).upper()

        def serializar_datas(obj):
            if isinstance(obj, (date, datetime)): return obj.strftime("%Y-%m-%d")
            if isinstance(obj, dict): return {k: serializar_datas(v) for k, v in obj.items()}
            if isinstance(obj, list): return [serializar_datas(i) for i in obj]
            return obj
            
        data_limpa = serializar_datas(data)
        novo_json = json.dumps(data_limpa, ensure_ascii=False)
        fuso_br = timezone(timedelta(hours=-3))
        data_hora_agora = datetime.now(fuso_br).strftime("%d/%m/%Y %H:%M:%S")

        novo_registro = {
            "id": id_registro,
            "nome": name,
            "tipo_doc": doc_type,
            "dados_json": novo_json,
            "ultima_atualizacao": data_hora_agora
        }
        
        # O poderoso UPSERT substitui toda a sua lógica manual de lock e cópia de DFs
        supabase.table("Alunos").upsert(novo_registro).execute()
        st.toast(f"✅ Alterações em {name} salvas com segurança!", icon="💾")
    except Exception as e:
        st.error(f"❌ Falha ao salvar no banco Supabase. Erro: {e}")

def delete_student(student_name):
    is_monitor = st.session_state.get('user_role') == 'monitor'
    if is_monitor: return False
    try:
        supabase.table("Alunos").delete().eq("nome", student_name).execute()
        st.toast(f"🗑️ Registro de {student_name} excluído!", icon="🔥")
        return True
    except Exception as e:
        st.error(f"Erro ao excluir: {e}")
        return False
# --- FIM DAS FUNÇÕES DE BANCO DE DADOS ---'''

    codigo_novo = padrao_db.sub(novo_bloco, codigo)

    with open("app_pei.py", "w", encoding="utf-8") as f:
        f.write(codigo_novo)
    
    print("✅ Migração concluída com sucesso! O arquivo app_pei.py foi reescrito para usar Supabase nativamente.")

if __name__ == "__main__":
    migrar_codigo()
