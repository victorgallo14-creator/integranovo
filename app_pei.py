import streamlit as st
from datetime import datetime, date, timedelta, timezone
from fpdf import FPDF
import datetime as dt_module
import io
import os
import base64
import json
import tempfile
from PIL import Image
import pandas as pd
from streamlit_gsheets import GSheetsConnection
import time
import uuid
import threading
import random
import time
import zipfile
import io

MIN_DATA = date(1900, 1, 1)
MAX_DATA = date(2100, 12, 31)

# --- FUNÇÕES AUXILIARES DE DESENHO (GLOBAIS) ---
def calc_lines(pdf, text, w):
    if not text: return 1
    lines = 0
    for p in str(text).split('\n'):
        words = p.split(' ')
        line_w = 0
        for word in words:
            word_w = pdf.get_string_width(word + ' ')
            if line_w + word_w > w - 2:
                lines += 1
                line_w = word_w
            else:
                line_w += word_w
        lines += 1
    return max(1, lines)

def draw_flex_row(pdf, col_data, line_h=6, font_size=9, fill_color=(240, 240, 240)):
    max_lines = 1
    x_start_measure = pdf.get_x()
    for w, text, weight, align, fill in col_data:
        pdf.set_font("Arial", weight, font_size)
        real_w = w if w > 0 else (210 - 15 - x_start_measure)
        lines = calc_lines(pdf, text, real_w)
        if lines > max_lines: max_lines = lines
        x_start_measure += real_w
        
    row_h = max_lines * line_h
    if pdf.get_y() + row_h > 275:
        pdf.add_page()
        
    x_start = pdf.get_x()
    y_start = pdf.get_y()
    for w, text, weight, align, fill in col_data:
        real_w = w if w > 0 else (210 - 15 - x_start)
        pdf.set_font("Arial", weight, font_size)
        if fill: pdf.set_fill_color(*fill_color)
        else: pdf.set_fill_color(255, 255, 255)
        
        pdf.set_xy(x_start, y_start)
        pdf.cell(real_w, row_h, "", border=1, fill=fill)
        
        y_text = y_start + 1
        if max_lines > 1 and calc_lines(pdf, text, real_w) == 1:
            y_text = y_start + (row_h - line_h) / 2
            
        pdf.set_xy(x_start + 1, y_text)
        pdf.multi_cell(real_w - 2, line_h, str(text), border=0, align=align)
        x_start += real_w
        
    pdf.set_xy(15, y_start + row_h)

# --- CONEXÃO COM GOOGLE SHEETS ---
conn = st.connection("gsheets", type=GSheetsConnection)

# --- CONFIGURAÇÃO INICIAL ---
st.set_page_config(
    page_title="Integra | Sistema AEE",
    layout="wide",
    page_icon="🧠",
    initial_sidebar_state="auto"
)

# --- OCULTAR TOOLBAR E MENU E RESPONSIVIDADE ---
hide_st_style = """
            <style>
            #MainMenu {visibility: hidden;}
            footer {visibility: hidden;}
            .stAppDeployButton {display:none;}
            
            /* --- COMPORTAMENTO DESKTOP (Largura > 992px) --- */
            @media (min-width: 992px) {
                /* Esconde completamente o header */
                header {display: none !important;}
                [data-testid="stSidebarCollapseButton"] {display: none !important;}
                
                /* FORÇA A BARRA LATERAL A IR PARA O TOPO ABSOLUTO */
                section[data-testid="stSidebar"] {
                    top: 0px !important;
                    height: 100vh !important;
                }
            }
            
            /* --- COMPORTAMENTO MOBILE/TABLET (Largura <= 991px) --- */
            @media (max-width: 991px) {
                /* Header visível para acessar o menu hambúrguer */
                header {visibility: visible;}
                
                /* Ajustes para evitar que o conteúdo suba demais */
                .header-box {
                    margin-top: 0px !important;
                }
            }
            </style>
            """
st.markdown(hide_st_style, unsafe_allow_html=True)


# Cria uma trava global no servidor para evitar salvamentos simultâneos
@st.cache_resource
def get_db_lock():
    return threading.Lock()

db_lock = get_db_lock()

def load_db(strict=False):
    """
    Lê os dados da planilha do Google.
    """
    try:
        df = conn.read(worksheet="Alunos", ttl=0)
        if df.empty and strict:
            # Tenta ler outra aba leve apenas para testar conexão
            conn.read(worksheet="Professores", ttl=0)
        df = df.dropna(how="all")
        return df
    except Exception as e:
        if strict:
            raise Exception(f"Erro ao ler Google Sheets (API pode estar sobrecarregada): {e}")
        return pd.DataFrame(columns=["nome", "tipo_doc", "dados_json", "id"])

def safe_read(worksheet_name, columns):
    try:
        df = conn.read(worksheet=worksheet_name, ttl=0)
        if df.empty: return pd.DataFrame(columns=columns)
        return df.dropna(how="all")
    except:
        return pd.DataFrame(columns=columns)

def safe_update(worksheet_name, data):
    """Atualiza uma aba com fila de espera (Lock) e retentativas"""
    MAX_RETRIES = 3
    with db_lock: # Fila de espera
        for attempt in range(MAX_RETRIES):
            try:
                conn.update(worksheet=worksheet_name, data=data)
                return True
            except Exception as e:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(random.uniform(1, 3)) # Espera um tempo aleatório e tenta de novo
                else:
                    st.error(f"Erro ao atualizar {worksheet_name} após {MAX_RETRIES} tentativas: {e}")
                    return False

def create_backup(df_atual):
    if not df_atual.empty:
        try:
            conn.update(worksheet="Backup_Alunos", data=df_atual)
        except:
            pass # Falhas de backup não devem travar o sistema principal

def log_action(student_name, action, details):
    try:
        with db_lock:
            df_hist = safe_read("Historico", ["Data_Hora", "Aluno", "Usuario", "Acao", "Detalhes"])
            novo_log = {
                "Data_Hora": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
                "Aluno": student_name,
                "Usuario": st.session_state.get('usuario_nome', 'Desconhecido'),
                "Acao": action,
                "Detalhes": details
            }
            df_hist = pd.concat([df_hist, pd.DataFrame([novo_log])], ignore_index=True)
            conn.update(worksheet="Historico", data=df_hist)
    except:
        pass

def save_student(doc_type, name, data, section="Geral"):
    """Salva ou atualiza com LOCK e LEITURA DE ÚLTIMO MILISSEGUNDO"""
    is_monitor = st.session_state.get('user_role') == 'monitor'
    if is_monitor and doc_type != "DIARIO" and section != "Assinatura":
        st.error("Acesso negado: Monitores não podem editar este documento.")
        return

    MAX_RETRIES = 3

    # O WITH DB_LOCK garante que se 5 pessoas clicarem em salvar ao mesmo tempo,
    # o Streamlit fará uma fila e salvará um por um, evitando esmagamento de dados.
    with db_lock:
        for attempt in range(MAX_RETRIES):
            try:
                # 1. LÊ TUDO FRESQUINHO AGORA. Não confia nos dados velhos da tela.
                df_atual = load_db(strict=True)
                
                # 2. BACKUP (Apenas na primeira tentativa)
                if attempt == 0: 
                    create_backup(df_atual)

                id_registro = f"{name} ({doc_type})"
                
                if 'doc_uuid' not in data or not data['doc_uuid']:
                    data['doc_uuid'] = str(uuid.uuid4()).upper()

                def serializar_datas(obj):
                    if isinstance(obj, (date, datetime)): return obj.strftime("%Y-%m-%d")
                    if isinstance(obj, dict): return {k: serializar_datas(v) for k, v in obj.items()}
                    if isinstance(obj, list): return [serializar_datas(i) for i in obj]
                    return obj
                    
                data_limpa = serializar_datas(data)
                novo_json = json.dumps(data_limpa, ensure_ascii=False)

                df_final = df_atual.copy()
                
                if not df_atual.empty and "id" in df_atual.columns and id_registro in df_atual["id"].values:
                    df_final.loc[df_final["id"] == id_registro, "dados_json"] = novo_json
                else:
                    novo_registro = {
                        "id": id_registro,
                        "nome": name,
                        "tipo_doc": doc_type,
                        "dados_json": novo_json
                    }
                    if df_final.empty:
                        df_final = pd.DataFrame([novo_registro])
                    else:
                        df_final = pd.concat([df_final, pd.DataFrame([novo_registro])], ignore_index=True)

                # 3. TRAVA ANTI-WIPE (Se vier vazio da API por erro, ele barra aqui)
                qtd_antes = len(df_atual)
                qtd_depois = len(df_final)

                if qtd_antes > 5 and qtd_depois < (qtd_antes * 0.9): 
                    st.error(f"⛔ BLOQUEIO DE SEGURANÇA: Prevenção de perda de dados. Salvamento cancelado.")
                    return

                # 4. SALVA
                conn.update(worksheet="Alunos", data=df_final)
                
                # O log agora roda fora do seu próprio lock (já estamos no lock)
                st.toast(f"✅ Alterações em {name} salvas com segurança!", icon="💾")
                return # Sai do loop se deu certo
                
            except Exception as e:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(random.uniform(1.5, 3.5)) # Espera e tenta de novo se o Google bloquear
                else:
                    st.error(f"❌ Falha ao salvar no banco (API do Google sobrecarregada). Tente novamente em 10 segundos. Erro: {e}")

def delete_student(student_name):
    is_monitor = st.session_state.get('user_role') == 'monitor'
    if is_monitor: return False
        
    with db_lock:
        try:
            df = load_db(strict=True)
            create_backup(df)

            if "nome" in df.columns:
                df_new = df[df["nome"] != student_name]
                qtd_antes = len(df)
                qtd_depois = len(df_new)

                if qtd_antes > 0 and qtd_depois == 0 and qtd_antes > 5: 
                    st.error("⛔ BLOQUEIO: Tentativa de exclusão em massa cancelada.")
                    return False

                if qtd_depois < qtd_antes:
                    conn.update(worksheet="Alunos", data=df_new)
                    st.toast(f"🗑️ Registro de {student_name} excluído!", icon="🔥")
                    return True
        except Exception as e:
            st.error(f"Erro ao excluir: {e}")
    return False

# --- FIM DAS FUNÇÕES DE BANCO DE DADOS ---








# --- HELPERS PARA PDF ---
def clean_pdf_text(text):
    if text is None or text is False: return ""
    if text is True: return "Sim"
    return str(text).encode('latin-1', 'replace').decode('latin-1')

def get_pdf_bytes(pdf_instance):
    try: return bytes(pdf_instance.output(dest='S').encode('latin-1'))
    except: return bytes(pdf_instance.output(dest='S'))

# --- CLASSE PDF CUSTOMIZADA COM ASSINATURA ---
class OfficialPDF(FPDF):
    def __init__(self, orientation='P', unit='mm', format='A4'):
        super().__init__(orientation, unit, format)
        self.signature_info = None # Texto da assinatura
        self.doc_uuid = None
        self.doc_type = None

    def header(self):
        # TIMBRADO DE FUNDO EXCLUSIVO PARA A ATA
        if self.doc_type == "Ata":
            try:
                # NOME DO FICHEIRO CORRIGIDO PARA "image.png"
                self.image("image.png", x=0, y=0, w=210, h=297)
            except Exception as e:
                pass # Se a imagem não for encontrada, gera sem quebrar o sistema

    def set_signature_footer(self, signatures_list, doc_uuid):
        """Prepara o texto de validação para o rodapé"""
        self.doc_uuid = doc_uuid
        if signatures_list and len(signatures_list) > 0:
            names = [s.get('name', '').upper() for s in signatures_list]
            names_str = ", ".join(names[:-1]) + " e " + names[-1] if len(names) > 1 else names[0]
            self.signature_info = f"Assinado por {len(names)} pessoas: {names_str}"
        else:
            self.signature_info = "Documento gerado sem assinaturas digitais."
            
    def footer(self):
        # O rodapé padrão só aparece se NÃO for a Ata
        if self.doc_type != "Ata":
            self.set_y(-25)
            self.set_font('Arial', '', 8)
            self.set_text_color(80, 80, 80)
            
            # Bloco de Assinatura Digital
            if self.doc_uuid:
                # Posicionamento dinâmico baseado na altura da página
                box_h = 9  # Altura reduzida para ~2 linhas
                margin_bottom = 22 # Distância da borda inferior
                
                y_box = self.h - margin_bottom 
                x_box = 10
                w_box = self.w - 20 # Largura total (menos margens laterais de 10mm)

                # Caixa cinza claro para validação
                self.set_fill_color(245, 245, 245)
                self.rect(x_box, y_box, w_box, box_h, 'F')
                
                # Texto
                self.set_xy(x_box + 2, y_box + 1.5)
                self.set_font('Arial', 'B', 7)
                if self.signature_info:
                    self.cell(0, 3, clean_pdf_text(self.signature_info), 0, 1, 'L')
                else:
                    self.ln(3) # Espaço caso não tenha texto de assinatura
                
                self.set_x(x_box + 2)
                self.set_font('Arial', '', 7)
                link_txt = f"Para verificar a validade das assinaturas, acesse https://integra.streamlit.app e informe o código {self.doc_uuid}"
                self.cell(0, 3, clean_pdf_text(link_txt), 0, 1, 'L')

            # Endereço Padrão (Abaixo da caixa)
            self.set_y(-10)
            self.set_font('Arial', '', 8)
            addr = "Secretaria Municipal de Educação | Centro de Formação do Professor - Limeira-SP"
            self.cell(0, 5, clean_pdf_text(addr), 0, 0, 'C')
            self.set_font('Arial', 'I', 8)
            self.cell(0, 5, clean_pdf_text(f'Página {self.page_no()}'), 0, 0, 'R')

    def section_title(self, title, width=0):
        self.set_font('Arial', 'B', 12); self.set_fill_color(240, 240, 240)
        self.cell(width, 8, clean_pdf_text(title), 1, 1, 'L', 1)

# --- FUNÇÃO DE LOGIN COMPLETA E ROBUSTA (SME LIMEIRA) ---
def login():
    # Inicializa o estado de autenticação se não existir
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False
    
    if "user_role" not in st.session_state:
        st.session_state.user_role = None

    if not st.session_state.authenticated:
        # --- CSS DA TELA DE LOGIN (NO-SCROLL LAYOUT) ---
        st.markdown("""
            <style>
                /* Remove padding padrão do Streamlit para ocupar a tela toda */
                .block-container {
                    padding-top: 0rem !important;
                    padding-bottom: 0rem !important;
                    max-width: 100%;
                    min-height: 100vh;
                    display: flex;
                    flex-direction: column;
                    justify-content: center;
                }
                
                /* Fundo da Página */
                [data-testid="stAppViewContainer"] {
                    background: linear-gradient(135deg, #f0f4f8 0%, #d9e2ec 100%);
                }
                
                /* Painel Esquerdo (Arte) */
                .login-art-box {
                    background: linear-gradient(135deg, #2563eb 0%, #1e3a8a 100%);
                    min-height: 600px; /* Altura ajustada */
                    border-radius: 16px 0 0 16px; /* Arredondado apenas na esquerda */
                    display: flex;
                    flex-direction: column;
                    justify-content: center;
                    align-items: center;
                    color: white;
                    padding: 40px;
                    text-align: center;
                    box-shadow: -5px 10px 25px rgba(37, 99, 235, 0.2);
                }
                
                /* Painel Direito (Formulário) - Target the specific column wrapper (3rd column) */
                div[data-testid="column"]:nth-of-type(3),
                div[data-testid="stColumn"]:nth-of-type(3) {
                    background-color: white;
                    padding: 2rem 3rem !important;
                    border-radius: 0 16px 16px 0; /* Arredondado apenas na direita */
                    min-height: 600px; /* Mesma altura da arte */
                    display: flex;
                    flex-direction: column;
                    justify-content: flex-start; /* Alinhado ao topo para abas */
                    box-shadow: 5px 10px 25px rgba(0,0,0,0.05);
                }

                /* Tipografia */
                .welcome-title {
                    font-size: 1.8rem;
                    font-weight: 700;
                    color: #1e293b;
                    margin-bottom: 5px;
                }
                .welcome-sub {
                    font-size: 0.95rem;
                    color: #64748b;
                    margin-bottom: 20px;
                }
                
                /* Inputs Customizados */
                .stTextInput label {
                    font-size: 0.85rem;
                    color: #475569;
                    font-weight: 600;
                }
                
                /* Aviso LGPD */
                .lgpd-box {
                    background-color: #fff7ed;
                    border-left: 4px solid #f97316;
                    padding: 10px;
                    margin-top: 15px;
                    margin-bottom: 15px;
                    border-radius: 6px;
                }
                .lgpd-title {
                    color: #9a3412;
                    font-weight: 700;
                    font-size: 0.75rem;
                    display: flex; 
                    align-items: center; 
                    gap: 6px;
                }
                .lgpd-text {
                    color: #9a3412;
                    font-size: 0.7rem;
                    margin-top: 2px;
                    line-height: 1.2;
                    text-align: justify; /* Texto justificado */
                }
            </style>
        """, unsafe_allow_html=True)
        
        # Espaçamento para centralizar verticalmente na tela
        st.write("")
        st.write("")

        # Layout em Colunas: Spacer, Arte, Form, Spacer
        # Ajuste de proporção para ficar elegante
        c_pad1, c_art, c_form, c_pad2 = st.columns([1, 4, 4, 1])
        
        # --- LADO ESQUERDO (ARTE AZUL) ---
        with c_art:
            # Atenção: HTML sem indentação para evitar renderização de bloco de código
            st.markdown("""
<div class="login-art-box">
    <div style="font-size: 6rem; margin-bottom: 1rem; filter: drop-shadow(0px 4px 6px rgba(0,0,0,0.2));">🧠</div>
    <h1 style="color: white; font-weight: 800; font-size: 3.5rem; margin: 0; line-height: 1;">INTEGRA</h1>
    <p style="font-size: 1.2rem; opacity: 0.9; font-weight: 300; margin-top: 10px;">Gestão de Educação<br>Especial Inclusiva</p>
    <div style="margin-top: 40px; width: 100%;">
        <hr style="border-color: rgba(255,255,255,0.3); margin-bottom: 20px;">
        <p style="font-style: italic; font-size: 1rem; opacity: 0.9;">
            "A inclusão acontece quando se aprende com as diferenças e não com as igualdades."
        </p>
    </div>
</div>
""", unsafe_allow_html=True)
            
        # --- LADO DIREITO (FORMULÁRIO BRANCO) ---
        with c_form:
            # Abas de Login e Validação
            tab_login, tab_validar = st.tabs(["🔐 Acesso ao Sistema", "✅ Validar Documento"])
            
            with tab_login:
                with st.form("login_form"):
                    # Layout Header: Texto à esquerda, Logo à direita (menor)
                    c_head_txt, c_head_logo = st.columns([3, 1.2])
                    
                    with c_head_txt:
                        st.markdown('<div class="welcome-title" style="margin-top: 0px;">Bem-vindo(a)</div>', unsafe_allow_html=True)
                        st.markdown('<div class="welcome-sub">Insira suas credenciais para acessar o sistema.</div>', unsafe_allow_html=True)
                    
                    with c_head_logo:
                        if os.path.exists("logo_escola.png"):
                            st.image("logo_escola.png", use_container_width=True)
                    
                    st.write("") # Espaço
                    
                    user_id = st.text_input("Matrícula Funcional", placeholder="Ex: 12345")
                    password = st.text_input("Senha", type="password", placeholder="••••••")
                    
                    st.markdown("""
                        <div class="lgpd-box">
                            <div class="lgpd-title">🔒 CONFIDENCIALIDADE E SIGILO</div>
                            <div class="lgpd-text">
                                Acesso Monitorado. Protegido pela LGPD. Uso estritamente profissional.
                            </div>
                        </div>
                    """, unsafe_allow_html=True)
                    
                    submit = st.form_submit_button("ACESSAR SISTEMA", type="primary")
                    
                    if submit:
                        try:
                            SENHA_MESTRA = st.secrets.get("credentials", {}).get("password", "admin")
                            user_id_limpo = str(user_id).strip()
                            df_professores = conn.read(worksheet="Professores", ttl=0)
                            authenticated_as_prof = False
                            
                            if not df_professores.empty:
                                df_professores['matricula'] = df_professores['matricula'].astype(str).str.replace(r'\.0$', '', regex=True).str.strip()
                                if password == SENHA_MESTRA and user_id_limpo in df_professores['matricula'].values:
                                    registro = df_professores[df_professores['matricula'] == user_id_limpo]
                                    nome_prof = registro['nome'].values[0]
                                    st.session_state.authenticated = True
                                    st.session_state.usuario_nome = nome_prof
                                    st.session_state.user_role = 'professor'
                                    authenticated_as_prof = True
                                    st.toast(f"Acesso Docente autorizado. Bem-vindo(a), {nome_prof}!", icon="🔓")
                                    time.sleep(1); st.rerun()

                            if not authenticated_as_prof:
                                df_monitores = safe_read("Monitores", ["matricula", "nome"])
                                if not df_monitores.empty:
                                    df_monitores['matricula'] = df_monitores['matricula'].astype(str).str.replace(r'\.0$', '', regex=True).str.strip()
                                    if password == "123" and user_id_limpo in df_monitores['matricula'].values:
                                        registro = df_monitores[df_monitores['matricula'] == user_id_limpo]
                                        nome_mon = registro['nome'].values[0]
                                        st.session_state.authenticated = True
                                        st.session_state.usuario_nome = nome_mon
                                        st.session_state.user_role = 'monitor'
                                        st.toast(f"Acesso Monitor autorizado. Bem-vindo(a), {nome_mon}!", icon="🛡️")
                                        time.sleep(1); st.rerun()
                                    else:
                                        st.error("Credenciais inválidas.")
                                else:
                                    st.error("Credenciais inválidas.")
                        except Exception as e:
                            st.error(f"Erro técnico: {e}")

            with tab_validar:
                st.markdown("### Validação Pública")
                st.caption("Insira o código UUID presente no rodapé do documento para verificar sua autenticidade e assinaturas.")
                uuid_input = st.text_input("Código do Documento (UUID)", placeholder="Ex: 7D2B-5135...")
                if st.button("Verificar Autenticidade", type="primary"):
                    if uuid_input:
                        try:
                            df_alunos = load_db()
                            encontrado = False
                            for _, row in df_alunos.iterrows():
                                try:
                                    d = json.loads(row['dados_json'])
                                    if d.get('doc_uuid') == uuid_input.strip():
                                        encontrado = True
                                        st.success("✅ DOCUMENTO VÁLIDO E AUTÊNTICO")
                                        st.markdown(f"**Aluno:** {d.get('nome', 'N/A')}")
                                        st.markdown(f"**Tipo:** {row['tipo_doc']}")
                                        
                                        assinaturas = d.get('signatures', [])
                                        if assinaturas:
                                            st.markdown("---")
                                            st.markdown("### Assinaturas Digitais:")
                                            for sig in assinaturas:
                                                st.info(f"✍️ **{sig['name']}** ({sig.get('role', 'Profissional')})\n\n📅 Assinado em: {sig['date']}")
                                        else:
                                            st.warning("Este documento ainda não possui assinaturas digitais registradas.")
                                        break
                                except: pass
                            if not encontrado:
                                st.error("❌ Documento não encontrado ou código inválido.")
                        except Exception as e:
                            st.error(f"Erro na busca: {e}")
        
        # Interrompe o carregamento do restante do app até que o login seja feito
        st.stop()


# --- INICIALIZAÇÃO DO CONTROLE DE MÓDULO (PORTAL) ---
if "modulo_atuacao" not in st.session_state:
    st.session_state.modulo_atuacao = None


# --- ATIVAÇÃO DO LOGIN ---
login()


# ==============================================================================
# TELA DE PORTAL (ESCOLHA DO MÓDULO APÓS LOGIN)
# ==============================================================================
if st.session_state.authenticated:
    if st.session_state.modulo_atuacao is None:
        # Removido os <br> para o conteúdo subir e ficar mais centralizado
        st.write("") 
        
        # Título alterado para <div> para não aparecer o ícone de link
        st.markdown("<div style='text-align: center; color: #1e293b; font-size: 32px; font-weight: bold; margin-bottom: 40px;'>Seja bem-vindo(a)! Escolha seu ambiente de trabalho:</div>", unsafe_allow_html=True)
        
        col1, col2, col3, col4 = st.columns([1, 2, 2, 1])
        
        # --- BOTÃO 1: ENSINO REGULAR (ESQUERDA) ---
        with col2:
            st.markdown("""
            <div style='background-color: #eff6ff; padding: 35px 20px; border-radius: 12px; text-align: center; border: 2px solid #3b82f6; box-shadow: 0 4px 6px rgba(0,0,0,0.05);'>
                <div style='font-size: 55px; margin-bottom: 15px;'>🏫</div>
                <div style='color: #1d4ed8; font-size: 24px; font-weight: 700; margin: 0;'>Ensino Regular</div>
            </div>
            """, unsafe_allow_html=True)
            st.write("")
            if st.button("Acessar Ensino Regular", type="primary", use_container_width=True, key="btn_er"):
                st.session_state.modulo_atuacao = "🏫 Ensino Regular"
                st.rerun()

        # --- BOTÃO 2: EDUCAÇÃO ESPECIAL (DIREITA) ---
        with col3:
            st.markdown("""
            <div style='background-color: #f0fdf4; padding: 35px 20px; border-radius: 12px; text-align: center; border: 2px solid #22c55e; box-shadow: 0 4px 6px rgba(0,0,0,0.05);'>
                <div style='font-size: 55px; margin-bottom: 15px;'>🧠</div>
                <div style='color: #15803d; font-size: 24px; font-weight: 700; margin: 0;'>Educação Especial Inclusiva</div>
            </div>
            """, unsafe_allow_html=True)
            st.write("")
            if st.button("Acessar Educação Especial", type="primary", use_container_width=True, key="btn_ee"):
                st.session_state.modulo_atuacao = "🧠 Educação Especial Inclusiva"
                st.rerun()
                
        # st.stop() bloqueia o carregamento da sidebar até a pessoa clicar num botão
        st.stop()


# --- DEFINIÇÃO DE PERMISSÕES ---
user_role = st.session_state.get('user_role', 'professor')
is_monitor = (user_role == 'monitor') # Flag para bloquear edições

# --- ESTILO VISUAL DA INTERFACE (CSS MELHORADO E RESPONSIVO) ---
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;700&display=swap');
    
    html, body, [class*="css"] { font-family: 'Inter', sans-serif; }
    .stApp { background-color: #f8fafc; }
    
    /* --- CORREÇÃO: PUXA O CONTEÚDO PARA CIMA --- */
    .block-container {
        padding-top: 1.5rem !important; /* Reduz o espaço em branco no topo */
    }
    
    /* Melhoria da Sidebar */
    [data-testid="stSidebar"] [data-testid="stImage"] {
        display: flex;
        justify-content: center;
        margin-left: auto;
        margin-right: auto;
    }
    
    /* Centralizar o container de texto da sidebar */
    .sidebar-header {
        display: flex;
        flex-direction: column;
        align-items: center;
        text-align: center;
        width: 100%;
        padding-bottom: 20px;
    }
    
    .sidebar-title {
        color: #1e3a8a; /* Azul Institucional */
        font-weight: 800;
        font-size: 1.4rem;
        margin-top: 10px;
        line-height: 1.2;
    }
    .sidebar-subtitle {
        color: #64748b;
        font-size: 0.85rem;
        font-weight: 400;
    }

    /* Estilo dos Cards Principais */
    .header-box {
        background: white; padding: 2rem; border-radius: 12px;
        border-left: 6px solid #2563eb; /* Borda lateral azul */
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05);
        margin-bottom: 2rem;
        margin-top: 0px; 
    }
    
    .header-title { color: #1e293b; font-weight: 700; font-size: 1.8rem; margin: 0; }
    .header-subtitle { color: #64748b; font-size: 1rem; margin-top: 5px; }
    
/* Dashboard Cards */
    .metric-card {
        background-color: white;
        padding: 1rem 0.2rem;
        border-radius: 10px;
        box-shadow: 0 2px 4px rgba(0,0,0,0.05);
        border: 1px solid #e2e8f0;
        text-align: center;
        white-space: nowrap; 
        overflow: hidden; 
        text-overflow: ellipsis; 
    }
    .metric-value {
        font-size: 1.8rem; 
        font-weight: 700;
        line-height: 1.2;
    }
    .metric-label {
        color: #64748b;
        font-size: 0.72rem; 
        font-weight: 600;
        text-transform: uppercase;
        margin-top: 5px;
    }
    
    /* Botões */
    .stButton button { width: 100%; border-radius: 8px; }
    
    /* --- MEDIA QUERIES PARA MOBILE --- */
    @media (max-width: 991px) {
        .header-box {
            margin-top: 10px !important; 
            padding: 1.5rem !important;
        }
        .header-title {
            font-size: 1.5rem !important;
        }
        
        .stBlock {
            padding-top: 1rem;
        }
    }
</style>
""", unsafe_allow_html=True)

# --- INICIALIZAÇÃO DE ESTADO ---
if 'data_declaracao' not in st.session_state:
    st.session_state.data_declaracao = {}
# --- NOVAS VARIÁVEIS PARA O PORTÃO DE ENTRADA ---
if 'ee_aluno_confirmado' not in st.session_state:
    st.session_state.ee_aluno_confirmado = None
if 'ee_doc_confirmado' not in st.session_state:
    st.session_state.ee_doc_confirmado = None
if 'data_pei' not in st.session_state: 
    st.session_state.data_pei = {
        'terapias': {}, 'avaliacao': {}, 'flex': {}, 'plano_ensino': {},
        'comunicacao_tipo': [], 'permanece': []
    }
if 'data_conduta' not in st.session_state:
    st.session_state.data_conduta = {}
if 'data_avaliacao' not in st.session_state:
    st.session_state.data_avaliacao = {}
if 'data_diario' not in st.session_state:
    st.session_state.data_diario = {}
if 'data_pdi' not in st.session_state:
    st.session_state.data_pdi = {
        'metas': [{'objetivo': '', 'prazo': '', 'estrategia': '', 'status': 'Pendente'} for _ in range(5)],
        'pdi_fortalezas': '',
        'pdi_desafios': '',
        'pdi_recursos': '',
        'pdi_periodo': 'Trimestral',
        'pdi_obs': ''
    }
if 'data_declaracao' not in st.session_state:
    st.session_state.data_declaracao = {}

def carregar_dados_aluno():
    selecao = st.session_state.get('aluno_selecionado')
    
    # Init empty
    st.session_state.data_pei = {'terapias': {}, 'avaliacao': {}, 'flex': {}, 'plano_ensino': {}, 'comunicacao_tipo': [], 'permanece': []}
    st.session_state.data_case = {'irmaos': [{'nome': '', 'idade': '', 'esc': ''} for _ in range(4)], 'checklist': {}, 'clinicas': []}
    st.session_state.data_conduta = {}
    st.session_state.data_avaliacao = {}
    st.session_state.data_diario = {}
    st.session_state.data_pdi = {
        'metas': [{'objetivo': '', 'prazo': '', 'estrategia': '', 'status': 'Pendente'} for _ in range(5)],
        'pdi_fortalezas': '', 'pdi_desafios': '', 'pdi_recursos': '', 'pdi_periodo': 'Trimestral', 'pdi_obs': ''
    }
    st.session_state.nome_original_salvamento = None

    if not selecao or selecao == "-- Novo Registro --":
        return

    try:
        df_db = load_db()
        # Filter by name
        if "nome" in df_db.columns:
            rows = df_db[df_db["nome"] == selecao]
            if rows.empty: return
            
            st.session_state.nome_original_salvamento = selecao
            st.session_state.data_pei['nome'] = selecao
            st.session_state.data_case['nome'] = selecao
            st.session_state.data_conduta['nome'] = selecao
            st.session_state.data_avaliacao['nome'] = selecao
            st.session_state.data_diario['nome'] = selecao
            st.session_state.data_pdi['nome'] = selecao

            for _, row in rows.iterrows():
                try:
                    dados = json.loads(row["dados_json"])
                    # Date conversion
                    for k, v in dados.items():
                        if isinstance(v, str) and len(v) == 10 and v.count('-') == 2:
                            try: dados[k] = datetime.strptime(v, '%Y-%m-%d').date()
                            except: pass
                    
                    dtype = row["tipo_doc"]
                    if dtype == "PEI":
                        st.session_state.data_pei.update(dados)
                    elif dtype == "CASO":
                        st.session_state.data_case.update(dados)
                    elif dtype == "CONDUTA":
                        st.session_state.data_conduta.update(dados)
                    elif dtype == "AVALIACAO":
                        st.session_state.data_avaliacao.update(dados)
                    elif dtype == "DIARIO":
                        st.session_state.data_diario.update(dados)
                    elif dtype == "PDI":
                        st.session_state.data_pdi.update(dados)
                except: pass
            
            st.toast(f"✅ {selecao} carregado.")
            
    except Exception as e:
        st.info("Pronto para novo preenchimento.")

# --- BARRA LATERAL ULTRA-COMPACTA ---
with st.sidebar:
    # CSS PARA "ESPREMER" O LAYOUT
    st.markdown("""
    <style>
        section[data-testid="stSidebar"] > div {
            padding-top: 1rem !important;
            padding-bottom: 2rem !important;
        }
        [data-testid="stSidebar"] [data-testid="stVerticalBlock"] {
            gap: 1.2rem !important;
        }
        .sidebar-title {
            font-size: 1.1rem;
            font-weight: 800;
            color: #1e3a8a;
            margin: 0;
            margin-top: 0px !important;
            text-align: center;
            line-height: 1.2;
        }
        .sidebar-sub {
            font-size: 0.7rem;
            color: #64748b;
            text-align: center;
            margin-bottom: 8px;
        }
        .section-label {
            font-size: 0.8rem;
            font-weight: 700;
            color: #475569;
            margin-top: 8px;
            margin-bottom: 0px;
        }
        .user-slim {
            background-color: #f8fafc;
            border: 1px solid #e2e8f0;
            border-radius: 4px;
            padding: 4px;
            font-size: 0.8rem;
            color: #334155;
            text-align: center;
        }
        .role-tag {
            background-color: #e0f2fe;
            color: #0369a1;
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 0.7rem;
            font-weight: 600;
            margin-top: 2px;
            display: inline-block;
        }
        .stRadio { margin-top: -5px; }
        div[data-baseweb="select"] { min-height: 32px; }
        hr { margin: 0.5em 0 !important; }
    </style>
    """, unsafe_allow_html=True)

    # 1. TÍTULO
    st.markdown('<div class="sidebar-title">SISTEMA INTEGRA</div>', unsafe_allow_html=True)
    st.markdown('<div class="sidebar-sub">Gestão Escolar</div>', unsafe_allow_html=True)

    # 2. USUÁRIO
    nome_prof = st.session_state.get('usuario_nome', 'Usuário')
    role_label = "Monitor(a)" if is_monitor else "Docente/Admin"
    nomes = nome_prof.split()
    nome_curto = f"{nomes[0]} {nomes[-1]}" if len(nomes) > 1 else nomes[0]
    
    st.markdown(f"""
        <div style="text-align: center;">
            <div class="user-slim">👤 <b>{nome_curto}</b></div>
            <span class="role-tag">{role_label}</span>
        </div>
    """, unsafe_allow_html=True)
    
    st.divider()
    
    # 3. MÓDULO DE ATUAÇÃO
    modulo_atuacao = st.session_state.modulo_atuacao
    
    # Exibe em qual módulo estamos e cria um botão para voltar ao Portal
    st.markdown(f'<p class="section-label" style="color:#2563eb; font-weight:bold; text-align:center;">{modulo_atuacao}</p>', unsafe_allow_html=True)
    if st.button("🔄 Trocar Ambiente", use_container_width=True):
        st.session_state.modulo_atuacao = None
        st.rerun()

    st.divider()

    # Variáveis padrão de controle do sistema
    selected_student = "-- Novo Registro --"
    pei_level = "Fundamental" 
    doc_mode = "Dashboard"

    # --- NAVEGAÇÃO CONDICIONAL BASEADA NO MÓDULO ---
    if modulo_atuacao == "🧠 Educação Especial Inclusiva":
        st.markdown('<p class="section-label">📌 Navegação</p>', unsafe_allow_html=True)
        app_mode = st.radio("Navegação", ["📊 Painel de Gestão", "👥 Gestão de Alunos"], label_visibility="collapsed")

    elif modulo_atuacao == "🏫 Ensino Regular":
        app_mode = "Atas_Conselho" # Trava o sistema antigo em background
        
        st.markdown('<p class="section-label">📌 Documentos</p>', unsafe_allow_html=True)
        app_mode_regular = st.radio("Documentos", ["📝 Nova Ata de Conselho", "📂 Histórico de Atas", "💻 Agendamento Informática", "⚙️ Configurações"], label_visibility="collapsed")
        
        st.markdown('<p class="section-label">🏫 Modalidade</p>', unsafe_allow_html=True)
        modalidade_ata = st.selectbox("Nível", ["Ensino Fundamental", "Educação Infantil", "EJA"], label_visibility="collapsed")


# --- SEÇÃO GESTÃO DE ALUNOS ---
    # --- SEÇÃO GESTÃO DE ALUNOS (SIDEBAR LIMPA) ---
    if app_mode == "👥 Gestão de Alunos":
        st.divider()
        st.info("👉 Selecione o aluno e o documento na tela principal ao lado.")
        
        # Puxamos as variáveis confirmadas para a sidebar saber o que exibir de foto
        selected_student = st.session_state.get('ee_aluno_confirmado', "-- Novo Registro --")
        doc_mode = st.session_state.get('ee_doc_confirmado', "Dashboard")

        # Foto na Sidebar (só renderiza se o aluno estiver confirmado)
        current_photo_sb = None
        if selected_student and selected_student != "-- Novo Registro --":
            if st.session_state.get('data_pei', {}).get('nome') == selected_student:
                 current_photo_sb = st.session_state.data_pei.get('foto_base64')
            elif st.session_state.get('data_case', {}).get('nome') == selected_student:
                 current_photo_sb = st.session_state.data_case.get('foto_base64')
                 
        if current_photo_sb:
            try:
                img_bytes_sb = base64.b64decode(current_photo_sb)
                st.image(img_bytes_sb, use_container_width=True)
            except: pass
        
        st.markdown('<div style="flex-grow: 1;"></div>', unsafe_allow_html=True)

    # 4. RODAPÉ FIXO
    if st.sidebar.button("🚪 Sair", use_container_width=True):
        for key in list(st.session_state.keys()): del st.session_state[key]
        st.rerun()


    # Confirmação de exclusão
    if st.session_state.get("confirm_delete"):
        if is_monitor:
             st.session_state.confirm_delete = False
             st.error("Monitores não podem excluir alunos.")
        else:
            st.warning(f"Excluir {selected_student}?")
            col_d1, col_d2 = st.columns(2)
            if col_d1.button("✅ Sim"):
                delete_student(selected_student)
                st.session_state.confirm_delete = False
                st.rerun()
            if col_d2.button("❌ Não"):
                st.session_state.confirm_delete = False
                st.rerun()

# ==============================================================================
# VIEW: DASHBOARD
# ==============================================================================
if app_mode == "📊 Painel de Gestão":
    # Data e Hora (Fuso BR)
    fuso_br = timezone(timedelta(hours=-3))
    agora = datetime.now(fuso_br)
    
    dias_semana = ["Segunda-feira", "Terça-feira", "Quarta-feira", "Quinta-feira", "Sexta-feira", "Sábado", "Domingo"]
    meses = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"]
    
    dia_str = dias_semana[agora.weekday()]
    mes_str = meses[agora.month - 1]
    data_formatada = f"{dia_str}, {agora.day} de {mes_str} de {agora.year}"
    
    st.markdown(f"""
    <div class="header-box" style="margin-top:-50px;">
        <div class="header-title">Painel de Gestão</div>
        <div class="header-subtitle">{data_formatada} | {agora.strftime('%H:%M')}</div>
    </div>
    """, unsafe_allow_html=True)
    
    df_dash = load_db()
    
    # --- CHECK DE ASSINATURAS PENDENTES ---
    pending_docs = []
    user_name_lower = st.session_state.get('usuario_nome', '').strip().lower()
    
    if not df_dash.empty and user_name_lower:
        for idx, row in df_dash.iterrows():
            try:
                d = json.loads(row['dados_json'])
                doc_uuid = d.get('doc_uuid')
                signatures = d.get('signatures', [])
                signed_names = [s.get('name', '').strip().lower() for s in signatures]
                
                # Check fields for possible citation
                fields_to_check = [
                    'prof_poli', 'prof_aee', 'prof_arte', 'prof_ef', 'prof_tec', 'gestor', 'coord', # PEI
                    'resp_sala', 'resp_ee', 'resp_dir', # Avaliação
                    'acompanhante' # Diario
                ]
                
                found_role = None
                for f in fields_to_check:
                    val = d.get(f)
                    if val and isinstance(val, str) and user_name_lower in val.strip().lower():
                        found_role = f
                        break
                
                if found_role and user_name_lower not in signed_names:
                    pending_docs.append(f"{row['nome']} - {row['tipo_doc']}")
            except: pass

    if pending_docs:
        st.warning(f"⚠️ **Atenção:** Você foi citado em {len(pending_docs)} documento(s) e necessita assinar digitalmente.")
        with st.expander("Ver documentos pendentes"):
            for p in pending_docs:
                st.write(f"- {p}")
        st.divider()
    
    # --- CÁLCULO DE MÉTRICAS ---
    # Contagem de alunos únicos
    if not df_dash.empty and "nome" in df_dash.columns:
        total_alunos = df_dash["nome"].nunique()
    else:
        total_alunos = 0
        
    total_pei = len(df_dash[df_dash["tipo_doc"] == "PEI"])
    total_caso = len(df_dash[df_dash["tipo_doc"] == "CASO"])
    total_pdi = len(df_dash[df_dash["tipo_doc"] == "PDI"])
    
    # Função Auxiliar de Progresso
    def calc_progress(row_json, keys_check):
        try:
            data = json.loads(row_json)
            filled = 0
            for k in keys_check:
                val = data.get(k)
                if val:
                    if isinstance(val, list) and len(val) > 0: filled += 1
                    elif isinstance(val, dict) and len(val) > 0: filled += 1
                    elif isinstance(val, str) and val.strip() != "": filled += 1
                    elif isinstance(val, (int, float)): filled += 1
                    elif val is True: filled += 1
            return int((filled / len(keys_check)) * 100)
        except: return 0

    # --- DEFINIÇÃO DAS CHAVES ESSENCIAIS PARA CADA DOCUMENTO ---
    
    keys_pei = [
        'prof_poli', 'prof_aee',       # 1. Identificação
        'defic_txt', 'saude_extra',    # 2. Saúde
        'beh_interesses', 'beh_desafios', # 3. Conduta
        'dev_afetivo',                 # 4. Escolar
        'aval_port', 'aval_ling_verbal', # 5. Acadêmico (um dos dois)
        'meta_social_obj', 'meta_acad_obj', # 6. Metas
        'plano_obs_geral'              # Final
    ]

    keys_caso = [
        'endereco', 'quem_mora',                   # Identificação e Família
        'hist_idade_entrou', 'gest_parentesco',    # Histórico e Gestação
        'saude_prob', 'med_uso',                   # Saúde
        'entrevista_prof', 'entrevista_resp'       # Comportamento / Entrevista
    ]

    keys_aval = [
        'aspectos_gerais', 'defic_chk',            # Identificação
        'alim_nivel', 'hig_nivel', 'loc_nivel',    # Parte I
        'comportamento', 'part_grupo', 'interacao',# Parte II
        'rotina', 'ativ_pedag',                    # Parte III
        'atencao_sust', 'linguagem',               # Parte IV
        'conclusao_nivel', 'resp_ee'               # Conclusão
    ]

    keys_pdi = [
        'potencialidades', 'areas_interesse',      # Avaliação Inicial
        'acao_escola', 'acao_sala', 'acao_familia',# Ações Necessárias
        'aee_tempo', 'aee_tipo',                   # Organização AEE
        'goals_specific'                           # Objetivos Detalhados
    ]
    
    concluidos = 0
    deficiencies_count = {}
    
    # --- INICIALIZAÇÃO DAS LISTAS DE PROGRESSO ---
    pei_progress_list = []
    caso_progress_list = []
    apoio_progress_list = []
    pdi_progress_list = []

    # --- LOOP DE CÁLCULO GERAL ---
    for idx, row in df_dash.iterrows():
        try:
            d = json.loads(row['dados_json'])
            
            # Gráfico de Deficiências
            for dtype in d.get('diag_tipo', []):
                deficiencies_count[dtype] = deficiencies_count.get(dtype, 0) + 1
            if "Deficiência" in d.get('diag_tipo', []) and d.get('defic_txt'):
                d_txt = d.get('defic_txt').upper().strip()
                deficiencies_count[d_txt] = deficiencies_count.get(d_txt, 0) + 1
            
            # Separação por Tipo de Documento e Cálculo
            tipo_documento = row['tipo_doc']
            nome_aluno = row['nome']
            
            if tipo_documento == "PEI":
                prog = calc_progress(row['dados_json'], keys_pei)
                pei_progress_list.append({"Aluno": nome_aluno, "Progresso": prog})
                if prog >= 90: concluidos += 1
                
            elif tipo_documento == "CASO":
                prog = calc_progress(row['dados_json'], keys_caso)
                caso_progress_list.append({"Aluno": nome_aluno, "Progresso": prog})
                
            elif tipo_documento == "AVALIACAO":
                prog = calc_progress(row['dados_json'], keys_aval)
                apoio_progress_list.append({"Aluno": nome_aluno, "Progresso": prog})
                
            elif tipo_documento == "PDI":
                prog = calc_progress(row['dados_json'], keys_pdi)
                pdi_progress_list.append({"Aluno": nome_aluno, "Progresso": prog})
                
        except: pass

# --- CÁLCULO DE NOVAS MÉTRICAS DE GESTÃO ---
    
    # 1. Total de Alunos Únicos
    total_alunos = df_dash["nome"].nunique() if not df_dash.empty and "nome" in df_dash.columns else 0
    
    # 2. Alunos com Laudo Médico / Diagnóstico Conclusivo (NOVA MÉTRICA)
    alunos_com_laudo = set()
    if not df_dash.empty:
        for _, row in df_dash.iterrows():
            try:
                d_laudo = json.loads(row['dados_json'])
                # Checa no PEI se marcou "Sim" para diagnóstico conclusivo
                if row['tipo_doc'] == "PEI" and d_laudo.get('diag_status') == "Sim":
                    alunos_com_laudo.add(row['nome'])
                # Ou checa no Estudo de Caso se o campo "Possui diagnóstico" foi preenchido
                elif row['tipo_doc'] == "CASO" and d_laudo.get('diag_possui') and str(d_laudo.get('diag_possui')).strip():
                    alunos_com_laudo.add(row['nome'])
            except: pass
    total_laudos = len(alunos_com_laudo)
    
    # 3. Documentos em Elaboração (PEIs e PDIs abaixo de 100%)
    docs_em_elaboracao = sum(1 for p in pei_progress_list + pdi_progress_list if p['Progresso'] < 100)
    
    # 4. Alunos com necessidade de Profissional de Apoio (Extraído da Avaliação)
    total_apoio = 0
    if not df_dash.empty:
        df_aval = df_dash[df_dash["tipo_doc"] == "AVALIACAO"]
        for _, row in df_aval.iterrows():
            try:
                d_aval = json.loads(row['dados_json'])
                nivel = d_aval.get('conclusao_nivel', '')
                if "Nível 2" in nivel or "Nível 3" in nivel or d_aval.get('apoio_existente'):
                    total_apoio += 1
            except: pass

    # 5. Estudos de Caso Realizados 
    total_caso = len(df_dash[df_dash["tipo_doc"] == "CASO"]) if not df_dash.empty else 0


    # --- CARDS DE MÉTRICAS ---
    # CSS inline para dar destaque aos números que exigem atenção
    cor_elaboracao = "#ea580c" if docs_em_elaboracao > 0 else "#64748b"

    col_m1, col_m2, col_m3, col_m4, col_m5 = st.columns(5)
    
    col_m1.markdown(f'<div class="metric-card"><div class="metric-value">{total_alunos}</div><div class="metric-label">👥 Total AEE</div></div>', unsafe_allow_html=True)
    
    col_m2.markdown(f'<div class="metric-card"><div class="metric-value">{total_apoio}</div><div class="metric-label">🤝 Apoio Escolar</div></div>', unsafe_allow_html=True)
    
    col_m3.markdown(f'<div class="metric-card"><div class="metric-value" style="color: {cor_elaboracao};">{docs_em_elaboracao}</div><div class="metric-label">⏳ Em elaboração</div></div>', unsafe_allow_html=True)
    
    col_m4.markdown(f'<div class="metric-card"><div class="metric-value" style="color: #0284c7;">{total_laudos}</div><div class="metric-label">📄 Com Laudo</div></div>', unsafe_allow_html=True)
    
    col_m5.markdown(f'<div class="metric-card"><div class="metric-value" style="color: #1e3a8a;">{total_caso}</div><div class="metric-label">📋 Estudos Caso</div></div>', unsafe_allow_html=True)
    
    st.divider()

# --- ABAS DO DASHBOARD ---
    tab_graf, tab_com = st.tabs(["📊 Estatísticas & Progresso", "📢 Comunicação & Agenda"])
    
    with tab_graf:
        c_chart, c_prog = st.columns([1, 1])
        with c_chart:
            st.subheader("Tipos de Deficiência")
            if deficiencies_count:
                df_def = pd.DataFrame(list(deficiencies_count.items()), columns=["Tipo", "Qtd"])
                st.bar_chart(df_def.set_index("Tipo"), color="#1e3a8a")
            else:
                st.info("Sem dados suficientes.")
        
        with c_prog:
            st.subheader("Progresso de Preenchimento")
            
            # 1. Cria o seletor de documentos
            tipo_doc = st.selectbox(
                "Selecione o documento:",
                ["PEI", "Estudo de Caso", "Avaliação de Apoio", "PDI"],
                label_visibility="collapsed" # Esconde o rótulo para ficar mais limpo
            )
            
            # 2. Define qual lista usar baseado na seleção
            lista_progresso_atual = []
            
            if tipo_doc == "PEI":
                # Sua lista original que já funciona
                lista_progresso_atual = pei_progress_list 
            elif tipo_doc == "Estudo de Caso":
                # Você precisará ter essa lista calculada no seu backend
                lista_progresso_atual = caso_progress_list 
            elif tipo_doc == "Avaliação de Apoio":
                # Você precisará ter essa lista calculada no seu backend
                lista_progresso_atual = apoio_progress_list 
            elif tipo_doc == "PDI":
                # Você precisará ter essa lista calculada no seu backend
                lista_progresso_atual = pdi_progress_list 

            # 3. Renderiza os gráficos da lista escolhida
            if lista_progresso_atual:
                # Opcional: ascending=False deixa os mais completos no topo
                df_prog = pd.DataFrame(lista_progresso_atual).sort_values("Progresso", ascending=False) 
                with st.container(height=300):
                    for _, row in df_prog.iterrows():
                        st.caption(f"{row['Aluno']} ({row['Progresso']}%)")
                        st.progress(row['Progresso'] / 100)
            else:
                st.info(f"Nenhum {tipo_doc} calculado ainda.")

    with tab_com:
        c_aviso, c_agenda = st.columns([1, 1])
        
        # --- MURAL DE AVISOS ---
        with c_aviso:
            st.markdown("### 📌 Mural de Avisos")
            if not is_monitor:
                with st.form("form_recado"):
                    txt_recado = st.text_area("Novo Recado", height=80)
                    if st.form_submit_button("Publicar"):
                        df_recados = safe_read("Recados", ["Data", "Autor", "Mensagem"])
                        novo_recado = {
                            "Data": datetime.now().strftime("%d/%m %H:%M"),
                            "Autor": st.session_state.get('usuario_nome', 'Admin'),
                            "Mensagem": txt_recado
                        }
                        df_recados = pd.concat([pd.DataFrame([novo_recado]), df_recados], ignore_index=True)
                        safe_update("Recados", df_recados)
                        st.cache_data.clear() # Limpa cache para atualizar
                        time.sleep(1) # Aguarda propagação
                        st.rerun()
            else:
                st.info("Apenas Docentes podem publicar avisos.")
            
            # Listar Recados
            df_recados = safe_read("Recados", ["Data", "Autor", "Mensagem"])
            if not df_recados.empty:
                with st.container(height=300):
                    for index, row in df_recados.iterrows():
                        c_msg, c_del = st.columns([0.85, 0.15])
                        with c_msg:
                            st.info(f"**{row['Autor']}** ({row['Data']}):\n\n{row['Mensagem']}")
                        with c_del:
                            if not is_monitor:
                                if st.button("🗑️", key=f"del_rec_{index}", help="Excluir recado"):
                                    df_recados = df_recados.drop(index)
                                    safe_update("Recados", df_recados)
                                    st.cache_data.clear()
                                    time.sleep(0.5)
                                    st.rerun()
            else:
                st.write("Nenhum recado.")

        # --- AGENDA DA EQUIPE ---
        with c_agenda:
            st.markdown("### 📅 Agenda da Equipe")
            if not is_monitor:
                with st.form("form_agenda"):
                    c_d, c_e = st.columns([1, 2])
                    data_evento = c_d.date_input("Data", format="DD/MM/YYYY")
                    desc_evento = c_e.text_input("Evento")
                    if st.form_submit_button("Agendar"):
                        df_agenda = safe_read("Agenda", ["Data", "Evento", "Autor"])
                        novo_evento = {
                            "Data": data_evento.strftime("%Y-%m-%d"),
                            "Evento": desc_evento,
                            "Autor": st.session_state.get('usuario_nome', 'Admin')
                        }
                        df_agenda = pd.concat([df_agenda, pd.DataFrame([novo_evento])], ignore_index=True)
                        # Ordenar por data
                        df_agenda = df_agenda.sort_values(by="Data", ascending=False)
                        safe_update("Agenda", df_agenda)
                        st.cache_data.clear() # Limpa cache para atualizar
                        time.sleep(1) # Aguarda propagação
                        st.rerun()
            else:
                st.info("Apenas Docentes podem adicionar eventos.")
            
            # Listar Agenda
            df_agenda = safe_read("Agenda", ["Data", "Evento", "Autor"])
            if not df_agenda.empty:
                with st.container(height=300):
                    for index, row in df_agenda.iterrows():
                        try:
                            d_fmt = datetime.strptime(str(row['Data']), "%Y-%m-%d").strftime("%d/%m")
                        except:
                            d_fmt = str(row['Data'])
                        
                        c_evt, c_del_evt = st.columns([0.85, 0.15])
                        with c_evt:
                            st.write(f"🗓️ **{d_fmt}** - {row['Evento']} _({row['Autor']})_")
                        with c_del_evt:
                            if not is_monitor:
                                if st.button("🗑️", key=f"del_agd_{index}", help="Excluir evento"):
                                    df_agenda = df_agenda.drop(index)
                                    safe_update("Agenda", df_agenda)
                                    st.cache_data.clear()
                                    time.sleep(0.5)
                                    st.rerun()
            else:
                st.write("Agenda vazia.")

# ==============================================================================
# VIEW: GESTÃO DE ALUNOS (PEI / CASO)
# ==============================================================================
elif app_mode == "👥 Gestão de Alunos":
    
    # --- PORTÃO DE ENTRADA ---
    if not st.session_state.ee_aluno_confirmado or not st.session_state.ee_doc_confirmado:
        st.markdown("""
        <div style='background-color: white; padding: 2rem; border-radius: 10px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); border: 1px solid #e2e8f0;'>
            <h3 style='color: #1e293b; margin-top:0;'>🚪 Selecione o Estudante e o Documento</h3>
            <p style='color: #64748b;'>Para iniciar o preenchimento ou edição, selecione abaixo o estudante e o tipo de documento desejado.</p>
        </div>
        <br>
        """, unsafe_allow_html=True)

        df_db = load_db()
        lista_nomes = df_db["nome"].dropna().unique().tolist() if not df_db.empty else []
        opcoes_nomes = ["-- Novo Registro --"] + lista_nomes

        c_aluno, c_doc = st.columns(2)
        aluno_sel = c_aluno.selectbox("1. Selecione o Estudante:", opcoes_nomes)

        # Se for novo registro, abre campo para digitar o nome
        nome_novo = ""
        if aluno_sel == "-- Novo Registro --":
            nome_novo = c_aluno.text_input("Digite o nome completo do novo estudante:")

        # LISTA DE DOCUMENTOS ATUALIZADA (PEI Dividido)
        docs_disponiveis = [
            "Estudo de Caso", 
            "PEI - Ensino Fundamental", 
            "PEI - Educação Infantil", 
            "PDI", 
            "Protocolo de Conduta", 
            "Avaliação de Apoio", 
            "Relatório de Acompanhamento", 
            "Declaração de Matrícula"
        ]
        doc_sel = c_doc.selectbox("2. Selecione o Documento:", docs_disponiveis)

        st.write("")
        if st.button("✅ Confirmar e Acessar Documento", type="primary", use_container_width=True):
            nome_final = nome_novo if aluno_sel == "-- Novo Registro --" else aluno_sel
            if nome_final and nome_final.strip() != "":
                st.session_state.ee_aluno_confirmado = nome_final
                st.session_state.ee_doc_confirmado = doc_sel
                
                # Seta o nome para a função carregar_dados_aluno puxar do banco
                st.session_state.aluno_selecionado = nome_final 
                carregar_dados_aluno()
                st.rerun()
            else:
                st.warning("⚠️ Por favor, informe o nome do estudante.")

        # Interrompe a renderização para não mostrar os docs em branco embaixo
        st.stop() 

    # --- BARRA DE INFORMAÇÃO E NAVEGAÇÃO ---
    c_info, c_btn, c_del = st.columns([6, 2, 1])
    c_info.success(f"📌 **Documento em edição:** {st.session_state.ee_doc_confirmado} - do estudante {st.session_state.ee_aluno_confirmado}")
    
    if c_btn.button("⬅️ Trocar Aluno/Documento", use_container_width=True):
        st.session_state.ee_aluno_confirmado = None
        st.session_state.ee_doc_confirmado = None
        st.session_state.aluno_selecionado = None
        st.rerun()
        
    if not is_monitor:
        if c_del.button("🗑️ Excluir Aluno", use_container_width=True, help="Atenção: Isso excluirá o estudante e TODOS os seus documentos do banco."):
            st.session_state.confirm_delete = True

    # --- LÓGICA DE INTERPRETAÇÃO DO DOCUMENTO (PEI Fundamental vs Infantil) ---
    escolha_doc = st.session_state.ee_doc_confirmado
    selected_student = st.session_state.ee_aluno_confirmado
    
    if escolha_doc == "PEI - Ensino Fundamental":
        doc_mode = "PEI"
        pei_level = "Fundamental"
    elif escolha_doc == "PEI - Educação Infantil":
        doc_mode = "PEI"
        pei_level = "Infantil"
    else:
        doc_mode = escolha_doc
        pei_level = "Fundamental" # Padrão para os outros documentos

    st.divider()

    # PEI COM FORMULÁRIOS
    if doc_mode == "PEI":
        st.markdown(f"""<div class="header-box"><div class="header-title">Plano Educacional Individualizado - PEI ({pei_level})</div></div>""", unsafe_allow_html=True)
        
        st.markdown("""<style>div[data-testid="stFormSubmitButton"] > button {width: 100%; background-color: #dcfce7; color: #166534; border: 1px solid #166534;}</style>""", unsafe_allow_html=True)

        tabs = st.tabs(["1. Identificação", "2. Saúde", "3. Conduta", "4. Escolar", "5. Acadêmico", "6. Metas/Flex", "7. Assinaturas", "8. Emissão", "9. Histórico"])
        data = st.session_state.data_pei

        # --- ABA 1: IDENTIFICAÇÃO ---
        with tabs[0]:
            with st.form("form_pei_identificacao") if not is_monitor else st.container():
                st.subheader("1. Identificação")
                
                # --- LAYOUT COM FOTO ---
                col_img, col_data = st.columns([1, 4])
                
                with col_img:
                    st.markdown("📷 **Foto**")
                    # Se ja tiver foto, mostra
                    if data.get('foto_base64'):
                        try:
                            b = base64.b64decode(data['foto_base64'])
                            st.image(b, use_container_width=True)
                            if not is_monitor:
                                if st.checkbox("Remover", key="rem_foto_pei"):
                                    data['foto_base64'] = None
                        except:
                            st.error("Erro foto")
                    
                    # Upload
                    uploaded_photo = st.file_uploader("Carregar", type=["jpg", "jpeg", "png"], label_visibility="collapsed", key="up_foto_pei", disabled=is_monitor)
                    if uploaded_photo and not is_monitor:
                        try:
                            img = Image.open(uploaded_photo)
                            if img.mode != 'RGB': img = img.convert('RGB')
                            # Resize para não pesar no banco
                            img.thumbnail((300, 400))
                            buf = io.BytesIO()
                            img.save(buf, format="JPEG", quality=85)
                            data['foto_base64'] = base64.b64encode(buf.getvalue()).decode()
                            st.success("OK!")
                        except Exception as e:
                            st.error(f"Erro: {e}")
                
                with col_data:
                    c1, c2 = st.columns([3, 1])
                    data['nome'] = c1.text_input("Nome", value=data.get('nome', ''), disabled=True)
                    
                    # --- TRATAMENTO ROBUSTO ---
                    d_val = data.get('nasc')
                    if isinstance(d_val, str): 
                        try: d_val = datetime.strptime(d_val, '%Y-%m-%d').date()
                        except: d_val = date.today()
                    
                    if not isinstance(d_val, date):
                        d_val = date.today()

                    # Garante que o valor inicial não quebre o widget
                    d_val = max(MIN_DATA, min(d_val, MAX_DATA))

                    # CHAVE ÚNICA PARA FORÇAR O RESET
                    # Mudando o nome da chave de 'data_nasc_unique' para 'nasc_fix_v3'
                    # o Streamlit ignora o bloqueio anterior de 2016.
                    input_key = f"nasc_fix_{data.get('nome', 'novo').replace(' ', '_')}"

                    data['nasc'] = c2.date_input(
                        "Nascimento", 
                        value=d_val,
                        min_value=MIN_DATA, 
                        max_value=MAX_DATA,
                        format="DD/MM/YYYY", 
                        disabled=is_monitor,
                        key=input_key
                    )
                    
                    c3, c4 = st.columns(2)
                    data['idade'] = c3.text_input("Idade", value=data.get('idade', ''), disabled=is_monitor)
                    data['ano_esc'] = c4.text_input("Ano Escolar", value=data.get('ano_esc', ''), disabled=is_monitor)
                    
                    data['mae'] = st.text_input("Nome da Mãe", value=data.get('mae', ''), disabled=is_monitor)
                    data['pai'] = st.text_input("Nome do Pai", value=data.get('pai', ''), disabled=is_monitor)
                    data['tel'] = st.text_input("Telefone", value=data.get('tel', ''), disabled=is_monitor)
                
                st.markdown("**Docentes Responsáveis**")
                d1, d2, d3 = st.columns(3)
                data['prof_poli'] = d1.text_input("Polivalente/Regente", value=data.get('prof_poli', ''), disabled=is_monitor)
                data['prof_aee'] = d2.text_input("Prof. AEE", value=data.get('prof_aee', ''), disabled=is_monitor)
                data['prof_arte'] = d3.text_input("Arte", value=data.get('prof_arte', ''), disabled=is_monitor)
                
                d4, d5, d6 = st.columns(3)
                data['prof_ef'] = d4.text_input("Ed. Física", value=data.get('prof_ef', ''), disabled=is_monitor)
                data['prof_tec'] = d5.text_input("Tecnologia", value=data.get('prof_tec', ''), disabled=is_monitor)
                data['gestor'] = d6.text_input("Gestor Escolar", value=data.get('gestor', ''), disabled=is_monitor)
                
                dg1, dg2 = st.columns(2)
                data['coord'] = dg1.text_input("Coordenação", value=data.get('coord', ''), disabled=is_monitor)
                data['revisoes'] = st.text_input("Revisões", value=data.get('revisoes', ''), disabled=is_monitor)
                
                elab_opts = ["1º Trimestre", "2º Trimestre", "3º Trimestre", "Anual"]
                idx_elab = elab_opts.index(data['elab_per']) if data.get('elab_per') in elab_opts else 0
                data['elab_per'] = st.selectbox("Período", elab_opts, index=idx_elab, disabled=is_monitor)

                st.markdown("---")
                if not is_monitor:
                    if st.form_submit_button("💾 Salvar Identificação"):
                        save_student("PEI", data.get('nome'), data, "Identificação")

        # --- ABA 2: SAÚDE ---
        with tabs[1]:
            with st.form("form_pei_saude") if not is_monitor else st.container():
                st.subheader("Informações de Saúde")
                diag_idx = 0 if data.get('diag_status') == "Sim" else 1
                data['diag_status'] = st.radio("Diagnóstico conclusivo?", ["Sim", "Não"], horizontal=True, index=diag_idx, disabled=is_monitor)
                
                c_l1, c_l2 = st.columns(2)
                ld_val = data.get('laudo_data')
                if isinstance(ld_val, str): 
                    try: ld_val = datetime.strptime(ld_val, '%Y-%m-%d').date()
                    except: ld_val = date.today()
                data['laudo_data'] = c_l1.date_input("Data do Laudo Médico", value=ld_val if ld_val else date.today(), format="DD/MM/YYYY", disabled=is_monitor)
                data['laudo_medico'] = c_l2.text_input("Médico Responsável pelo Laudo", value=data.get('laudo_medico', ''), disabled=is_monitor)
                
                st.markdown("Categorias de Diagnóstico:")
                cats = ["Deficiência", "Transtorno do Neurodesenvolvimento", "Transtornos Aprendizagem", "AH/SD", "Outros"]
                if 'diag_tipo' not in data: data['diag_tipo'] = []
                
                c_c1, c_c2 = st.columns(2)
                for i, cat in enumerate(cats):
                    col = c_c1 if i % 2 == 0 else c_c2
                    is_checked = cat in data['diag_tipo']
                    if col.checkbox(cat, value=is_checked, key=f"pei_chk_{i}", disabled=is_monitor):
                        if cat not in data['diag_tipo']: data['diag_tipo'].append(cat)
                    else:
                        if cat in data['diag_tipo']: data['diag_tipo'].remove(cat)
                
                data['defic_txt'] = st.text_input("Descrição da Deficiência", value=data.get('defic_txt', ''), disabled=is_monitor)
                data['neuro_txt'] = st.text_input("Descrição do Transtorno Neuro", value=data.get('neuro_txt', ''), disabled=is_monitor)
                data['aprend_txt'] = st.text_input("Descrição do Transtorno de Aprendizagem", value=data.get('aprend_txt', ''), disabled=is_monitor)

                st.divider()
                st.markdown("**Terapias que realiza**")
                especs = ["Psicologia", "Fonoaudiologia", "Terapia Ocupacional", "Psicopedagogia", "Fisioterapia", "Outros"]
                if 'terapias' not in data: data['terapias'] = {}
                
                for esp in especs:
                    st.markdown(f"**{esp}**")
                    if esp not in data['terapias']: data['terapias'][esp] = {'realiza': False, 'dias': [], 'horario': ''}
                    
                    c_t1, c_t2, c_t3 = st.columns([1, 2, 2])
                    data['terapias'][esp]['realiza'] = c_t1.checkbox("Realiza?", value=data['terapias'][esp].get('realiza', False), key=f"pei_terapias_{esp}", disabled=is_monitor)
                    
                    data['terapias'][esp]['dias'] = c_t2.multiselect("Dias", ["2ª", "3ª", "4ª", "5ª", "6ª", "Sábado", "Domingo"], default=data['terapias'][esp].get('dias', []), key=f"pei_dias_{esp}", disabled=is_monitor)
                    data['terapias'][esp]['horario'] = c_t3.text_input("Horário", value=data['terapias'][esp].get('horario', ''), key=f"pei_hor_{esp}", disabled=is_monitor)
                    
                    if esp == "Outros":
                        data['terapias'][esp]['nome_custom'] = st.text_input("Especifique (Outros):", value=data['terapias'][esp].get('nome_custom', ''), key="pei_custom_name", disabled=is_monitor)
                    st.divider()

                data['med_nome'] = st.text_area("Nome da(s) Medicação(ões)", value=data.get('med_nome', ''), disabled=is_monitor)
                m1, m2 = st.columns(2)
                data['med_hor'] = m1.text_input("Horário(s)", value=data.get('med_hor', ''), disabled=is_monitor)
                data['med_doc'] = m2.text_input("Médico Responsável (Medicação)", value=data.get('med_doc', ''), disabled=is_monitor)
                data['med_obj'] = st.text_area("Objetivo da medicação", value=data.get('med_obj', ''), disabled=is_monitor)
                data['saude_extra'] = st.text_area("Outras informações de saúde:", value=data.get('saude_extra', ''), disabled=is_monitor)

                st.markdown("---")
                if not is_monitor:
                    if st.form_submit_button("💾 Salvar Saúde"):
                        save_student("PEI", data.get('nome'), data, "Saúde")

        # --- ABA 3: CONDUTA ---
        with tabs[2]:
            with st.form("form_pei_conduta") if not is_monitor else st.container():
                st.subheader("3. Protocolo de Conduta")
                st.markdown("### 🗣️ COMUNICAÇÃO")
                com_opts = ["Oralmente", "Não comunica", "Não se aplica", "Comunicação alternativa"]
                idx_com = com_opts.index(data['com_tipo']) if data.get('com_tipo') in com_opts else 0
                data['com_tipo'] = st.selectbox("Como o estudante se comunica?", com_opts, index=idx_com, disabled=is_monitor)
                data['com_alt_espec'] = st.text_input("Especifique (Comunicação alternativa):", value=data.get('com_alt_espec', ''), disabled=is_monitor)
                
                nec_idx = 0 if data.get('com_necessidades') == 'Sim' else 1
                data['com_necessidades'] = st.radio("Expressa necessidades/desejos?", ["Sim", "Não"], horizontal=True, index=nec_idx, disabled=is_monitor)
                data['com_necessidades_espec'] = st.text_input("Especifique necessidades:", value=data.get('com_necessidades_espec', ''), disabled=is_monitor)
                
                cha_idx = 0 if data.get('com_chamado') == 'Sim' else 1
                data['com_chamado'] = st.radio("Atende quando é chamado?", ["Sim", "Não"], horizontal=True, index=cha_idx, disabled=is_monitor)
                data['com_chamado_espec'] = st.text_input("Especifique chamado:", value=data.get('com_chamado_espec', ''), disabled=is_monitor)
                
                cmd_idx = 0 if data.get('com_comandos') == 'Sim' else 1
                data['com_comandos'] = st.radio("Responde a comandos simples?", ["Sim", "Não"], horizontal=True, index=cmd_idx, disabled=is_monitor)
                data['com_comandos_espec'] = st.text_input("Especifique comandos:", value=data.get('com_comandos_espec', ''), disabled=is_monitor)

                st.divider()
                st.markdown("### 🚶 LOCOMOÇÃO")
                loc_r_idx = 1 if data.get('loc_reduzida') == 'Sim' else 0
                data['loc_reduzida'] = st.radio("Possui mobilidade reduzida?", ["Não", "Sim"], horizontal=True, index=loc_r_idx, disabled=is_monitor)
                data['loc_reduzida_espec'] = st.text_input("Especifique mobilidade:", value=data.get('loc_reduzida_espec', ''), disabled=is_monitor)
                
                c_l1, c_l2 = st.columns(2)
                amb_idx = 0 if data.get('loc_ambiente') == 'Sim' else 1
                data['loc_ambiente'] = c_l1.radio("Locomove-se pela casa?", ["Sim", "Não"], horizontal=True, index=amb_idx, disabled=is_monitor)
                helper_idx = 0 if data.get('loc_ambiente_ajuda') == 'Com autonomia' else 1
                data['loc_ambiente_ajuda'] = c_l2.selectbox("Grau:", ["Com autonomia", "Com ajuda"], index=helper_idx, disabled=is_monitor)
                data['loc_ambiente_espec'] = st.text_input("Especifique locomoção:", value=data.get('loc_ambiente_espec', ''), disabled=is_monitor)

                st.divider()
                st.markdown("### 🧼 AUTOCUIDADO E HIGIENE")
                c_h1, c_h2 = st.columns(2)
                wc_idx = 0 if data.get('hig_banheiro') == 'Sim' else 1
                data['hig_banheiro'] = c_h1.radio("Utiliza o banheiro?", ["Sim", "Não"], horizontal=True, index=wc_idx, disabled=is_monitor)
                wc_help_idx = 0 if data.get('hig_banheiro_ajuda') == 'Com autonomia' else 1
                data['hig_banheiro_ajuda'] = c_h2.selectbox("Ajuda banheiro:", ["Com autonomia", "Com ajuda"], index=wc_help_idx, disabled=is_monitor)
                data['hig_banheiro_espec'] = st.text_input("Especifique banheiro:", value=data.get('hig_banheiro_espec', ''), disabled=is_monitor)
                
                c_h3, c_h4 = st.columns(2)
                tooth_idx = 0 if data.get('hig_dentes') == 'Sim' else 1
                data['hig_dentes'] = c_h3.radio("Escova os dentes?", ["Sim", "Não"], horizontal=True, index=tooth_idx, disabled=is_monitor)
                tooth_help_idx = 0 if data.get('hig_dentes_ajuda') == 'Com autonomia' else 1
                data['hig_dentes_ajuda'] = c_h4.selectbox("Ajuda dentes:", ["Com autonomia", "Com ajuda"], index=tooth_help_idx, disabled=is_monitor)
                data['hig_dentes_espec'] = st.text_input("Especifique dentes:", value=data.get('hig_dentes_espec', ''), disabled=is_monitor)

                st.divider()
                st.markdown("### 🧩 COMPORTAMENTO")
                data['beh_interesses'] = st.text_area("Interesses do estudante:", value=data.get('beh_interesses', ''), disabled=is_monitor)
                data['beh_objetos_gosta'] = st.text_area("Objetos que gosta / Apego:", value=data.get('beh_objetos_gosta', ''), disabled=is_monitor)
                data['beh_objetos_odeia'] = st.text_area("Objetos que não gosta / Aversão:", value=data.get('beh_objetos_odeia', ''), disabled=is_monitor)
                data['beh_toque'] = st.text_area("Gosta de toque/abraço?", value=data.get('beh_toque', ''), disabled=is_monitor)
                data['beh_calmo'] = st.text_area("O que o acalma?", value=data.get('beh_calmo', ''), disabled=is_monitor)
                data['beh_atividades'] = st.text_area("Atividades prazerosas:", value=data.get('beh_atividades', ''), disabled=is_monitor)
                data['beh_gatilhos'] = st.text_area("Gatilhos de crise:", value=data.get('beh_gatilhos', ''), disabled=is_monitor)
                data['beh_crise_regula'] = st.text_area("Como se regula na crise?", value=data.get('beh_crise_regula', ''), disabled=is_monitor)
                data['beh_desafios'] = st.text_area("Comportamentos desafiadores / Manejo:", value=data.get('beh_desafios', ''), disabled=is_monitor)
                
                c_b1, c_b2 = st.columns([1, 2])
                food_idx = 1 if data.get('beh_restricoes') == 'Sim' else 0
                data['beh_restricoes'] = c_b1.radio("Restrições alimentares?", ["Não", "Sim"], horizontal=True, index=food_idx, disabled=is_monitor)
                data['beh_restricoes_espec'] = c_b2.text_input("Especifique alimentação:", value=data.get('beh_restricoes_espec', ''), disabled=is_monitor)
                
                c_b3, c_b4 = st.columns([1, 2])
                water_idx = 0 if data.get('beh_autonomia_agua') == 'Sim' else 1
                data['beh_autonomia_agua'] = c_b3.radio("Autonomia (água/comida)?", ["Sim", "Não"], horizontal=True, index=water_idx, disabled=is_monitor)
                data['beh_autonomia_agua_espec'] = c_b4.text_input("Especifique autonomia:", value=data.get('beh_autonomia_agua_espec', ''), disabled=is_monitor)
                
                data['beh_pertinentes'] = st.text_area("Outras informações:", value=data.get('beh_pertinentes', ''), disabled=is_monitor)

                st.markdown("---")
                if not is_monitor:
                    if st.form_submit_button("💾 Salvar Conduta"):
                        save_student("PEI", data.get('nome'), data, "Conduta")

        # --- ABA 4: ESCOLAR ---
        with tabs[3]:
            with st.form("form_pei_escolar") if not is_monitor else st.container():
                st.subheader("4. Desenvolvimento Escolar")
                
                c_p1, c_p2 = st.columns([1, 2])
                perm_opts = ["Sim - Por longo período", "Sim - Por curto período", "Não"]
                idx_perm = perm_opts.index(data.get('dev_permanece')) if data.get('dev_permanece') in perm_opts else 0
                data['dev_permanece'] = c_p1.selectbox("Permanece em sala?", perm_opts, index=idx_perm, disabled=is_monitor)
                data['dev_permanece_espec'] = c_p2.text_input("Obs Permanência:", value=data.get('dev_permanece_espec', ''), disabled=is_monitor)

                c_i1, c_i2 = st.columns([1, 2])
                int_idx = 0 if data.get('dev_integrado') == 'Sim' else 1
                data['dev_integrado'] = c_i1.radio("Integrado ao ambiente?", ["Sim", "Não"], horizontal=True, index=int_idx, disabled=is_monitor)
                data['dev_integrado_espec'] = c_i2.text_input("Obs Integração:", value=data.get('dev_integrado_espec', ''), disabled=is_monitor)

                c_l1, c_l2 = st.columns([1, 2])
                loc_opts = ["Sim - Com autonomia", "Sim - Com ajuda", "Não"]
                idx_loc = loc_opts.index(data.get('dev_loc_escola')) if data.get('dev_loc_escola') in loc_opts else 0
                data['dev_loc_escola'] = c_l1.selectbox("Locomove-se pela escola?", loc_opts, index=idx_loc, disabled=is_monitor)
                data['dev_loc_escola_espec'] = c_l2.text_input("Obs Locomoção:", value=data.get('dev_loc_escola_espec', ''), disabled=is_monitor)

                c_t1, c_t2 = st.columns([1, 2])
                tar_opts = ["Sim - Com autonomia", "Sim - Com ajuda", "Não"]
                idx_tar = tar_opts.index(data.get('dev_tarefas')) if data.get('dev_tarefas') in tar_opts else 0
                data['dev_tarefas'] = c_t1.selectbox("Realiza tarefas?", tar_opts, index=idx_tar, disabled=is_monitor)
                data['dev_tarefas_espec'] = c_t2.text_input("Obs Tarefas:", value=data.get('dev_tarefas_espec', ''), disabled=is_monitor)

                c_a1, c_a2 = st.columns([1, 2])
                amg_idx = 0 if data.get('dev_amigos') == 'Sim' else 1
                data['dev_amigos'] = c_a1.radio("Tem amigos?", ["Sim", "Não"], horizontal=True, index=amg_idx, disabled=is_monitor)
                data['dev_amigos_espec'] = c_a2.text_input("Obs Amigos:", value=data.get('dev_amigos_espec', ''), disabled=is_monitor)

                data['dev_colega_pref'] = st.radio("Tem colega predileto?", ["Sim", "Não"], horizontal=True, index=0 if data.get('dev_colega_pref') == 'Sim' else 1, disabled=is_monitor)

                c_ia1, c_ia2 = st.columns([1, 2])
                ia_idx = 0 if data.get('dev_participa') == 'Sim' else 1
                data['dev_participa'] = c_ia1.radio("Participa/Interage?", ["Sim", "Não"], horizontal=True, index=ia_idx, disabled=is_monitor)
                data['dev_participa_espec'] = c_ia2.text_input("Obs Interação:", value=data.get('dev_participa_espec', ''), disabled=is_monitor)

                data['dev_afetivo'] = st.text_area("Envolvimento afetivo/social da turma:", value=data.get('dev_afetivo', ''), disabled=is_monitor)

                st.markdown("---")
                if not is_monitor:
                    if st.form_submit_button("💾 Salvar Escolar"):
                        save_student("PEI", data.get('nome'), data, "Escolar")

        # --- ABA 5: ACADÊMICO ---
        with tabs[4]:
            with st.form("form_pei_academico") if not is_monitor else st.container():
                st.subheader("5. Avaliação Acadêmica")
                
                if pei_level == "Fundamental":
                    c_f1, c_f2 = st.columns(2)
                    data['aval_port'] = c_f1.text_area("Língua Portuguesa", value=data.get('aval_port', ''), disabled=is_monitor)
                    data['aval_mat'] = c_f2.text_area("Matemática", value=data.get('aval_mat', ''), disabled=is_monitor)
                    data['aval_con_gerais'] = st.text_area("Conhecimentos Gerais", value=data.get('aval_con_gerais', ''), disabled=is_monitor)

                    st.markdown("**ARTE**")
                    data['aval_arte_visuais'] = st.text_area("Artes Visuais", value=data.get('aval_arte_visuais', ''), disabled=is_monitor)
                    data['aval_arte_musica'] = st.text_area("Música", value=data.get('aval_arte_musica', ''), disabled=is_monitor)
                    c_a1, c_a2 = st.columns(2)
                    data['aval_arte_teatro'] = c_a1.text_area("Teatro", value=data.get('aval_arte_teatro', ''), disabled=is_monitor)
                    data['aval_arte_danca'] = c_a2.text_area("Dança", value=data.get('aval_arte_danca', ''), disabled=is_monitor)

                    st.markdown("**EDUCAÇÃO FÍSICA**")
                    c_ef1, c_ef2 = st.columns(2)
                    data['aval_ef_motoras'] = c_ef1.text_area("Habilidades Motoras", value=data.get('aval_ef_motoras', ''), disabled=is_monitor)
                    data['aval_ef_corp_conhec'] = c_ef2.text_area("Conhecimento Corporal", value=data.get('aval_ef_corp_conhec', ''), disabled=is_monitor)
                    data['aval_ef_exp'] = st.text_area("Exp. Corporais e Expressividade", value=data.get('aval_ef_exp', ''), disabled=is_monitor)
                    
                    st.markdown("**LINGUAGENS E TECNOLOGIAS**")
                    data['aval_ling_tec'] = st.text_area("Avaliação da disciplina:", value=data.get('aval_ling_tec', ''), disabled=is_monitor)
                else:
                    # Infantil
                    data['aval_ling_verbal'] = st.text_area("Linguagem Verbal", value=data.get('aval_ling_verbal', ''), disabled=is_monitor)
                    data['aval_ling_mat'] = st.text_area("Linguagem Matemática", value=data.get('aval_ling_mat', ''), disabled=is_monitor)
                    data['aval_ind_soc'] = st.text_area("Indivíduo e Sociedade", value=data.get('aval_ind_soc', ''), disabled=is_monitor)
                    
                    st.markdown("**ARTE**")
                    data['aval_arte_visuais'] = st.text_area("Artes Visuais", value=data.get('aval_arte_visuais', ''), disabled=is_monitor)
                    data['aval_arte_musica'] = st.text_area("Música", value=data.get('aval_arte_musica', ''), disabled=is_monitor)
                    data['aval_arte_teatro'] = st.text_area("Teatro", value=data.get('aval_arte_teatro', ''), disabled=is_monitor)

                    st.markdown("**EDUCAÇÃO FÍSICA**")
                    c_ef1, c_ef2, c_ef3 = st.columns(3)
                    data['aval_ef_jogos'] = c_ef1.text_area("Jogos/Brincadeiras", value=data.get('aval_ef_jogos', ''), disabled=is_monitor)
                    data['aval_ef_ritmo'] = c_ef2.text_area("Ritmo", value=data.get('aval_ef_ritmo', ''), disabled=is_monitor)
                    data['aval_ef_corp'] = c_ef3.text_area("Conhecimento Corporal", value=data.get('aval_ef_corp', ''), disabled=is_monitor)
                    
                    st.markdown("**LINGUAGEM E TECNOLOGIAS**")
                    data['aval_ling_tec'] = st.text_area("Avaliação da disciplina:", value=data.get('aval_ling_tec', ''), disabled=is_monitor)

                st.markdown("---")
                if not is_monitor:
                    if st.form_submit_button("💾 Salvar Acadêmico"):
                        save_student("PEI", data.get('nome'), data, "Acadêmico")

        # --- ABA 6: METAS E FLEXIBILIZAÇÃO (VERSÃO CORRIGIDA E SEGURA) ---
        with tabs[5]:
            # Identificador único para as keys (evita que dados de um aluno fiquem presos na tela de outro)
            aluno_id = data.get('nome', 'default')

            with st.form("form_pei_metas") if not is_monitor else st.container():
                st.header("6. Metas Específicas")
                
                st.subheader("Habilidades Sociais")
                data['meta_social_obj'] = st.text_area("Metas (Sociais):", value=data.get('meta_social_obj', ''), key=f"ms_obj_{aluno_id}", disabled=is_monitor)
                data['meta_social_est'] = st.text_area("Estratégias (Sociais):", value=data.get('meta_social_est', ''), key=f"ms_est_{aluno_id}", disabled=is_monitor)

                st.divider(); st.subheader("Autocuidado e Vida Prática")
                data['meta_auto_obj'] = st.text_area("Metas (Autocuidado):", value=data.get('meta_auto_obj', ''), key=f"ma_obj_{aluno_id}", disabled=is_monitor)
                data['meta_auto_est'] = st.text_area("Estratégias (Autocuidado):", value=data.get('meta_auto_est', ''), key=f"ma_est_{aluno_id}", disabled=is_monitor)

                st.divider(); st.subheader("Habilidades Acadêmicas")
                data['meta_acad_obj'] = st.text_area("Metas (Acadêmicas):", value=data.get('meta_acad_obj', ''), key=f"mac_obj_{aluno_id}", disabled=is_monitor)
                data['meta_acad_est'] = st.text_area("Estratégias (Acadêmicas):", value=data.get('meta_acad_est', ''), key=f"mac_est_{aluno_id}", disabled=is_monitor)

                st.header("7. Flexibilização Curricular")
                if pei_level == "Fundamental":
                    disciplinas_flex = ["Língua Portuguesa", "Matemática", "História", "Geografia", "Ciências", "Arte", "Educação Física", "Linguagens e Tecnologia"]
                else:
                    disciplinas_flex = ["Linguagem Verbal", "Linguagem Matemática", "Indivíduo e Sociedade", "Arte", "Educação Física", "Linguagens e Tecnologia"]

                if 'flex_matrix' not in data: data['flex_matrix'] = {}
                
                st.markdown("**7.1 Disciplinas que necessitam de adaptação**")
                c_h1, c_h2, c_h3 = st.columns([2, 1, 1])
                c_h1.write("**Disciplina**")
                c_h2.write("**Conteúdo?**")
                c_h3.write("**Metodologia?**")
                
                for disc in disciplinas_flex:
                    if disc not in data['flex_matrix']: data['flex_matrix'][disc] = {'conteudo': False, 'metodologia': False}
                    
                    c1, c2, c3 = st.columns([2, 1, 1])
                    c1.write(disc)
                    data['flex_matrix'][disc]['conteudo'] = c2.checkbox("Sim", key=f"flex_c_{aluno_id}_{disc}", value=data['flex_matrix'][disc]['conteudo'], disabled=is_monitor)
                    data['flex_matrix'][disc]['metodologia'] = c3.checkbox("Sim", key=f"flex_m_{aluno_id}_{disc}", value=data['flex_matrix'][disc]['metodologia'], disabled=is_monitor)

                st.divider()
                st.subheader("7.2 Plano de Ensino Anual")
                trimestres = ["1º Trimestre", "2º Trimestre", "3º Trimestre"]
                if 'plano_ensino_tri' not in data: data['plano_ensino_tri'] = {}

                for tri in trimestres:
                    st.markdown(f"### 🗓️ {tri}")
                    if tri not in data['plano_ensino_tri']: data['plano_ensino_tri'][tri] = {}
                    
                    for disc in disciplinas_flex:
                        with st.expander(f"{tri} - {disc}", expanded=False):
                            if disc not in data['plano_ensino_tri'][tri]:
                                data['plano_ensino_tri'][tri][disc] = {'obj': '', 'cont': '', 'met': ''}
                            
                            p_ref = data['plano_ensino_tri'][tri][disc]
                            p_ref['obj'] = st.text_area(f"Objetivos ({disc})", value=p_ref.get('obj', ''), key=f"obj_{aluno_id}_{tri}_{disc}", disabled=is_monitor)
                            p_ref['cont'] = st.text_area(f"Conteúdos ({disc})", value=p_ref.get('cont', ''), key=f"cont_{aluno_id}_{tri}_{disc}", disabled=is_monitor)
                            p_ref['met'] = st.text_area(f"Metodologia ({disc})", value=p_ref.get('met', ''), key=f"met_{aluno_id}_{tri}_{disc}", disabled=is_monitor)

                    # --- CORREÇÃO DA OBS/RECOMENDAÇÕES ---
                    obs_valor_banco = data['plano_ensino_tri'][tri].get('obs', '')
                    
                    obs_input = st.text_area(
                        f"Obs/Recomendações {tri}:", 
                        value=obs_valor_banco, 
                        key=f"obs_tri_{aluno_id}_{tri}", 
                        disabled=is_monitor
                    )
                    data['plano_ensino_tri'][tri]['obs'] = obs_input
                    st.markdown("---")

                st.markdown("Considerações finais:")
                data['plano_obs_geral'] = st.text_area("", value=data.get('plano_obs_geral', ''), key=f"obs_geral_{aluno_id}", disabled=is_monitor)

                st.markdown("---")
                if not is_monitor:
                    if st.form_submit_button("💾 Salvar Metas e Plano"):
                        save_student("PEI", data.get('nome'), data, "Metas e Plano")
        
        # --- ABA 7: ASSINATURAS (NOVO) ---
        with tabs[6]:
            st.subheader("Assinaturas Digitais")
            st.caption(f"Código Único do Documento: {data.get('doc_uuid', 'Não gerado ainda')}")
            
            # Identify required signers based on content
            required_roles = []
            if data.get('prof_poli'): required_roles.append({'role': 'Prof. Polivalente', 'name': data['prof_poli']})
            if data.get('prof_aee'): required_roles.append({'role': 'Prof. AEE', 'name': data['prof_aee']})
            if data.get('prof_arte'): required_roles.append({'role': 'Prof. Arte', 'name': data['prof_arte']})
            if data.get('prof_ef'): required_roles.append({'role': 'Prof. Ed. Física', 'name': data['prof_ef']})
            if data.get('prof_tec'): required_roles.append({'role': 'Prof. Tecnologia', 'name': data['prof_tec']})
            if data.get('gestor'): required_roles.append({'role': 'Gestor Escolar', 'name': data['gestor']})
            if data.get('coord'): required_roles.append({'role': 'Coordenação', 'name': data['coord']})
            
            # Show list of signatories
            if required_roles:
                st.markdown("##### Profissionais Citados no Documento")
                for r in required_roles:
                    st.write(f"- **{r['role']}:** {r['name']}")
            else:
                st.info("Nenhum profissional identificado automaticamente nos campos.")

            st.divider()
            
            # Current Signatures
            current_signatures = data.get('signatures', [])
            if current_signatures:
                st.success("✅ Documento assinado por:")
                for sig in current_signatures:
                    st.write(f"✍️ **{sig['name']}** ({sig.get('role', 'Profissional')}) em {sig['date']}")
            else:
                st.warning("Nenhuma assinatura registrada.")

            st.divider()
            
            # Signing Action
            user_name = st.session_state.get('usuario_nome', '')
            user_role_sys = "Monitor" if is_monitor else "Docente/Gestor"
            
            # Check if user matches any role
            match_role = "Profissional"
            is_cited = False
            for r in required_roles:
                if user_name.strip().lower() in r['name'].strip().lower():
                    is_cited = True
                    match_role = r['role']
                    break
            
            st.markdown(f"**Assinar como:** {user_name} ({match_role})")
            
            # Check if already signed
            already_signed = any(s['name'] == user_name for s in current_signatures)
            
            if already_signed:
                st.info("Você já assinou este documento.")
            else:
                if st.button("🖊️ Assinar Digitalmente"):
                    new_sig = {
                        "name": user_name,
                        "role": match_role,
                        "date": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
                        "hash": str(uuid.uuid4())
                    }
                    if 'signatures' not in data: data['signatures'] = []
                    data['signatures'].append(new_sig)
                    
                    # Salva apenas a assinatura
                    save_student("PEI", data.get('nome'), data, "Assinatura")
                    st.rerun()

        # --- ABA 8: EMISSÃO ---
        with tabs[7]:
            if not is_monitor:
                st.info("Antes de gerar o PDF, certifique-se de ter clicado em 'Salvar' nas abas anteriores.")
                if st.button("💾 SALVAR PEI COMPLETO", type="primary"): save_student("PEI", data['nome'], data, "Completo")
            else:
                st.info("Modo Visualização.")

            if st.button("👁️ GERAR PDF COMPLETO"):
                # Registrar ação de gerar PDF
                log_action(data.get('nome'), "Gerou PDF", "PEI Completo")
                
                pdf = OfficialPDF('L', 'mm', 'A4'); pdf.add_page(); pdf.set_margins(10, 10, 10)
                
                # SET SIGNATURE FOOTER
                pdf.set_signature_footer(data.get('signatures', []), data.get('doc_uuid', ''))
                
                # --- PÁGINA 1 ---
                if os.path.exists("logo_prefeitura.png"): pdf.image("logo_prefeitura.png", 10, 8, 25)
                if os.path.exists("logo_escola.png"): pdf.image("logo_escola.png", 252, 4, 37) 
                pdf.set_xy(0, 12); pdf.set_font("Arial", "", 14)
                pdf.cell(305, 6, clean_pdf_text("      PREFEITURA MUNICIPAL DE LIMEIRA"), 0, 1, 'C')
                pdf.ln(6); pdf.set_font("Arial", "B", 12)
                pdf.cell(297, 6, clean_pdf_text("CEIEF RAFAEL AFFONSO LEITE"), 0, 1, 'C')
                pdf.ln(8); pdf.set_font("Arial", "B", 14)
                pdf.cell(297, 8, clean_pdf_text("PLANO EDUCACIONAL ESPECIALIZADO - PEI"), 0, 1, 'C')
                
                # --- FOTO ---
                # Retângulo da foto: x=256, y=53, w=30, h=40
                if data.get('foto_base64'):
                    try:
                        img_data = base64.b64decode(data.get('foto_base64'))
                        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp_file:
                            tmp_file.write(img_data)
                            tmp_path = tmp_file.name
                        pdf.image(tmp_path, 256, 53, 30, 40)
                        os.unlink(tmp_path)
                        pdf.rect(256, 53, 30, 40) # Borda
                    except:
                        pdf.rect(256, 53, 30, 40)
                        pdf.set_xy(255.5, 70); pdf.set_font("Arial", "", 8); pdf.cell(30, 5, "Erro", 0, 0, 'C')
                else:
                    pdf.rect(256, 53, 30, 40)
                    pdf.set_xy(255.5, 70); pdf.set_font("Arial", "", 9); pdf.cell(30, 5, "FOTO", 0, 0, 'C')
                
                pdf.set_xy(10, 48); table_w = 240; h = 9 
                pdf.section_title("1. IDENTIFICAÇÃO DO ESTUDANTE", width=table_w) 
                pdf.set_font("Arial", "B", 12); pdf.cell(40, h, "Estudante:", 1); pdf.set_font("Arial", "", 12); pdf.cell(table_w-40, h, clean_pdf_text(data.get('nome', '')), 1, 1)
                pdf.set_font("Arial", "B", 12); pdf.cell(40, h, "Nascimento:", 1); pdf.set_font("Arial", "", 12); pdf.cell(40, h, clean_pdf_text(str(data.get('nasc', ''))), 1)
                pdf.set_font("Arial", "B", 12); pdf.cell(20, h, "Idade:", 1); pdf.set_font("Arial", "", 12); pdf.cell(20, h, clean_pdf_text(data.get('idade', '')), 1)
                pdf.set_font("Arial", "B", 12); pdf.cell(30, h, "Ano:", 1); pdf.set_font("Arial", "", 12); pdf.cell(table_w - 150, h, clean_pdf_text(data.get('ano_esc', '')), 1, 1)
                pdf.set_font("Arial", "B", 12); pdf.cell(40, h, "Mãe:", 1); pdf.set_font("Arial", "", 12); pdf.cell(table_w - 40, h, clean_pdf_text(data.get('mae', '')), 1, 1)
                pdf.set_font("Arial", "B", 12); pdf.cell(40, h, "Pai:", 1); pdf.set_font("Arial", "", 12); pdf.cell(table_w - 40, h, clean_pdf_text(data.get('pai', '')), 1, 1)
                pdf.set_font("Arial", "B", 12); pdf.cell(40, h, "Telefone:", 1); pdf.set_font("Arial", "", 12); pdf.cell(table_w - 40, h, clean_pdf_text(data.get('tel', '')), 1, 1)
                
                pdf.ln(5); full_w = 277 
                pdf.set_font("Arial", "B", 12); pdf.cell(full_w, h, "Docentes Responsáveis", 1, 1, 'L', 1)
                docs = [("Polivalente:", data.get('prof_poli')), ("Arte:", data.get('prof_arte')), ("Ed. Física:", data.get('prof_ef')), ("Tecnologia:", data.get('prof_tec')), ("AEE:", data.get('prof_aee')), ("Gestor:", data.get('gestor')), ("Coordenação:", data.get('coord')), ("Revisões:", data.get('revisoes'))]
                for l, v in docs:
                    pdf.set_font("Arial", "B", 12); pdf.cell(60, h, clean_pdf_text(l), 1); pdf.set_font("Arial", "", 12); pdf.cell(full_w-60, h, clean_pdf_text(v), 1, 1)

                # --- PÁGINA 2 ---
                pdf.add_page(); pdf.section_title("2. INFORMAÇÕES DE SAÚDE", width=0); h = 10
                pdf.set_font("Arial", "B", 12); pdf.cell(100, h, clean_pdf_text("O estudante tem diagnóstico conclusivo:"), 1, 0, 'L')
                status_sim = "[ X ]" if data.get('diag_status') == "Sim" else "[   ]"
                status_nao = "[ X ]" if data.get('diag_status') == "Não" else "[   ]"
                pdf.set_font("Arial", "", 12); pdf.cell(0, h, f"  {status_sim} Sim      {status_nao} Não", 1, 1, 'L')
                pdf.set_font("Arial", "B", 12); pdf.cell(40, h, "Data do Laudo:", 1, 0, 'L')
                pdf.set_font("Arial", "", 12); pdf.cell(60, h, clean_pdf_text(str(data.get('laudo_data', ''))), 1, 0, 'L')
                pdf.set_font("Arial", "B", 12); pdf.cell(40, h, "Médico Respons.:", 1, 0, 'L')
                pdf.set_font("Arial", "", 12); pdf.cell(0, h, clean_pdf_text(data.get('laudo_medico', '---')), 1, 1, 'L')

                pdf.ln(2); diag_list = data.get('diag_tipo', []); diag_ativos = []
                if "Deficiência" in diag_list and data.get('defic_txt'): diag_ativos.append(("Deficiência:", data.get('defic_txt')))
                if "Transtorno do Neurodesenvolvimento" in diag_list and data.get('neuro_txt'): diag_ativos.append(("Transtorno Neuro:", data.get('neuro_txt')))
                if "Transtornos Aprendizagem" in diag_list and data.get('aprend_txt'): diag_ativos.append(("Transt. Aprendizagem:", data.get('aprend_txt')))
                if "AH/SD" in diag_list: diag_ativos.append(("Destaque:", "Altas Habilidades / Superdotação"))
                if "Outros" in diag_list: diag_ativos.append(("Outros Diagnósticos:", "Conforme prontuário"))

                if diag_ativos:
                    for l_diag, t_diag in diag_ativos:
                        pdf.set_font("Arial", "B", 11); pdf.cell(60, h, clean_pdf_text(l_diag), "LTB", 0, 'L')
                        pdf.set_font("Arial", "", 11); pdf.cell(0, h, clean_pdf_text(t_diag), "RTB", 1, 'L')
                else: pdf.set_font("Arial", "I", 11); pdf.cell(0, h, "Nenhum diagnóstico selecionado.", 1, 1, 'C')

                pdf.ln(6); pdf.set_font("Arial", "B", 12); pdf.set_fill_color(245, 245, 245); pdf.cell(277, 10, "Terapias que realiza", 1, 1, 'C', 1)
                pdf.set_font("Arial", "B", 11); pdf.cell(80, 10, "Especialidades", 1, 0, 'L', 1); pdf.cell(0, 10, clean_pdf_text("Frequência e Horário de Atendimento"), 1, 1, 'L', 1)
                for esp in ["Psicologia", "Fonoaudiologia", "Terapia Ocupacional", "Psicopedagogia", "Fisioterapia", "Outros"]:
                    info = data.get('terapias', {}).get(esp, {'realiza': False, 'dias': [], 'horario': ''})
                    chk = "[ X ]" if info['realiza'] else "[   ]"
                    label_esp = f"  {chk} {esp}"
                    if esp == "Outros" and info.get('nome_custom'): label_esp = f"  {chk} Outros ({info['nome_custom']})"
                    pdf.set_font("Arial", "B", 11); pdf.cell(80, 12, clean_pdf_text(label_esp), 1, 0, 'L')
                    x_start = pdf.get_x(); y_start = pdf.get_y(); pdf.set_font("Arial", "", 10)
                    if info['realiza']:
                        pdf.set_xy(x_start + 5, y_start + 2); pdf.cell(0, 4, clean_pdf_text("Dias: " + ", ".join(info['dias'])), 0, 1)
                        pdf.set_x(x_start + 5); pdf.set_font("Arial", "B", 10); pdf.cell(16, 4, "Horário:", 0); pdf.set_font("Arial", "", 10); pdf.cell(0, 4, clean_pdf_text(info['horario']), 0, 1)
                    else:
                        pdf.set_xy(x_start + 5, y_start + 4); pdf.set_font("Arial", "I", 10); pdf.set_text_color(150, 0, 0)
                        pdf.cell(0, 4, "NÃO REALIZA ATENDIMENTO NESTA ESPECIALIDADE.", 0, 1); pdf.set_text_color(0, 0, 0)
                    pdf.set_xy(x_start, y_start); pdf.cell(0, 12, "", 1, 1)

                pdf.ln(5); pdf.set_font("Arial", "B", 12); pdf.cell(0, 10, "Medicação e Horários:", "LTR", 1, 'L', 1)
                pdf.set_font("Arial", "", 12); pdf.multi_cell(0, 8, clean_pdf_text(f"{data.get('med_nome', 'Não utiliza')}\nHorários: {data.get('med_hor', 'N/A')}"), "LRB")
                pdf.ln(5); pdf.set_font("Arial", "B", 12); pdf.cell(50, 8, clean_pdf_text("Médico Responsável:"), 1, 0); pdf.set_font("Arial", "", 12); pdf.cell(0, 8, clean_pdf_text(data.get('med_doc', 'N/A')), 1, 1)
                pdf.set_font("Arial", "B", 12); pdf.cell(0, 8, "Objetivo da medicação:", "LTR", 1, 'L', 1); pdf.set_font("Arial", "", 12); pdf.multi_cell(0, 8, clean_pdf_text(data.get('med_obj', 'Não informado.')), "LRB")
                pdf.ln(3); pdf.set_font("Arial", "B", 12); pdf.cell(0, 8, clean_pdf_text("Outras informações de saúde consideradas relevantes:"), "LTR", 1, 'L', 1)
                pdf.set_font("Arial", "", 12); pdf.multi_cell(0, 8, clean_pdf_text(data.get('saude_extra', 'Nenhuma informação adicional.')), "LRB")

                # --- 3. PROTOCOLO DE CONDUTA ---
                pdf.ln(5); pdf.section_title("3. PROTOCOLO DE CONDUTA", width=0); h = 8
                pdf.set_font("Arial", "B", 11); pdf.set_fill_color(245, 245, 245); pdf.cell(0, 8, "COMUNICAÇÃO, LOCOMOÇÃO E HIGIENE", 1, 1, 'C', 1)
                rows_cond = [
                    ("Como o estudante se comunica?", f"{data.get('com_tipo')} {data.get('com_alt_espec')}"),
                    ("Capaz de expressar necessidades, desejos e interesses?", f"{data.get('com_necessidades')} - {data.get('com_necessidades_espec')}"),
                    ("Atende quando é chamado?", f"{data.get('com_chamado')} - {data.get('com_chamado_espec')}"),
                    ("Responde a comandos simples?", f"{data.get('com_comandos')} - {data.get('com_comandos_espec')}"),
                    ("Possui mobilidade reduzida?", f"{data.get('loc_reduzida')} - {data.get('loc_reduzida_espec')}"),
                    ("Locomove-se pela casa e ambientes?", f"{data.get('loc_ambiente')} ({data.get('loc_ambiente_ajuda')}) - {data.get('loc_ambiente_espec')}"),
                    ("Utiliza o banheiro?", f"{data.get('hig_banheiro')} ({data.get('hig_banheiro_ajuda')}) - {data.get('hig_banheiro_espec')}"),
                    ("Escova os dentes?", f"{data.get('hig_dentes')} ({data.get('hig_dentes_ajuda')}) - {data.get('hig_dentes_espec')}")
                ]
                for l, v in rows_cond:
                    pdf.set_font("Arial", "B", 10); pdf.cell(95, h, clean_pdf_text(l), 1, 0, 'L'); pdf.set_font("Arial", "", 10); pdf.cell(0, h, clean_pdf_text(v), 1, 1, 'L')
                
                pdf.ln(4); pdf.set_font("Arial", "B", 11); pdf.set_fill_color(245, 245, 245); pdf.cell(0, 8, "COMPORTAMENTO E INTERESSES", 1, 1, 'C', 1)

                verbatims = [
                    ("Quais são os interesses do estudante?", data.get('beh_interesses')),
                    ("Quais objetos que gosta? Tem um objeto de apego?", data.get('beh_objetos_gosta')),
                    ("Quais objetos o estudante não gosta e/ou causam aversão?", data.get('beh_objetos_odeia')),
                    ("Gosta de toque, abraço, beijo?", data.get('beh_toque')),
                    ("O que o deixa calmo e relaxado?", data.get('beh_calmo')),
                    ("Quais atividades são mais prazerosas?", data.get('beh_atividades')),
                    ("Quais são os gatilhos já identificados para episódios de crise?", data.get('beh_gatilhos')),
                    ("Quando o estudante está em crise como normalmente se regula?", data.get('beh_crise_regula')),
                    ("O estudante costuma apresentar comportamentos desafiadores? Manejo?", data.get('beh_desafios')),
                    ("Tem restrições alimentares / Seletividade?", f"{data.get('beh_restricoes')} - {data.get('beh_restricoes_espec')}"),
                    ("Tem autonomia para tomar água e se alimentar?", f"{data.get('beh_autonomia_agua')} - {data.get('beh_autonomia_agua_espec')}"),
                    ("Outras informações julgadas pertinentes:", data.get('beh_pertinentes'))
                ]
                
                for l, v in verbatims:
                    if pdf.get_y() > 250: 
                        pdf.add_page()
                        
                    pdf.set_x(10)
                    pdf.set_font("Arial", "B", 10)
                    pdf.multi_cell(0, 7, clean_pdf_text(l), border="LTR", align='L', fill=True) 
                    
                    pdf.set_x(10)
                    pdf.set_font("Arial", "", 10)
                    pdf.multi_cell(0, 6, clean_pdf_text(v if v else "---"), border="LBR", align='L', fill=False)

                # --- 4. DESENVOLVIMENTO ESCOLAR ---
                pdf.ln(5); pdf.section_title("4. DESENVOLVIMENTO ESCOLAR", width=0); h = 8
                dev_rows = [
                    ("Permanece em sala e aula?", f"{data.get('dev_permanece')} - {data.get('dev_permanece_espec')}"),
                    ("Está integrado ao ambiente escolar?", f"{data.get('dev_integrado')} - {data.get('dev_integrado_espec')}"),
                    ("Locomove-se pela escola?", f"{data.get('dev_loc_escola')} - {data.get('dev_loc_escola_espec')}"),
                    ("Realiza tarefas escolares?", f"{data.get('dev_tarefas')} - {data.get('dev_tarefas_espec')}"),
                    ("Tem amigos?", f"{data.get('dev_amigos')} - {data.get('dev_amigos_espec')}"),
                    ("Tem um colega predileto?", f"{data.get('dev_colega_pref')}"),
                    ("Participa das atividades e interage em diferentes espaços?", f"{data.get('dev_participa')} - {data.get('dev_participa_espec')}")
                ]
                for l, v in dev_rows:
                    pdf.set_font("Arial", "B", 10); pdf.cell(100, h, clean_pdf_text(l), 1, 0, 'L'); pdf.set_font("Arial", "", 10); pdf.cell(0, h, clean_pdf_text(v), 1, 1, 'L')
                
                pdf.ln(2); pdf.set_font("Arial", "B", 10); pdf.cell(0, 7, clean_pdf_text("Envolvimento afetivo e social da turma com o estudante:"), "LTR", 1, 'L', 1)
                pdf.set_font("Arial", "", 10); pdf.multi_cell(0, 6, clean_pdf_text(data.get('dev_afetivo', '---')), "LRB")

                # --- 5. AVALIAÇÃO ACADÊMICA ---
                pdf.ln(5)
                if pdf.get_y() > 220: pdf.add_page()

                pdf.section_title("5. AVALIAÇÃO ACADÊMICA DO ESTUDANTE", width=0)
                pdf.ln(2)
                
                areas_aval = []
                
                if pei_level == "Fundamental":
                    areas_aval = [
                        ("LÍNGUA PORTUGUESA", data.get('aval_port')),
                        ("MATEMÁTICA", data.get('aval_mat')),
                        ("CONHECIMENTOS GERAIS", data.get('aval_con_gerais')),
                        ("ARTE - Artes Visuais", data.get('aval_arte_visuais')),
                        ("ARTE - Música", data.get('aval_arte_musica')),
                        ("ARTE - Teatro", data.get('aval_arte_teatro')),
                        ("ARTE - Dança", data.get('aval_arte_danca')),
                        ("EDUCAÇÃO FÍSICA - Habilidades Motoras", data.get('aval_ef_motoras')),
                        ("EDUCAÇÃO FÍSICA - Conhecimento Corporal", data.get('aval_ef_corp_conhec')),
                        ("EDUCAÇÃO FÍSICA - Exp. Corporais e Expressividade", data.get('aval_ef_exp')),
                        ("LINGUAGENS E TECNOLOGIA", data.get('aval_ling_tec'))
                    ]
                else: # Infantil
                    areas_aval = [
                        ("LINGUAGEM VERBAL", data.get('aval_ling_verbal')),
                        ("LINGUAGEM MATEMÁTICA", data.get('aval_ling_mat')),
                        ("INDÍVIDUO E SOCIEDADE", data.get('aval_ind_soc')),
                        ("ARTE - Artes Visuais", data.get('aval_arte_visuais')),
                        ("ARTE - Música", data.get('aval_arte_musica')),
                        ("ARTE - Teatro", data.get('aval_arte_teatro')),
                        ("EDUCAÇÃO FÍSICA - Jogos e Brincadeiras", data.get('aval_ef_jogos')),
                        ("EDUCAÇÃO FÍSICA - Ritmo e Expressividade", data.get('aval_ef_ritmo')),
                        ("EDUCAÇÃO FÍSICA - Conhecimento Corporal", data.get('aval_ef_corp')),
                        ("LINGUAGEM E TECNOLOGIAS", data.get('aval_ling_tec'))
                    ]
                
                for titulo, texto in areas_aval:
                    if pdf.get_y() > 230: pdf.add_page()
                    
                    pdf.set_font("Arial", "B", 10); pdf.set_fill_color(240, 240, 240)
                    pdf.cell(0, 7, clean_pdf_text(titulo), "LTR", 1, 'L', 1)
                    pdf.set_font("Arial", "", 10)
                    pdf.multi_cell(0, 6, clean_pdf_text(texto if texto else "---"), "LRB")
                    pdf.ln(2)

                # --- 6. METAS ---
                pdf.ln(5)
                if pdf.get_y() > 220: pdf.add_page()
                
                pdf.section_title("6. METAS ESPECÍFICAS PARA O ANO EM CURSO", width=0)
                pdf.ln(2)
                
                def print_meta_row(titulo, meta, estrategia):
                    if pdf.get_y() > 220: pdf.add_page()
                    pdf.set_font("Arial", "B", 11); pdf.set_fill_color(230, 230, 230)
                    pdf.cell(0, 8, clean_pdf_text(titulo), 1, 1, 'L', 1)
                    pdf.set_font("Arial", "B", 10); pdf.cell(0, 6, "Metas / Habilidades:", "LTR", 1)
                    pdf.set_font("Arial", "", 10); pdf.multi_cell(0, 5, clean_pdf_text(meta if meta else "---"), "LRB")
                    pdf.set_x(10); pdf.set_font("Arial", "B", 10); pdf.cell(0, 5, clean_pdf_text("Estratégias:"), "LTR", 1)
                    pdf.set_x(10); pdf.set_font("Arial", "", 10); pdf.multi_cell(0, 5, clean_pdf_text(estrategia if estrategia else "---"), "LRB")
                    pdf.ln(2)

                print_meta_row("Habilidades Sociais", data.get('meta_social_obj'), data.get('meta_social_est'))
                print_meta_row("Habilidades de Autocuidado e Vida Prática", data.get('meta_auto_obj'), data.get('meta_auto_est'))
                print_meta_row("Habilidades Acadêmicas", data.get('meta_acad_obj'), data.get('meta_acad_est'))

                # --- 7. FLEXIBILIZAÇÃO ---
                pdf.ln(5)
                if pdf.get_y() > 230: pdf.add_page()
                
                pdf.section_title("7. FLEXIBILIZAÇÃO CURRICULAR", width=0)
                pdf.ln(4)
                
                pdf.set_font("Arial", "B", 10)
                pdf.cell(0, 6, clean_pdf_text("7.1 DISCIPLINAS QUE NECESSITAM DE ADAPTAÇÃO"), 0, 1)
                pdf.ln(2)

                pdf.set_fill_color(240, 240, 240); pdf.set_font("Arial", "B", 9)
                pdf.cell(90, 8, "DISCIPLINA", 1, 0, 'C', 1)
                pdf.cell(90, 8, clean_pdf_text("CONTEÚDO"), 1, 0, 'C', 1)
                pdf.cell(0, 8, "METODOLOGIA", 1, 1, 'C', 1)

                if pei_level == "Fundamental":
                    disciplinas_flex = ["Língua Portuguesa", "Matemática", "História", "Geografia", "Ciências", "Arte", "Educação Física", "Linguagens e Tecnologia"]
                else:
                    disciplinas_flex = ["Linguagem Verbal", "Linguagem Matemática", "Indivíduo e Sociedade", "Arte", "Educação Física", "Linguagens e Tecnologia"]
                
                pdf.set_font("Arial", "", 10)
                for disc in disciplinas_flex:
                    vals = data.get('flex_matrix', {}).get(disc, {'conteudo': False, 'metodologia': False})
                    chk_c_sim = "[X] Sim  [  ] Não" if vals['conteudo'] else "[  ] Sim  [X] Não"
                    chk_m_sim = "[X] Sim  [  ] Não" if vals['metodologia'] else "[  ] Sim  [X] Não"
                    pdf.cell(90, 8, clean_pdf_text(f" {disc}"), 1, 0, 'L')
                    pdf.cell(90, 8, chk_c_sim, 1, 0, 'C')
                    pdf.cell(0, 8, chk_m_sim, 1, 1, 'C')

                # --- 7.2 PLANO DE ENSINO (TRIMESTRES) ---
                trimestres = ["1º Trimestre", "2º Trimestre", "3º Trimestre"]
                
                for tri in trimestres:
                    dados_tri = data.get('plano_ensino_tri', {}).get(tri, {})
                    has_content = False
                    if dados_tri.get('obs', '').strip(): has_content = True
                    for disc in disciplinas_flex:
                        d_dados = dados_tri.get(disc, {'obj': '', 'cont': '', 'met': ''})
                        if d_dados['obj'].strip() or d_dados['cont'].strip() or d_dados['met'].strip():
                            has_content = True; break
                    
                    if has_content:
                        pdf.ln(8)
                        if pdf.get_y() > 180: pdf.add_page()
                        pdf.set_font("Arial", "B", 12)
                        pdf.cell(0, 8, clean_pdf_text(f"7.2 PLANO DE ENSINO - {tri.upper()}"), 0, 1, 'L')
                        pdf.ln(2)

                        for disc in disciplinas_flex:
                            plan = dados_tri.get(disc, {'obj': '', 'cont': '', 'met': ''})
                            
                            if plan['obj'].strip() or plan['cont'].strip() or plan['met'].strip():
                                if pdf.get_y() > 180: pdf.add_page()
                            
                            pdf.set_font("Arial", "B", 10); pdf.set_fill_color(230, 230, 230)
                            pdf.cell(0, 7, clean_pdf_text(disc), 1, 1, 'L', 1)
                            
                            pdf.set_font("Arial", "B", 9); pdf.set_fill_color(250, 250, 250)
                            pdf.cell(0, 6, "Objetivos:", "LTR", 1, 'L', 1); pdf.set_font("Arial", "", 9)
                            pdf.multi_cell(0, 5, clean_pdf_text(plan['obj'] if plan['obj'] else "---"), "LRB")
                            
                            pdf.set_font("Arial", "B", 9)
                            pdf.cell(0, 6, clean_pdf_text("Conteúdos Específicos:"), "LTR", 1, 'L', 1); pdf.set_font("Arial", "", 9)
                            pdf.multi_cell(0, 5, clean_pdf_text(plan['cont'] if plan['cont'] else "---"), "LRB")
                            
                            pdf.set_font("Arial", "B", 9)
                            pdf.cell(0, 6, "Metodologia:", "LTR", 1, 'L', 1); pdf.set_font("Arial", "", 9)
                            pdf.multi_cell(0, 5, clean_pdf_text(plan['met'] if plan['met'] else "---"), "LRB")
                            pdf.ln(2)

                        if dados_tri.get('obs'):
                            if pdf.get_y() > 240: pdf.add_page()
                            pdf.ln(2)
                            pdf.set_font("Arial", "B", 10)
                            pdf.cell(0, 6, clean_pdf_text(f"Observações do {tri}:"), "LTR", 1, 'L')
                            pdf.set_font("Arial", "", 10)
                            pdf.multi_cell(0, 6, clean_pdf_text(dados_tri.get('obs')), "LRB")

                # --- OBSERVAÇÕES FINAIS ---
                if data.get('plano_obs_geral'):
                    pdf.ln(5)
                    if pdf.get_y() > 230: pdf.add_page()
                    pdf.set_font("Arial", "B", 10)
                    pdf.cell(0, 6, clean_pdf_text("Considerações e/ou recomendações finais:"), "LTR", 1, 'L')
                    pdf.set_font("Arial", "", 10)
                    pdf.multi_cell(0, 6, clean_pdf_text(data.get('plano_obs_geral')), "LRB")

                # --- ASSINATURAS ---
                pdf.ln(15)
                if pdf.get_y() > 230: pdf.add_page(); pdf.ln(15)
                pdf.set_font("Arial", "", 8)
                
                # Exibe assinaturas tradicionais (linhas)
                def draw_signature(x_pos, y_pos, nome, cargo):
                    pdf.line(x_pos, y_pos, x_pos + 70, y_pos)
                    pdf.set_xy(x_pos, y_pos + 2)
                    pdf.cell(70, 4, clean_pdf_text(nome if nome else "____________________"), 0, 2, 'C')
                    pdf.set_font("Arial", "B", 7)
                    pdf.cell(70, 3, clean_pdf_text(cargo), 0, 0, 'C')
                    pdf.set_font("Arial", "", 8)

                y = pdf.get_y()
                draw_signature(15, y, data.get('prof_poli', ''), "Prof. Polivalente / Regente")
                draw_signature(113, y, data.get('prof_arte', ''), "Prof. Arte")
                draw_signature(211, y, data.get('prof_ef', ''), "Prof. Ed. Física")
                
                pdf.ln(18)
                y = pdf.get_y()
                draw_signature(65, y, data.get('prof_aee', ''), "Prof. Ed. Especial (AEE)")
                draw_signature(162, y, data.get('prof_tec', ''), "Prof. Linguagens e Tec.")
                
                pdf.ln(18)
                y = pdf.get_y()
                draw_signature(65, y, data.get('coord', ''), "Coordenador Pedagógico")
                draw_signature(162, y, data.get('gestor', ''), "Gestor Escolar")

                st.session_state.pdf_bytes = get_pdf_bytes(pdf)
                st.rerun()

            if 'pdf_bytes' in st.session_state:
                st.download_button("📥 BAIXAR PEI COMPLETO", st.session_state.pdf_bytes, f"PEI_{data.get('nome','aluno')}.pdf", "application/pdf", type="primary")

        # --- ABA 9: HISTÓRICO ---
        with tabs[8]:
            st.subheader("Histórico de Atividades")
            st.caption("Registro de alterações, salvamentos e geração de documentos.")
            
            df_hist = safe_read("Historico", ["Data_Hora", "Aluno", "Usuario", "Acao", "Detalhes"])
            
            if not df_hist.empty and data.get('nome'):
                # Filtrar pelo aluno atual
                student_hist = df_hist[df_hist["Aluno"] == data.get('nome')]
                
                if not student_hist.empty:
                    # Ordenar por data (mais recente primeiro)
                    student_hist = student_hist.iloc[::-1]
                    st.dataframe(student_hist, use_container_width=True, hide_index=True)
                else:
                    st.info("Nenhum histórico encontrado para este aluno.")
            else:
                st.info("O histórico está vazio ou aluno não selecionado.")

   
    # --- PDI - PLANO DE DESENVOLVIMENTO INDIVIDUAL (ATUALIZADO) ---
    if doc_mode == "PDI":
        st.markdown(f"""<div class="header-box"><div class="header-title">PDI - Plano de Desenvolvimento Individual</div></div>""", unsafe_allow_html=True)
        st.markdown("""<style>div[data-testid="stFormSubmitButton"] > button {width: 100%; background-color: #dcfce7; color: #166534; border: 1px solid #166534;}</style>""", unsafe_allow_html=True)

        data_pdi = st.session_state.data_pdi
        data_case = st.session_state.get('data_case', {})
        
        # --- DEFINIÇÃO DOS CHECKLISTS ESPECÍFICOS (ATUALIZADO) ---
        checklist_options = {
            "sis_monetario": ["Não reconhece o sistema monetário.", "Reconhece o sistema monetário.", "Atribui poder de compra."],
            "brincar_funcional": ["Sim", "Não"],
            "brincar_explora": ["Explora os brinquedos espontaneamente.", "Necessita de modelo / direcionamento para explorar os brinquedos."],
            "brincar_criativa": ["Sim", "Não"],
            "brincar_funcoes": ["Sim", "Não"],
            "memoria_curto": [
                "Não realiza jogo de memória.",
                "Realiza jogo de memória com _____ peças.",
                "Relembra sequência de até ______ cores.",
                "Relembra sequência de até ______ números.",
                "Relembra sequência de até ______ objetos.",
                "Relembra sentenças simples.",
                "Relembra sentenças complexas."
            ],
            "memoria_episodica": [
                "Relembra fatos do cotidiano.",
                "Necessita de ajuda para relembrar fatos do cotidiano.",
                "Não relembra fatos do cotidiano."
            ],
            "memoria_semantica": [
                "Relaciona o significado da palavra com o objeto.",
                "Necessita de apoio para relacionar o significado da palavra com o objeto.",
                "Não relaciona."
            ],
            "atencao_sustentada": [
                "Mantém atenção por longo período de tempo.",
                "Mantém atenção por longo período de tempo com apoio.",
                "Não mantém atenção por longo período de tempo."
            ],
            "atencao_dividida": [
                "Mantém atenção em dois estímulos diferentes.",
                "Mantém atenção em dois estímulos diferentes em algumas situações.",
                "Não mantém atenção em dois estímulos diferentes."
            ],
            "atencao_seletiva": [
                "Mantém atenção na tarefa ignorando estímulos externos.",
                "Mantém atenção na tarefa ignorando estímulos externos com apoio.",
                "Não mantém atenção na tarefa com a presença de outros estímulos."
            ],
            "vm_desenho": [
                "Não reproduz.",
                "Reproduz diferente do modelo.",
                "Reproduz semelhante ao modelo."
            ],
            "vm_limite_folha": ["Sim", "Não", "Com apoio"],
            "vm_limite_pintura": ["Sim", "Não", "Com apoio"],
            "vm_rasgar": ["Sim", "Não", "Com apoio"],
            "vm_tesoura": [
                "Não realiza recorte com tesoura.",
                "Utiliza tesoura com dificuldade.",
                "Utiliza tesoura de modo satisfatório."
            ],
            "vm_cola": ["Não consegue.", "Usa muita cola.", "Adequado."],
            "vm_encaixe": [
                "Não realiza.",
                "Realiza encaixe só com apoio.",
                "Realiza encaixe simples.",
                "Realiza encaixe mais complexos.",
                "Outros: _____________________"
            ],
            "vm_reproducao": [
                "Não reproduz.",
                "Reproduz diferente do modelo.",
                "Reproduz semelhante ao modelo."
            ],
            "vm_quebra_cabeca": [
                "Não realiza.",
                "Realiza por tentativa e erro.",
                "Realiza por visualização."
            ],
            "mf_punho": ["Não apresenta.", "Apresenta em alguns momentos.", "Apresenta satisfatoriamente."],
            "mf_pinca": ["Não apresenta.", "Apresenta em alguns momentos.", "Apresenta satisfatoriamente."],
            "mf_preensao": [
                "Segura o lápis/pincel com autonomia.",
                "Necessita de apoio para segurar o lápis/pincel.",
                "Apresenta preensão palmar.",
                "Apresenta preensão digital.",
                "Manuseia massinha/argila.",
                "Outros: _____________________"
            ],
            "mg_tronco_sentado": ["Sim", "Não"],
            "mg_tronco_pe": ["Sim", "Não"],
            "mg_postura_opts": ["Cabeça muito próxima à folha.", "Outros: _____________________"],
            "mg_mao_apoio": ["Não utiliza.", "Utiliza quando necessário.", "Outros: _____________________"],
            "mg_locomocao": [
                "Atualmente acamado.",
                "Faz uso de cadeira de rodas.",
                "Possui prótese/órtese",
                "Faz uso de andador.",
                "Faz uso de bengala.",
                "Se arrasta/engatinha.",
                "Apresenta marcha com dificuldade.",
                "Apresenta marcha adequada.",
                "Outros: _____________________"
            ],
            "mg_equilibrio": [
                "Anda sobre linha reta.",
                "Anda sobre linha sinuosa.",
                "Corre em linha reta.",
                "Corre em linha sinuosa.",
                "Equilibra-se em um pé só.",
                "Realiza posição do avião.",
                "Realiza saltos com os dois pés.",
                "Realiza saltos com um pé só.",
                "Lança bola com as mãos.",
                "Chuta bola com os pés.",
                "Necessita de apoio para subir escadas.",
                "Sobe escadas com autonomia.",
                "Outros: _____________________"
            ],
            "ec_imagem": ["Sim", "Não"],
            "ec_partes": [
                "Não identifica ou nomeia as partes do corpo.",
                "Só identifica partes gerais.",
                "Só identifica e nomeia partes gerais.",
                "Identifica partes gerais e específicas.",
                "Identifica e nomeia partes gerais e específicas."
            ],
            "ec_funcoes": ["Sim", "Não"],
            "ec_imitar": ["Sim", "Não"],
            "ec_desenho": ["Sim", "Não"],
            "ec_dominancia": ["Direita", "Esquerda", "Sem definição"],
            "ec_identifica": ["Direita", "Esquerda"],
            "ec_dois_lados": ["Sim", "Não"],
            "avd_alimentacao": ["É independente.", "Necessita de apoio parcial.", "Necessita de apoio total."],
            "avd_higiene": ["Usa sonda.", "Usa bolsa de colostomia.", "Usa fraldas.", "Necessita de apoio total.", "Necessita de apoio parcial.", "É independente."],
            "avd_objetos": ["Faz uso funcional.", "Necessita de apoio parcial.", "Necessita de total apoio."],
            "avd_locomocao": ["Se locomove com independência.", "Necessita de apoio para locomoção."],
            "ps_interacao": [
                "Adequada com as crianças.",
                "Adequada com adultos.",
                "Satisfatória.",
                "Inadequada.",
                "Outros: _____________________"
            ],
            "ps_iniciativa_dialogo": [
                "Não.",
                "Sim, mas reduzida.",
                "Adequada.",
                "Outros: _____________________"
            ],
            "ps_iniciativa_ativ": ["Não.", "Sim, mas reduzida.", "Adequada."],
            "ps_comps": [
                "Timidez", "Insegurança", "Agressividade", "Resistência", "Apatia", "Respeita regras e limites", "Chora facilmente", "Impulsividade",
                "Agitação", "Ansiedade", "Cooperação", "Desinteresse", "Comportamento infantilizado", "Tiques", "Contato visual"
            ],
            "vp_nome": ["Não.", "Sim, mas só o prenome.", "Sim, o nome completo."],
            "vp_sim_nao": ["Sim", "Não"],
            "vp_niver": ["Sim", "Não", "Só o mês"],
            "ling_verbal": [
                "Não faz uso de palavras para se comunicar.",
                "Faz uso de palavras para se comunicar.",
                "Apresenta trocas fonéticas orais.",
                "Consegue expressar e explicar seus pensamentos ideias e desejos.",
                "Faz relatos do cotidiano numa sequência lógica.",
                "Estabelece diálogo com troca de turno.",
                "Inventa frases ou histórias.",
                "Descreve cenas com sentido.",
                "Reconta histórias com sentido e sequência lógica.",
                "Outros: _____________________"
            ],
            "ling_compreensiva": [
                "Compreende e processa informações orais simples.",
                "Compreende e processa informações orais complexas.",
                "Não compreende e não processa informações orais.",
                "Compreende informações textuais.",
                "Compreende o contexto de uma história."
            ],
            "ling_gestual": ["Utiliza apenas linguagem gestual.", "Utiliza linguagem gestual parcialmente.", "Não utiliza linguagem gestual."],
            "ling_ecolalia": ["Não fala de forma ecolálica.", "Apresenta ecolalia.", "Apresenta ecolalia em alguns momentos."],
            "ling_escrita": [
                "Não escreve convencionalmente.",
                "Não distingue desenho, letras e números",
                "Distingue desenho, letras e números",
                "Escreva letras de forma aleatórias.",
                "Identifica e nomeia as letras.",
                "Escreve seu nome.",
                "Relaciona som/grafia.",
                "Escreve apenas palavras canônicas.",
                "Escreve palavras não-canônicas.",
                "Apresenta dificuldades na segmentação.",
                "Escreve frases simples.",
                "Escreve textos simples.",
                "Apresenta desorganização textual.",
                "Apresenta trocas fonéticas."
            ],
            "ling_leitura": [
                "Não realiza leitura.",
                "Domina sequência alfabética.",
                "Identifica seu nome.",
                "Realiza leitura apenas de palavras canônicas.",
                "Realiza leitura de palavras canônicas e não-canônicas.",
                "Realiza leitura de frases e textos com dificuldade.",
                "Realiza leitura de frases e textos com fluência.",
                "Não compreende o que lê.",
                "Compreende o que lê com apoio.",
                "Compreende o que lê.",
                "Outros: _____________________"
            ],
            "libras_aparelho": ["OD", "OE"],
            "libras_implante": ["OD", "OE"],
            "libras_com": ["Não", "Básico", "Fluente"],
            "libras_compreende": ["Sim", "Não"],
            "braille_esc": ["Com autonomia.", "Com apoio.", "Com dificuldade."],
            "braille_leit": ["Com autonomia.", "Com apoio.", "Com dificuldade."],
            "com_alt": [
                "Comunica-se através de apontamentos.",
                "Comunica-se através do piscar dos olhos.",
                "Comunica-se através de comunicação alternativa.",
                "Compreende e processa informações através de comunicação alternativa.",
                "Outros: _____________________"
            ]
        }

        # Estrutura para Objetivos Específicos
        objectives_structure = {
            "DESENVOLVIMENTO COGNITIVO": {
                "PERCEPÇÃO": ["Visual", "Auditiva", "Tátil", "Espacial / Lateralidade", "Temporal / Ritmo / Sequência lógica"],
                "RACIOCÍNIO LÓGICO": ["Correspondência", "Comparação", "Classificação", "Sequenciação", "Seriação", "Inclusão", "Conservação", "Resolução de situações-problema"],
                "OUTROS": ["Sistema Monetário", "Capacidade de Brincar", "Memória", "Atenção"]
            },
            "DESENVOLVIMENTO MOTOR": {
                "COORDENAÇÃO MOTORA FINA": ["Estabilidade de punho", "Movimento de pinça", "Preensão"],
                "COORDENAÇÃO MOTORA GLOBAL": ["Postura", "Mão de apoio", "Locomoção", "Equilíbrio"],
                "COORDENAÇÃO VISO-MOTORA": ["Desenho", "Limites da folha e desenho", "Recorte", "Uso de cola", "Encaixes", "Reprodução de figuras", "Quebra-cabeça"],
                "ESQUEMA CORPORAL": ["Imagem corporal", "Partes do corpo e funções", "Lateralidade"],
                "AVD": ["Alimentação", "Higiene", "Uso funcional dos objetos", "Locomoção na escola"]
            },
            "FUNÇÃO PESSOAL E SOCIAL": {
                "GERAL": ["Interação", "Iniciativa", "Comportamento", "Vida Prática"]
            },
            "LINGUAGEM": {
                "GERAL": ["Verbal", "Compreensiva", "Gestual", "Ecolalia", "Escrita", "Leitura", "Libras / Braille / CA"]
            }
        }

        # Tabs de Navegação
        tabs = st.tabs([
            "Item 2: Plano AEE",
            "Item 3: Avaliação Pedagógica",
            "Item 4: Objetivos a Atingir",
            "PDF Final",
            "Histórico"
        ])
        
        st.info("ℹ️ Os dados de **Identificação**, **Família**, **Histórico** e **Avaliação Geral** são importados automaticamente do módulo **Estudo de Caso** (Item 1).")

        # --- ABA 1: PLANO AEE ---
        with tabs[0]:
            with st.form("pdi_plano_aee"):
                st.header("2. Plano de AEE & Ações Necessárias")
                
                st.subheader("2.1 Avaliação Pedagógica Inicial")
                data_pdi['potencialidades'] = st.text_area("Potencialidades do Estudante", value=data_pdi.get('potencialidades', ''), disabled=is_monitor)
                data_pdi['areas_interesse'] = st.text_area("Áreas de Interesse", value=data_pdi.get('areas_interesse', ''), disabled=is_monitor)
                
                st.divider()
                st.subheader("2.2 Ações Necessárias")
                data_pdi['acao_escola'] = st.text_area("Âmbito Escola", value=data_pdi.get('acao_escola', ''), disabled=is_monitor)
                data_pdi['acao_sala'] = st.text_area("Âmbito Sala de Aula", value=data_pdi.get('acao_sala', ''), disabled=is_monitor)
                data_pdi['acao_familia'] = st.text_area("Âmbito Família", value=data_pdi.get('acao_familia', ''), disabled=is_monitor)
                data_pdi['acao_saude'] = st.text_area("Âmbito Saúde", value=data_pdi.get('acao_saude', ''), disabled=is_monitor)

                st.divider()
                st.subheader("2.3 Organização do AEE")
                
                data_pdi['aee_tempo'] = st.text_input("Tempo de Atendimento", value=data_pdi.get('aee_tempo', '50 minutos'), disabled=is_monitor)
                
                data_pdi['aee_tipo'] = st.radio("Local/Modalidade", ["Sala de Recursos Multifuncionais", "Trabalho Colaborativo", "Itinerante", "Domiciliar"], horizontal=True, disabled=is_monitor)
                data_pdi['aee_comp'] = st.radio("Composição", ["Individual", "Grupal"], horizontal=True, disabled=is_monitor)

                if st.form_submit_button("💾 Salvar Plano AEE"):
                    save_student("PDI", data_pdi.get('nome'), data_pdi, "Plano AEE")

        # --- ABA 2: AVALIAÇÃO PEDAGÓGICA (CHECKLISTS) ---
        with tabs[1]:
            st.header("3. Objetivos e Metas (Avaliação Pedagógica)")
            st.caption("Preencha a situação do aluno: Diagnóstico (Multiseleção), Percurso (Texto) e Final (Texto).")

            def render_evolution_row(label, key_base, option_list):
                """Helper para renderizar: Diag (Multiselect), Proc (Text), Final (Text)"""
                st.markdown(f"**{label}**")
                c1, c2, c3 = st.columns(3)
                
                # Retrieve saved values
                v_diag = data_pdi.get(f"{key_base}_diag", [])
                if not isinstance(v_diag, list): v_diag = [] 

                # Filter defaults to ensure they exist in option_list (avoids StreamlitAPIException on schema change)
                v_diag = [x for x in v_diag if x in option_list]

                v_proc = data_pdi.get(f"{key_base}_proc", "")
                v_final = data_pdi.get(f"{key_base}_final", "")

                data_pdi[f"{key_base}_diag"] = c1.multiselect("Resultados da Avaliação Diagnóstica", option_list, default=v_diag, key=f"d_{key_base}", disabled=is_monitor)
                data_pdi[f"{key_base}_proc"] = c2.text_area("Resultados da Avaliação de Percurso", value=v_proc, key=f"p_{key_base}", height=120, disabled=is_monitor)
                data_pdi[f"{key_base}_final"] = c3.text_area("Resultados da Avaliação Final", value=v_final, key=f"f_{key_base}", height=120, disabled=is_monitor)
                st.divider()

            def render_text_grid(label, key_base):
                st.markdown(f"**{label}**")
                c1, c2, c3 = st.columns(3)
                data_pdi[f"{key_base}_diag"] = c1.text_area("Resultados da Avaliação Diagnóstica", value=data_pdi.get(f"{key_base}_diag", ""), key=f"d_{key_base}", height=120, disabled=is_monitor)
                data_pdi[f"{key_base}_proc"] = c2.text_area("Resultados da Avaliação de Percurso", value=data_pdi.get(f"{key_base}_proc", ""), key=f"p_{key_base}", height=120, disabled=is_monitor)
                data_pdi[f"{key_base}_final"] = c3.text_area("Resultados da Avaliação Final", value=data_pdi.get(f"{key_base}_final", ""), key=f"f_{key_base}", height=120, disabled=is_monitor)
                st.divider()

            with st.form("pdi_avaliacao_form"):
                
                # 3.1 DESENVOLVIMENTO COGNITIVO
                st.subheader("3.1 DESENVOLVIMENTO COGNITIVO")
                
                with st.expander("3.1.1 Percepção e 3.1.2 Raciocínio (Descritivo)", expanded=False):
                    items_desc = ["Visual", "Auditiva", "Tátil", "Espacial", "Temporal", "Correspondência", "Comparação", "Classificação", "Sequenciação", "Seriação", "Inclusão", "Conservação", "Resolução de Problemas"]
                    for it in items_desc:
                        render_text_grid(it, f"cog_{it.lower()}")

                with st.expander("3.1.3 Sistema Monetário", expanded=False):
                    render_evolution_row("Sistema Monetário", "sis_monetario", checklist_options["sis_monetario"])

                with st.expander("3.1.4 Capacidade de Brincar", expanded=False):
                    render_evolution_row("Uso funcional?", "brincar_funcional", checklist_options["brincar_funcional"])
                    render_evolution_row("Exploração", "brincar_explora", checklist_options["brincar_explora"])
                    render_evolution_row("Criação/Simbolismo", "brincar_criativa", checklist_options["brincar_criativa"])
                    render_evolution_row("Atribui funções", "brincar_funcoes", checklist_options["brincar_funcoes"])
                    data_pdi['brincar_obs'] = st.text_input("Observações Brincar", value=data_pdi.get('brincar_obs',''), disabled=is_monitor)

                with st.expander("3.1.5 e 3.1.6 Memória", expanded=False):
                    render_evolution_row("Curto Prazo", "mem_curto", checklist_options["memoria_curto"])
                    render_evolution_row("Longo Prazo - Episódica", "mem_episodica", checklist_options["memoria_episodica"])
                    render_evolution_row("Longo Prazo - Semântica", "mem_semantica", checklist_options["memoria_semantica"])
                    data_pdi['memoria_obs'] = st.text_input("Observações Memória", value=data_pdi.get('memoria_obs',''), disabled=is_monitor)

                with st.expander("3.1.7 Atenção", expanded=False):
                    render_evolution_row("Sustentada", "at_sust", checklist_options["atencao_sustentada"])
                    render_evolution_row("Dividida", "at_div", checklist_options["atencao_dividida"])
                    render_evolution_row("Seletiva", "at_sel", checklist_options["atencao_seletiva"])
                    data_pdi['atencao_obs'] = st.text_input("Observações Atenção", value=data_pdi.get('atencao_obs',''), disabled=is_monitor)

                with st.expander("3.1.8 Coordenação Viso-Motora", expanded=False):
                    render_evolution_row("Desenho", "vm_desenho", checklist_options["vm_desenho"])
                    render_evolution_row("Limites Folha", "vm_l_folha", checklist_options["vm_limite_folha"])
                    render_evolution_row("Limites Pintura", "vm_l_pint", checklist_options["vm_limite_pintura"])
                    render_evolution_row("Recorte (Rasgar)", "vm_rasgar", checklist_options["vm_rasgar"])
                    render_evolution_row("Uso Tesoura", "vm_tesoura", checklist_options["vm_tesoura"])
                    render_evolution_row("Uso Cola", "vm_cola", checklist_options["vm_cola"])
                    render_evolution_row("Encaixes", "vm_encaixe", checklist_options["vm_encaixe"])
                    render_evolution_row("Reprodução Figuras", "vm_reproducao", checklist_options["vm_reproducao"])
                    render_evolution_row("Quebra-Cabeça", "vm_quebra_cabeca", checklist_options["vm_quebra_cabeca"])
                    data_pdi['vm_obs'] = st.text_input("Observações Viso-Motora", value=data_pdi.get('vm_obs',''), disabled=is_monitor)

                # 3.2 DESENVOLVIMENTO MOTOR
                st.subheader("3.2 DESENVOLVIMENTO MOTOR")
                with st.expander("3.2.1 Coordenação Fina", expanded=False):
                    render_evolution_row("Estabilidade Punho", "mf_punho", checklist_options["mf_punho"])
                    render_evolution_row("Pinça", "mf_pinca", checklist_options["mf_pinca"])
                    render_evolution_row("Preensão", "mf_preensao", checklist_options["mf_preensao"])
                    data_pdi['mf_obs'] = st.text_input("Observações Motora Fina", value=data_pdi.get('mf_obs',''), disabled=is_monitor)

                with st.expander("3.2.2 Coordenação Global", expanded=False):
                    render_evolution_row("Postura (Sentado)", "mg_sentado", checklist_options["mg_tronco_sentado"])
                    render_evolution_row("Postura (Pé)", "mg_pe", checklist_options["mg_tronco_pe"])
                    render_evolution_row("Outros (Postura)", "mg_postura_opts", checklist_options["mg_postura_opts"])
                    render_evolution_row("Mão de Apoio", "mg_mao_apoio", checklist_options["mg_mao_apoio"])
                    render_evolution_row("Locomoção", "mg_loc", checklist_options["mg_locomocao"])
                    render_evolution_row("Equilíbrio", "mg_eq", checklist_options["mg_equilibrio"])
                    data_pdi['mg_obs'] = st.text_input("Observações Motor Global", value=data_pdi.get('mg_obs',''), disabled=is_monitor)

                with st.expander("3.2.3 Esquema Corporal", expanded=False):
                    render_evolution_row("Imagem Corporal", "ec_img", checklist_options["ec_imagem"])
                    render_evolution_row("Identificação Partes", "ec_partes", checklist_options["ec_partes"])
                    render_evolution_row("Funções Partes", "ec_func", checklist_options["ec_funcoes"])
                    render_evolution_row("Imitação", "ec_imit", checklist_options["ec_imitar"])
                    render_evolution_row("Desenho Humano", "ec_des", checklist_options["ec_desenho"])
                    render_evolution_row("Dominância Lateral", "ec_lat", checklist_options["ec_dominancia"])
                    render_evolution_row("Identifica Lateralidade", "ec_id_lat", checklist_options["ec_identifica"])
                    render_evolution_row("Uso dois lados", "ec_dois", checklist_options["ec_dois_lados"])
                    data_pdi['ec_obs'] = st.text_input("Observações Esquema Corporal", value=data_pdi.get('ec_obs',''), disabled=is_monitor)

                with st.expander("3.2.4 Autonomia / AVD", expanded=False):
                    render_evolution_row("Alimentação", "avd_alim", checklist_options["avd_alimentacao"])
                    render_evolution_row("Higiene", "avd_hig", checklist_options["avd_higiene"])
                    render_evolution_row("Uso Objetos", "avd_obj", checklist_options["avd_objetos"])
                    render_evolution_row("Locomoção Escola", "avd_loc", checklist_options["avd_locomocao"])
                    data_pdi['avd_obs'] = st.text_input("Observações AVD", value=data_pdi.get('avd_obs',''), disabled=is_monitor)

                # 3.3 PESSOAL SOCIAL
                st.subheader("3.3 FUNÇÃO PESSOAL E SOCIAL")
                with st.expander("3.3.1 Interação e Comportamento", expanded=False):
                    render_evolution_row("Interação", "ps_int", checklist_options["ps_interacao"])
                    render_evolution_row("Iniciativa Diálogo", "ps_ini_d", checklist_options["ps_iniciativa_dialogo"])
                    render_evolution_row("Iniciativa Atividade", "ps_ini_a", checklist_options["ps_iniciativa_ativ"])
                    
                    st.markdown("**Comportamentos Apresentados:**")
                    # Filter defaults for comps
                    v_comps = data_pdi.get('ps_comps', [])
                    if not isinstance(v_comps, list): v_comps = []
                    v_comps = [x for x in v_comps if x in checklist_options["ps_comps"]]
                    
                    data_pdi['ps_comps'] = st.multiselect("Selecione:", checklist_options["ps_comps"], default=v_comps, disabled=is_monitor)
                    
                    st.markdown("**Vida Prática:**")
                    render_evolution_row("Sabe Nome?", "vp_nome", checklist_options["vp_nome"])
                    render_evolution_row("Sabe Idade?", "vp_idade", checklist_options["vp_sim_nao"])
                    render_evolution_row("Sabe Aniversário?", "vp_niver", checklist_options["vp_niver"])
                    render_evolution_row("Nomeia Familiares?", "vp_fam", checklist_options["vp_sim_nao"])
                    render_evolution_row("Nomeia Profs?", "vp_prof", checklist_options["vp_sim_nao"])
                    render_evolution_row("Nomeia Escola?", "vp_escola", checklist_options["vp_sim_nao"])
                    render_evolution_row("Sabe Ano Escolar?", "vp_ano_esc", checklist_options["vp_sim_nao"])
                    render_evolution_row("Sabe Endereço?", "vp_end", checklist_options["vp_sim_nao"])
                    data_pdi['vp_outros'] = st.text_input("Outros (Vida Prática)", value=data_pdi.get('vp_outros',''), disabled=is_monitor)

                # 3.4 LINGUAGEM
                st.subheader("3.4 LINGUAGEM")
                with st.expander("3.4.1 Linguagem", expanded=False):
                    render_evolution_row("Verbal", "ling_verb", checklist_options["ling_verbal"])
                    render_evolution_row("Compreensiva", "ling_comp", checklist_options["ling_compreensiva"])
                    render_evolution_row("Gestual", "ling_gest", checklist_options["ling_gestual"])
                    render_evolution_row("Ecolalia", "ling_eco", checklist_options["ling_ecolalia"])
                    render_evolution_row("Escrita", "ling_esc", checklist_options["ling_escrita"])
                    render_evolution_row("Leitura", "ling_leit", checklist_options["ling_leitura"])

                with st.expander("3.4.2 LIBRAS e Com. Alternativa", expanded=False):
                    render_evolution_row("Aparelho Auditivo", "lib_ap", checklist_options["libras_aparelho"])
                    render_evolution_row("Implante Coclear", "lib_imp", checklist_options["libras_implante"])
                    render_evolution_row("Comunicação LIBRAS", "lib_com", checklist_options["libras_com"])
                    render_evolution_row("Compreensão LIBRAS", "lib_comp", checklist_options["libras_compreende"])
                    render_evolution_row("Escrita Braille", "braille_esc", checklist_options["braille_esc"])
                    render_evolution_row("Leitura Braille", "braille_leit", checklist_options["braille_leit"])
                    data_pdi['libras_outros'] = st.text_input("Outros (Libras/Braille)", value=data_pdi.get('libras_outros',''), disabled=is_monitor)
                    render_evolution_row("Com. Alternativa", "ca_uso", checklist_options["com_alt"])

                if st.form_submit_button("💾 Salvar Avaliação Pedagógica"):
                    save_student("PDI", data_pdi.get('nome'), data_pdi, "Avaliação Pedagógica")

        # --- ABA 3: OBJETIVOS E METAS (ITEM 4 - DETALHADO) ---
        with tabs[2]:
            st.header("4. Objetivos a serem Atingidos")
            st.info("Descreva os objetivos específicos para cada área de desenvolvimento.")
            
            with st.form("pdi_objetivos_detalhado"):
                if 'goals_specific' not in data_pdi: data_pdi['goals_specific'] = {}

                for category, subcats in objectives_structure.items():
                    with st.expander(f"📍 {category}", expanded=False):
                        for subcat_name, items_list in subcats.items():
                            st.markdown(f"**{subcat_name}**")
                            for item in items_list:
                                item_key = f"goal_{category}_{subcat_name}_{item}".replace(" ", "_").lower()
                                val = data_pdi['goals_specific'].get(item_key, "")
                                data_pdi['goals_specific'][item_key] = st.text_input(f"{item}:", value=val, disabled=is_monitor)
                            st.divider()

                if st.form_submit_button("💾 Salvar Objetivos"):
                    save_student("PDI", data_pdi.get('nome'), data_pdi, "Objetivos Detalhados")

        # --- ABA 4: PDF ---
        with tabs[3]:
            st.subheader("Finalização")
            
            # Assinaturas
            current_signatures = data_pdi.get('signatures', [])
            if current_signatures:
                st.success(f"Assinado por: {', '.join([s['name'] for s in current_signatures])}")
            
            if st.button("🖊️ Assinar como Prof. AEE"):
                new_sig = {"name": st.session_state.get('usuario_nome',''), "date": datetime.now().strftime("%d/%m/%Y"), "role": "Professor AEE"}
                if 'signatures' not in data_pdi: data_pdi['signatures'] = []
                data_pdi['signatures'].append(new_sig)
                save_student("PDI", data_pdi.get('nome'), data_pdi, "Assinatura")
                st.rerun()
            
            st.divider()
            
            if st.button("👁️ GERAR PDI COMPLETO (PDF)"):
                log_action(data_pdi.get('nome'), "Gerou PDF", "PDI Completo")
                
                pdf = OfficialPDF('P', 'mm', 'A4')
                pdf.set_auto_page_break(auto=True, margin=15)
                pdf.set_signature_footer(data_pdi.get('signatures', []), data_pdi.get('doc_uuid', ''))
                
                # --- CAPA PRINCIPAL ---
                pdf.add_page()
                if os.path.exists("logo_prefeitura.png"): pdf.image("logo_prefeitura.png", 10, 10, 25)
                if os.path.exists("logo_escola.png"): pdf.image("logo_escola.png", 175, 10, 25)

                pdf.set_y(15); pdf.set_font("Arial", "B", 14)
                pdf.cell(0, 10, clean_pdf_text("PREFEITURA MUNICIPAL DE LIMEIRA"), 0, 1, 'C')
                pdf.cell(0, 10, clean_pdf_text("SECRETARIA MUNICIPAL DE EDUCAÇÃO"), 0, 1, 'C')
                
                pdf.ln(40)
                pdf.set_font("Arial", "B", 30)
                pdf.cell(0, 20, "PDI", 0, 1, 'C')
                pdf.set_font("Arial", "B", 20)
                pdf.cell(0, 15, "PLANO DE DESENVOLVIMENTO", 0, 1, 'C')
                pdf.cell(0, 15, "INDIVIDUAL", 0, 1, 'C')
                
                pdf.ln(20)
                pdf.set_font("Arial", "", 16)
                pdf.cell(0, 10, "Estudo de Caso e Plano de AEE", 0, 1, 'C')
                pdf.ln(40)
                pdf.set_font("Arial", "B", 14)
                pdf.cell(0, 10, f"ANO: {datetime.now().year}", 0, 1, 'C')

                # --- CAPA SECUNDÁRIA: ESTUDO DE CASO ---
                pdf.add_page()
                if os.path.exists("logo_prefeitura.png"): pdf.image("logo_prefeitura.png", 10, 10, 25)
                if os.path.exists("logo_escola.png"): pdf.image("logo_escola.png", 175, 10, 25)
                
                pdf.set_y(120) 
                pdf.set_font("Arial", "B", 24)
                pdf.cell(0, 10, "ESTUDO DE CASO", 0, 1, 'C')

                # ==========================================================
                # INÍCIO DO CONTEÚDO DO ESTUDO DE CASO (INTEGRADO NO PDI)
                # ==========================================================
                data = data_case  # <--- ADICIONE ESTA LINHA AQUI!
                pdf.set_margins(15, 15, 15)
                # --- 1.1 DADOS GERAIS ---
                pdf.add_page()
                pdf.section_title("1.1 DADOS GERAIS DO ESTUDANTE", width=0)
                pdf.ln(4)
                
                # 1.1.1 IDENTIFICAÇÃO
                pdf.set_fill_color(240, 240, 240)
                pdf.set_font("Arial", "B", 10); pdf.cell(0, 8, "1.1.1 - IDENTIFICAÇÃO", 1, 1, 'L', 1)
                
                draw_flex_row(pdf, [
                    (30, "Nome:", "B", "L", True),
                    (110, clean_pdf_text(data.get('nome', '')), "", "L", False),
                    (15, "D.N.:", "B", "C", True),
                    (25, clean_pdf_text(str(data.get('d_nasc', ''))), "", "C", False)
                ], line_h=7, font_size=10)
                
                draw_flex_row(pdf, [
                    (30, "Escolaridade:", "B", "L", True),
                    (25, clean_pdf_text(data.get('ano_esc', '')), "", "L", False),
                    (20, "Período:", "B", "C", True),
                    (20, clean_pdf_text(data.get('periodo', '')), "", "C", False),
                    (20, "Unidade:", "B", "C", True),
                    (65, clean_pdf_text(data.get('unidade', '')), "", "L", False)
                ], line_h=7, font_size=10)
                
                draw_flex_row(pdf, [
                    (30, "Endereço:", "B", "L", True),
                    (150, clean_pdf_text(data.get('endereco', '')), "", "L", False)
                ], line_h=7, font_size=10)

                draw_flex_row(pdf, [
                    (20, "Bairro:", "B", "L", True),
                    (70, clean_pdf_text(data.get('bairro', '')), "", "L", False),
                    (20, "Cidade:", "B", "C", True),
                    (70, clean_pdf_text(data.get('cidade', '')), "", "L", False)
                ], line_h=7, font_size=10)
                
                draw_flex_row(pdf, [
                    (20, "Telefone:", "B", "L", True),
                    (160, clean_pdf_text(data.get('telefones', '')), "", "L", False)
                ], line_h=7, font_size=10)
                
                # 1.1.2 DADOS FAMILIARES
                pdf.ln(4)
                pdf.set_font("Arial", "B", 10); pdf.cell(0, 8, "1.1.2 - DADOS FAMILIARES", 1, 1, 'L', 1)
                
                draw_flex_row(pdf, [
                    (20, "Pai:", "B", "L", True),
                    (80, clean_pdf_text(data.get('pai_nome', '')), "", "L", False),
                    (25, "Profissão:", "B", "C", True),
                    (55, clean_pdf_text(data.get('pai_prof', '')), "", "L", False)
                ], line_h=7, font_size=10)
                
                draw_flex_row(pdf, [
                    (20, "Mãe:", "B", "L", True),
                    (80, clean_pdf_text(data.get('mae_nome', '')), "", "L", False),
                    (25, "Profissão:", "B", "C", True),
                    (55, clean_pdf_text(data.get('mae_prof', '')), "", "L", False)
                ], line_h=7, font_size=10)
                
                # Irmãos
                pdf.ln(2)
                pdf.set_font("Arial", "B", 10); pdf.cell(0, 8, clean_pdf_text("Irmãos (Nome | Idade | Escolaridade)"), 1, 1, 'L', 1)
                for i, irmao in enumerate(data.get('irmaos', [])):
                    if irmao['nome']:
                        txt = f"{irmao['nome']}  |  {irmao['idade']}  |  {irmao['esc']}"
                        draw_flex_row(pdf, [(180, clean_pdf_text(txt), "", "L", False)], line_h=6, font_size=9)
                
                pdf.ln(2)
                draw_flex_row(pdf, [
                    (40, "Com quem mora:", "B", "L", True),
                    (140, clean_pdf_text(data.get('quem_mora', '')), "", "L", False)
                ], line_h=7, font_size=10)
                
                draw_flex_row(pdf, [
                    (40, "Convênio Médico:", "B", "L", True),
                    (50, clean_pdf_text(data.get('convenio')), "", "L", False),
                    (20, "Qual:", "B", "C", True),
                    (70, clean_pdf_text(data.get('convenio_qual')), "", "L", False)
                ], line_h=7, font_size=10)
                
                draw_flex_row(pdf, [
                    (40, "Benefício Social:", "B", "L", True),
                    (50, clean_pdf_text(data.get('social')), "", "L", False),
                    (20, "Qual:", "B", "C", True),
                    (70, clean_pdf_text(data.get('social_qual')), "", "L", False)
                ], line_h=7, font_size=10)

                # 1.1.3 HISTÓRIA ESCOLAR
                pdf.ln(4)
                pdf.set_font("Arial", "B", 10); pdf.cell(0, 8, clean_pdf_text("1.1.3 - HISTÓRIA ESCOLAR"), 1, 1, 'L', 1)
                
                draw_flex_row(pdf, [(50, "Idade entrou na escola:", "B", "L", True), (130, clean_pdf_text(data.get('hist_idade_entrou')), "", "L", False)], line_h=7, font_size=10)
                draw_flex_row(pdf, [(50, "Outras escolas:", "B", "L", True), (130, clean_pdf_text(data.get('hist_outra_escola')), "", "L", False)], line_h=7, font_size=10)
                draw_flex_row(pdf, [(50, "Motivo transferência:", "B", "L", True), (130, clean_pdf_text(data.get('hist_motivo_transf')), "", "L", False)], line_h=7, font_size=10)
                
                if data.get('hist_obs'):
                    pdf.ln(2)
                    pdf.set_font("Arial", "B", 10); pdf.cell(0, 6, "Observações Escolares:", 0, 1)
                    draw_flex_row(pdf, [(180, clean_pdf_text(data.get('hist_obs')), "", "L", False)], line_h=6, font_size=9)

                # --- 1.2 GESTAÇÃO, PARTO E DESENVOLVIMENTO ---
                pdf.add_page()
                pdf.section_title("1.2 GESTAÇÃO, PARTO E DESENVOLVIMENTO", width=0)
                pdf.ln(4)
                
                def print_data_row(label, value):
                    draw_flex_row(pdf, [
                        (80, clean_pdf_text(label), "B", "L", True),
                        (100, clean_pdf_text(str(value) if value else ""), "", "L", False)
                    ], line_h=6, font_size=9)

                rows_gest = [
                    ("Parentesco entre pais:", data.get('gest_parentesco')),
                    ("Doença/Trauma na gestação:", data.get('gest_doenca')),
                    ("Uso de substâncias (mãe):", data.get('gest_substancias')),
                    ("Uso de medicamentos (mãe):", data.get('gest_medicamentos')),
                    ("Ocorrência no parto:", data.get('parto_ocorrencia')),
                    ("Necessitou de incubadora:", data.get('parto_incubadora')),
                    ("Prematuro?", f"{data.get('parto_prematuro')}  |  UTI: {data.get('parto_uti')}"),
                    ("Tempo de gestação / Peso:", f"{data.get('dev_tempo_gest')}  /  {data.get('dev_peso')}"),
                    ("Desenvolvimento normal no 1º ano:", data.get('dev_normal_1ano')),
                    ("Apresentou atraso importante?", data.get('dev_atraso')),
                    ("Idade que andou / falou:", f"{data.get('dev_idade_andar')}  /  {data.get('dev_idade_falar')}"),
                    ("Possui diagnóstico?", data.get('diag_possui')),
                    ("Reação da família ao diagnóstico:", data.get('diag_reacao')),
                    ("Data / Origem do diagnóstico:", f"{data.get('diag_data')}  |  {data.get('diag_origem')}"),
                    ("Pessoa com deficiência na família:", data.get('fam_deficiencia')),
                    ("Pessoa com AH/SD na família:", data.get('fam_altas_hab'))
                ]
                
                for label, value in rows_gest:
                    print_data_row(label, value)

                # --- 1.3 INFORMAÇÕES SOBRE SAÚDE ---
                pdf.add_page()
                pdf.section_title("1.3 INFORMAÇÕES SOBRE SAÚDE", width=0)
                pdf.ln(4)
                
                saude_rows = [
                    ("Problemas de saúde:", data.get('saude_prob')),
                    ("Já necessitou de internação:", data.get('saude_internacao')),
                    ("Restrição/Seletividade alimentar:", data.get('saude_restricao')),
                    ("Uso de medicamentos controlados:", f"{data.get('med_uso')} - Quais: {data.get('med_quais')}"),
                    ("Horário / Dosagem / Início:", f"{data.get('med_hor')}  |  {data.get('med_dos')}  |  {data.get('med_ini')}"),
                    ("Qualidade do sono:", data.get('sono')),
                    ("Última visita ao médico:", data.get('medico_ultimo'))
                ]
                for label, value in saude_rows:
                    print_data_row(label, value)
                
                esf = []
                if data.get('esf_urina'): esf.append("Urina")
                if data.get('esf_fezes'): esf.append("Fezes")
                print_data_row("Controle de Esfíncter:", f"{', '.join(esf) if esf else 'Não'}  (Idade: {data.get('esf_idade')})")
                
                pdf.ln(4)
                pdf.set_font("Arial", "B", 10); pdf.set_fill_color(240, 240, 240)
                pdf.cell(0, 8, "Atendimentos Clínicos Extraescolares", 1, 1, 'L', 1)
                
                clins = data.get('clinicas', [])
                print_data_row("Realiza atendimento em:", ", ".join(clins) if clins else "Não realiza")
                print_data_row("Especialidade médica:", data.get('clinicas_med_esp'))
                print_data_row("Nome da Clínica/Profissional:", data.get('clinicas_nome'))
                
                if data.get('saude_obs_geral'):
                    pdf.ln(2)
                    pdf.set_font("Arial", "B", 9); pdf.cell(0, 6, "Outras observações de saúde:", 0, 1)
                    draw_flex_row(pdf, [(180, clean_pdf_text(data.get('saude_obs_geral')), "", "L", False)], line_h=5, font_size=9)

                # --- 1.4 COMPREENSÃO DA FAMÍLIA (CHECKLIST) ---
                pdf.add_page()
                pdf.section_title("1.4 COMPREENSÃO DA FAMÍLIA (CHECKLIST)", width=0)
                pdf.ln(4)
                
                draw_flex_row(pdf, [
                    (110, "PERGUNTA / ASPECTO OBSERVADO", "B", "C", True),
                    (25, "SIM/NÃO", "B", "C", True),
                    (45, "OBSERVAÇÕES DA FAMÍLIA", "B", "C", True)
                ], line_h=8, font_size=9, fill_color=(220, 220, 220))
                
                checklist_items = [
                    "Relata fatos do dia a dia? Apresentando boa memória?",
                    "É organizado com seus pertences?",
                    "Aceita regras de forma tranquila?",
                    "Busca e aceita ajuda quando não sabe ou não consegue algo?",
                    "Aceita alterações no ambiente?",
                    "Tem algum medo?",
                    "Tem alguma mania?",
                    "Tem alguma área/assunto, brinquedo ou hiperfoco?",
                    "Prefere brincar sozinho ou com outras crianças? Tem amigos?",
                    "Qual a expectativa da família em relação à escolaridade da criança?"
                ]
                
                pdf.set_font("Arial", "", 9)
                
                # CORREÇÃO 1: Adicionado o enumerate para ter acesso ao índice 'i'
                for i, item in enumerate(checklist_items):
                    
                    # CORREÇÃO 2: Chave exata que foi usada no salvamento
                    key_base = f"itemcomport_{i}"
                    
                    opt = data_case.get('checklist', {}).get(f"{key_base}_opt", "Não")
                    obs = data_case.get('checklist', {}).get(f"{key_base}_obs", "")
                    
                    line_height = 6
                    num_lines = pdf.get_string_width(obs) / 50 
                    cell_height = max(line_height, (int(num_lines) + 1) * line_height)
                    
                    x_start = pdf.get_x(); y_start = pdf.get_y()
                    
                    pdf.multi_cell(110, line_height, clean_pdf_text(item), 1, 'L')
                    
                    pdf.set_xy(x_start + 110, y_start)
                    pdf.cell(25, cell_height, clean_pdf_text(opt), 1, 0, 'C')
                    
                    pdf.set_xy(x_start + 135, y_start)
                    pdf.multi_cell(0, line_height, clean_pdf_text(obs), 1, 'L')
                    
                    pdf.set_xy(x_start, y_start + cell_height)
                # ==========================================================
                # FIM DO CONTEÚDO DO ESTUDO DE CASO
                # RETOMADA DO PDI
                pdf.set_margins(10, 10, 10)
                # ==========================================================

                # --- CAPA SECUNDÁRIA: PLANO DE AEE ---
                pdf.add_page()
                if os.path.exists("logo_prefeitura.png"): pdf.image("logo_prefeitura.png", 10, 10, 25)
                if os.path.exists("logo_escola.png"): pdf.image("logo_escola.png", 175, 10, 25)
                
                pdf.set_y(100)
                pdf.set_font("Arial", "B", 20)
                pdf.cell(0, 10, clean_pdf_text("PLANO DE AEE"), 0, 1, 'C')
                pdf.ln(5)
                pdf.cell(0, 10, clean_pdf_text("ATENDIMENTO EDUCACIONAL"), 0, 1, 'C')
                pdf.cell(0, 10, clean_pdf_text("ESPECIALIZADO"), 0, 1, 'C')

                # --- 1. AVALIAÇÃO PEDAGÓGICA DO ESTUDANTE (RENOMEADO E REESTRUTURADO) ---
                pdf.add_page()
                pdf.section_title("AVALIAÇÃO PEDAGÓGICA DO ESTUDANTE", width=0)
                pdf.ln(5)
                
                pdf.set_font("Arial", "B", 10); pdf.cell(0, 6, "1.1 POTENCIALIDADES:", 0, 1)
                pdf.set_font("Arial", "", 10)
                pdf.multi_cell(0, 5, clean_pdf_text(data_pdi.get('potencialidades', '')), 1)

                pdf.ln(5)
                pdf.set_font("Arial", "B", 10); pdf.cell(0, 6, "1.2 ÁREAS DE INTERESSE:", 0, 1)
                pdf.set_font("Arial", "", 10)
                pdf.multi_cell(0, 5, clean_pdf_text(data_pdi.get('areas_interesse', '')), 1)

                pdf.ln(5)
                
                # Column Headers for Text Evolution (1.3.1 - 1.3.2)
                pdf.set_font("Arial", "B", 8)
                pdf.set_fill_color(220, 220, 220)
                w_col = (210 - 20) / 3 # Dynamic width for perfect fit
                
                def print_check_evolution(title, key, opt_key=None):
                    # Logic to show all options with checkboxes
                    real_opt_key = opt_key if opt_key else key
                    possible_opts = checklist_options.get(real_opt_key, [])
                    
                    selected_opts = data_pdi.get(f"{key}_diag", [])
                    if isinstance(selected_opts, str): selected_opts = [selected_opts]
                    if not selected_opts: selected_opts = []

                    if possible_opts:
                        d_lines = []
                        for opt in possible_opts:
                            mark = "[X]" if opt in selected_opts else "[ ]"
                            d_lines.append(f"{mark} {opt}")
                        d_text = "\n".join(d_lines)
                    else:
                        d_text = "\n".join(selected_opts) if selected_opts else "-"
                    
                    p_text = data_pdi.get(f"{key}_proc", "")
                    f_text = data_pdi.get(f"{key}_final", "")
                    
                    # Estimate height
                    pdf.set_font("Arial", "", 8)
                    
                    # Calc height accurately based on wrapping
                    def get_h(txt):
                         if not txt: return 6
                         lines = 0
                         eff_w = w_col - 4 # Effective width with padding
                         
                         for line in txt.split('\n'):
                             if not line:
                                 lines += 1
                                 continue
                             # Calculate lines needed for this paragraph
                             w_line = pdf.get_string_width(line)
                             if w_line > eff_w:
                                 # Ceiling division logic for positive integers
                                 lines += int((w_line / eff_w) + 0.99)
                             else:
                                 lines += 1
                         
                         return max(6, lines * 4) + 4
                    
                    h_max = max(get_h(d_text), get_h(p_text), get_h(f_text), 10)
                    
                    if pdf.get_y() + h_max + 8 > 270: 
                        pdf.add_page()
                        # Reprint headers
                        pdf.set_font("Arial", "B", 8)
                        pdf.set_fill_color(220, 220, 220)
                        pdf.cell(w_col, 10, clean_pdf_text("Resultados da Avaliação Diagnóstica"), 1, 0, 'C', True)
                        pdf.cell(w_col, 10, clean_pdf_text("Resultados da Avaliação de Percurso"), 1, 0, 'C', True)
                        pdf.cell(w_col, 10, clean_pdf_text("Resultados da Avaliação Final"), 1, 1, 'C', True)

                    # Title Row
                    pdf.set_font("Arial", "B", 9)
                    pdf.set_fill_color(240, 240, 240)
                    pdf.cell(0, 6, clean_pdf_text(title), 1, 1, 'L', True)
                    
                    y_start = pdf.get_y()
                    pdf.set_font("Arial", "", 8)
                    
                    pdf.set_xy(10, y_start)
                    pdf.multi_cell(w_col, 4, clean_pdf_text(d_text), 0, 'L')
                    
                    pdf.set_xy(10 + w_col, y_start)
                    pdf.multi_cell(w_col, 4, clean_pdf_text(p_text), 0, 'L')
                    
                    pdf.set_xy(10 + 2*w_col, y_start)
                    pdf.multi_cell(w_col, 4, clean_pdf_text(f_text), 0, 'L')
                    
                    # Borders
                    pdf.rect(10, y_start, w_col, h_max)
                    pdf.rect(10 + w_col, y_start, w_col, h_max)
                    pdf.rect(10 + 2*w_col, y_start, w_col, h_max)
                    
                    pdf.set_y(y_start + h_max)

                def print_text_evolution(title, key):
                    d = data_pdi.get(f"{key}_diag", "")
                    p = data_pdi.get(f"{key}_proc", "")
                    f = data_pdi.get(f"{key}_final", "")
                    
                    pdf.set_font("Arial", "", 8)
                    # Calc height accurately based on wrapping
                    def get_h(txt):
                         if not txt: return 6
                         lines = 0
                         eff_w = w_col - 4 # Effective width with padding
                         
                         for line in txt.split('\n'):
                             if not line:
                                 lines += 1
                                 continue
                             # Calculate lines needed for this paragraph
                             w_line = pdf.get_string_width(line)
                             if w_line > eff_w:
                                 # Ceiling division logic for positive integers
                                 lines += int((w_line / eff_w) + 0.99)
                             else:
                                 lines += 1
                         
                         return max(6, lines * 4) + 4
                    
                    h_max = max(get_h(d), get_h(p), get_h(f), 10)
                    
                    if pdf.get_y() + h_max + 8 > 270: 
                        pdf.add_page()
                        # Reprint headers
                        pdf.set_font("Arial", "B", 8)
                        pdf.set_fill_color(220, 220, 220)
                        pdf.cell(w_col, 10, clean_pdf_text("Resultados da Avaliação Diagnóstica"), 1, 0, 'C', True)
                        pdf.cell(w_col, 10, clean_pdf_text("Resultados da Avaliação de Percurso"), 1, 0, 'C', True)
                        pdf.cell(w_col, 10, clean_pdf_text("Resultados da Avaliação Final"), 1, 1, 'C', True)

                    # Title Row
                    pdf.set_font("Arial", "B", 9)
                    pdf.set_fill_color(240, 240, 240)
                    pdf.cell(0, 6, clean_pdf_text(title), 1, 1, 'L', True)
                    
                    y_start = pdf.get_y()
                    pdf.set_font("Arial", "", 8)
                    
                    pdf.set_xy(10, y_start)
                    pdf.multi_cell(w_col, 4, clean_pdf_text(d), 0, 'L')
                    
                    pdf.set_xy(10 + w_col, y_start)
                    pdf.multi_cell(w_col, 4, clean_pdf_text(p), 0, 'L')
                    
                    pdf.set_xy(10 + 2*w_col, y_start)
                    pdf.multi_cell(w_col, 4, clean_pdf_text(f), 0, 'L')
                    
                    pdf.rect(10, y_start, w_col, h_max)
                    pdf.rect(10 + w_col, y_start, w_col, h_max)
                    pdf.rect(10 + 2*w_col, y_start, w_col, h_max)
                    
                    pdf.set_y(y_start + h_max)

                def print_obs(key):
                    obs = data_pdi.get(key, '')
                    if obs:
                        pdf.set_font("Arial", "", 9)
                        pdf.multi_cell(0, 5, clean_pdf_text(f"OBSERVAÇÕES: {obs}"), 0, 'L')
                        pdf.ln(2)

                # 1.3 Cognitivo
                pdf.set_font("Arial", "B", 10); pdf.cell(0, 8, "1.3 DESENVOLVIMENTO COGNITIVO", 0, 1)
                
                # 1.3.1 & 1.3.2 (Text Tables)
                pdf.set_font("Arial", "B", 8)
                pdf.set_fill_color(220, 220, 220)
                pdf.cell(w_col, 10, clean_pdf_text("Resultados da Avaliação Diagnóstica"), 1, 0, 'C', True)
                pdf.cell(w_col, 10, clean_pdf_text("Resultados da Avaliação de Percurso"), 1, 0, 'C', True)
                pdf.cell(w_col, 10, clean_pdf_text("Resultados da Avaliação Final"), 1, 1, 'C', True)
                
                pdf.set_font("Arial", "B", 9); pdf.cell(0, 6, "1.3.1 PERCEPÇÃO", 0, 1)
                items_perc = ["Visual", "Auditiva", "Tátil", "Espacial", "Temporal"]
                for it in items_perc: print_text_evolution(it, f"cog_{it.lower()}")
                
                pdf.set_font("Arial", "B", 9); pdf.cell(0, 6, "1.3.2 RACIOCÍNIO LÓGICO", 0, 1)
                items_rac = ["Correspondência", "Comparação", "Classificação", "Sequenciação", "Seriação", "Inclusão", "Conservação", "Resolução de Problemas"]
                for it in items_rac: print_text_evolution(it, f"cog_{it.lower()}")

                # 1.3.3 Checklists
                pdf.ln(2); pdf.set_font("Arial", "B", 9); pdf.cell(0, 6, "1.3.3 SISTEMA MONETÁRIO", 0, 1)
                print_check_evolution("Sistema Monetário", "sis_monetario", "sis_monetario")
                
                pdf.ln(2); pdf.set_font("Arial", "B", 9); pdf.cell(0, 6, "1.3.4 CAPACIDADE DE BRINCAR", 0, 1)
                print_check_evolution("Faz uso dos brinquedos de maneira funcional?", "brincar_funcional", "brincar_funcional")
                print_check_evolution("Exploração dos brinquedos", "brincar_explora", "brincar_explora")
                print_check_evolution("Estrutura brincadeira de forma criativa?", "brincar_criativa", "brincar_criativa")
                print_check_evolution("Atribui diferentes funções aos objetos?", "brincar_funcoes", "brincar_funcoes")
                print_obs('brincar_obs')

                pdf.ln(2); pdf.set_font("Arial", "B", 9); pdf.cell(0, 6, "1.3.5 MEMÓRIA DE CURTO PRAZO", 0, 1)
                print_check_evolution("Memória Curto Prazo", "mem_curto", "memoria_curto")
                print_obs('memoria_obs')
                
                pdf.ln(2); pdf.set_font("Arial", "B", 9); pdf.cell(0, 6, "1.3.6 MEMÓRIA DE LONGO PRAZO", 0, 1)
                print_check_evolution("Episódica", "mem_episodica", "memoria_episodica")
                print_check_evolution("Semântica", "mem_semantica", "memoria_semantica")
                
                pdf.ln(2); pdf.set_font("Arial", "B", 9); pdf.cell(0, 6, "1.3.7 ATENÇÃO", 0, 1)
                print_check_evolution("Sustentada", "at_sust", "atencao_sustentada")
                print_check_evolution("Dividida", "at_div", "atencao_dividida")
                print_check_evolution("Seletiva", "at_sel", "atencao_seletiva")
                print_obs('atencao_obs')
                
                pdf.ln(2); pdf.set_font("Arial", "B", 9); pdf.cell(0, 6, "1.3.8 COORDENAÇÃO VISO-MOTORA", 0, 1)
                print_check_evolution("Desenho", "vm_desenho", "vm_desenho")
                print_check_evolution("Limites Folha", "vm_l_folha", "vm_limite_folha")
                print_check_evolution("Limites Pintura", "vm_l_pint", "vm_limite_pintura")
                print_check_evolution("Recorte (Rasgar)", "vm_rasgar", "vm_rasgar")
                print_check_evolution("Uso Tesoura", "vm_tesoura", "vm_tesoura")
                print_check_evolution("Uso Cola", "vm_cola", "vm_cola")
                print_check_evolution("Encaixes", "vm_encaixe", "vm_encaixe")
                print_check_evolution("Reprodução de Figuras", "vm_reproducao", "vm_reproducao")
                print_check_evolution("Quebra-Cabeça", "vm_qc", "vm_quebra_cabeca")
                print_obs('vm_obs')

                # 1.4 Motor
                pdf.ln(2); pdf.set_font("Arial", "B", 10); pdf.cell(0, 8, "1.4 DESENVOLVIMENTO MOTOR", 0, 1)
                pdf.set_font("Arial", "B", 9); pdf.cell(0, 6, "1.4.1 COORDENAÇÃO MOTORA FINA", 0, 1)
                print_check_evolution("Estabilidade Punho", "mf_punho", "mf_punho")
                print_check_evolution("Pinça", "mf_pinca", "mf_pinca")
                print_check_evolution("Preensão", "mf_preensao", "mf_preensao")
                print_obs('mf_obs')
                
                pdf.ln(2); pdf.set_font("Arial", "B", 9); pdf.cell(0, 6, "1.4.2 COORDENAÇÃO MOTORA GLOBAL", 0, 1)
                print_check_evolution("Postura (Sentado)", "mg_sentado", "mg_tronco_sentado")
                print_check_evolution("Postura (Pé)", "mg_pe", "mg_tronco_pe")
                print_check_evolution("Outros (Postura)", "mg_postura_opts", "mg_postura_opts")
                print_check_evolution("Mão de Apoio", "mg_mao_apoio", "mg_mao_apoio")
                print_check_evolution("Locomoção", "mg_loc", "mg_locomocao")
                print_check_evolution("Equilíbrio", "mg_eq", "mg_equilibrio")
                print_obs('mg_obs')
                
                pdf.ln(2); pdf.set_font("Arial", "B", 9); pdf.cell(0, 6, "1.4.3 ESQUEMA E IMAGEM CORPORAL", 0, 1)
                print_check_evolution("Imagem Corporal", "ec_img", "ec_imagem")
                print_check_evolution("Identificação Partes", "ec_partes", "ec_partes")
                print_check_evolution("Funções Partes", "ec_func", "ec_funcoes")
                print_check_evolution("Imitação", "ec_imit", "ec_imitar")
                print_check_evolution("Desenho Humano", "ec_des", "ec_desenho")
                print_check_evolution("Dominância Lateral", "ec_lat", "ec_dominancia")
                print_check_evolution("Identifica Lateralidade", "ec_id_lat", "ec_identifica")
                print_check_evolution("Uso dois lados", "ec_dois", "ec_dois_lados")
                print_obs('ec_obs')
                
                pdf.ln(2); pdf.set_font("Arial", "B", 9); pdf.cell(0, 6, "1.4.4 AUTONOMIA / AVD", 0, 1)
                print_check_evolution("Alimentação", "avd_alim", "avd_alimentacao")
                print_check_evolution("Higiene", "avd_hig", "avd_higiene")
                print_check_evolution("Uso Objetos", "avd_obj", "avd_objetos")
                print_check_evolution("Locomoção Escola", "avd_loc", "avd_locomocao")
                print_obs('avd_obs')

                # 1.5 Pessoal
                pdf.ln(2); pdf.set_font("Arial", "B", 10); pdf.cell(0, 8, "1.5 FUNÇÃO PESSOAL E SOCIAL", 0, 1)
                print_check_evolution("1.5.1 Interação", "ps_int", "ps_interacao")
                print_check_evolution("1.5.2 Iniciativa (Diálogo)", "ps_ini_d", "ps_iniciativa_dialogo")
                print_check_evolution("1.5.2 Iniciativa (Atividade)", "ps_ini_a", "ps_iniciativa_ativ")
                
                pdf.set_font("Arial", "B", 9); pdf.cell(0, 6, "1.5.3 Comportamentos Apresentados", 0, 1)
                print_check_evolution("Comportamentos", "ps_comps", "ps_comps")
                
                pdf.ln(2); pdf.set_font("Arial", "B", 9); pdf.cell(0, 6, "1.5.4 Vida Prática", 0, 1)
                print_check_evolution("Sabe Nome", "vp_nome", "vp_nome")
                print_check_evolution("Sabe Idade", "vp_idade", "vp_sim_nao")
                print_check_evolution("Sabe Aniversário", "vp_niver", "vp_niver")
                print_check_evolution("Nomeia Familiares", "vp_fam", "vp_sim_nao")
                print_check_evolution("Nomeia Profs", "vp_prof", "vp_sim_nao")
                print_check_evolution("Nomeia Escola", "vp_escola", "vp_sim_nao")
                print_check_evolution("Sabe Ano Escolar", "vp_ano_esc", "vp_sim_nao")
                print_check_evolution("Sabe Endereço", "vp_end", "vp_sim_nao")
                print_obs('vp_outros')
                
                # 1.6 Linguagem
                pdf.ln(2); pdf.set_font("Arial", "B", 10); pdf.cell(0, 8, "1.6 LINGUAGEM", 0, 1)
                print_check_evolution("1.6.1 Verbal", "ling_verb", "ling_verbal")
                print_check_evolution("1.6.2 Compreensiva", "ling_comp", "ling_compreensiva")
                print_check_evolution("1.6.3 Gestual", "ling_gest", "ling_gestual")
                print_check_evolution("1.6.4 Ecolalia", "ling_eco", "ling_ecolalia")
                print_check_evolution("1.6.5 Escrita", "ling_esc", "ling_escrita")
                print_check_evolution("1.6.6 Leitura", "ling_leit", "ling_leitura")
                
                pdf.ln(2); pdf.set_font("Arial", "B", 9); pdf.cell(0, 6, "1.6.7 LIBRAS e 1.6.8 Comunicação Alternativa", 0, 1)
                print_check_evolution("Aparelho Auditivo", "libras_aparelho", "libras_aparelho")
                print_check_evolution("Implante Coclear", "libras_implante", "libras_implante")
                print_check_evolution("Com. Libras", "libras_com", "libras_com")
                print_check_evolution("Compreensão Libras", "libras_compreende", "libras_compreende")
                print_check_evolution("Escrita Braille", "braille_esc", "braille_esc")
                print_check_evolution("Leitura Braille", "braille_leit", "braille_leit")
                print_obs('libras_outros')
                print_check_evolution("Com. Alternativa", "com_alt", "com_alt")

                # --- 2. AÇÕES NECESSÁRIAS E ORGANIZAÇÃO (REFORMULADO) ---
                pdf.add_page()
                pdf.section_title("2. AÇÕES NECESSÁRIAS E ORGANIZAÇÃO", width=0)
                pdf.ln(5)

                # Actions Table
                pdf.set_font("Arial", "B", 10)
                pdf.set_fill_color(220, 220, 220)
                # Scope col 50mm, Action col 140mm
                col_s = 50
                col_a = 190 - col_s
                
                pdf.cell(col_s, 8, clean_pdf_text("ÂMBITO"), 1, 0, 'C', True)
                pdf.cell(0, 8, clean_pdf_text("AÇÃO"), 1, 1, 'C', True)
                
                actions_list = [
                    ("ESCOLA", data_pdi.get('acao_escola', '')),
                    ("SALA DE AULA", data_pdi.get('acao_sala', '')),
                    ("FAMÍLIA", data_pdi.get('acao_familia', '')),
                    ("SAÚDE", data_pdi.get('acao_saude', ''))
                ]
                
                pdf.set_font("Arial", "", 10)
                for scope, txt in actions_list:
                    txt = clean_pdf_text(txt)
                    
                    # Estimate height for multicell
                    # Approx 65 chars per line for 140mm width with Arial 10
                    # Just a safe estimate
                    s_width = pdf.get_string_width(txt)
                    lines = int(s_width / col_a) + 1 + txt.count('\n')
                    h_row = max(8, lines * 5 + 4) # 5mm line height + padding
                    
                    # Check page break
                    if pdf.get_y() + h_row > 270:
                        pdf.add_page()
                        pdf.set_font("Arial", "B", 10)
                        pdf.set_fill_color(220, 220, 220)
                        pdf.cell(col_s, 8, clean_pdf_text("ÂMBITO"), 1, 0, 'C', True)
                        pdf.cell(0, 8, clean_pdf_text("AÇÃO"), 1, 1, 'C', True)
                        pdf.set_font("Arial", "", 10)
                    
                    x = pdf.get_x()
                    y = pdf.get_y()
                    
                    # Scope
                    pdf.set_font("Arial", "B", 10)
                    pdf.cell(col_s, h_row, clean_pdf_text(scope), 1, 0, 'C')
                    
                    # Action
                    pdf.set_xy(x + col_s, y)
                    pdf.set_font("Arial", "", 10)
                    pdf.multi_cell(col_a, 5, txt, 0, 'L')
                    
                    # Border around action
                    pdf.rect(x + col_s, y, col_a, h_row)
                    pdf.set_y(y + h_row)

                pdf.ln(5)
                # Organization
                pdf.set_font("Arial", "B", 10)
                pdf.set_fill_color(220, 220, 220)
                pdf.cell(0, 8, clean_pdf_text("ORGANIZAÇÃO DO AEE"), 1, 1, 'L', True)
                
                pdf.set_font("Arial", "", 10)
                # Simple table for organization
                pdf.cell(50, 8, "Tempo de Atendimento:", 1, 0, 'L')
                pdf.cell(0, 8, clean_pdf_text(data_pdi.get('aee_tempo', '')), 1, 1, 'L')
                
                pdf.cell(50, 8, "Modalidade:", 1, 0, 'L')
                pdf.cell(0, 8, clean_pdf_text(data_pdi.get('aee_tipo', '')), 1, 1, 'L')
                
                pdf.cell(50, 8, clean_pdf_text("Composição:"), 1, 0, 'L')
                pdf.cell(0, 8, clean_pdf_text(data_pdi.get('aee_comp', '')), 1, 1, 'L')

                # --- 4. OBJETIVOS ---
                pdf.add_page()
                pdf.section_title("4. OBJETIVOS A SEREM ATINGIDOS", width=0)
                pdf.ln(5)
                
                for category, subcats in objectives_structure.items():
                    if pdf.get_y() > 250: pdf.add_page()
                    pdf.set_fill_color(200, 200, 200) # Darker grey for category
                    pdf.set_font("Arial", "B", 10)
                    pdf.cell(0, 8, clean_pdf_text(category), 1, 1, 'C', True)
                    
                    for subcat_name, items_list in subcats.items():
                        if subcat_name != "GERAL" and subcat_name != "OUTROS":
                            pdf.set_fill_color(240, 240, 240) # Lighter grey for subcategory
                            pdf.cell(0, 6, clean_pdf_text(subcat_name), 1, 1, 'C', True)
                        
                        for item in items_list:
                             if pdf.get_y() > 250: pdf.add_page()
                             
                             item_key = f"goal_{category}_{subcat_name}_{item}".replace(" ", "_").lower()
                             content = data_pdi['goals_specific'].get(item_key, "")
                             
                             # Render Row: Label | Content
                             # Calculate dynamic height based on content width
                             label_w = 60
                             content_w = 190 - label_w 
                             
                             # Accurate height estimation using FPDF string width
                             pdf.set_font("Arial", "", 9)
                             text_len = pdf.get_string_width(content)
                             lines = int(text_len / content_w) + 1
                             lines += content.count('\n')
                             line_height = 5
                             total_h = max(8, lines * line_height)
                             
                             if pdf.get_y() + total_h > 270: 
                                 pdf.add_page()
                             
                             x_start = pdf.get_x()
                             y_start = pdf.get_y()
                             
                             # Print Label
                             pdf.set_font("Arial", "B", 9)
                             pdf.cell(label_w, total_h, clean_pdf_text(item), 1, 0, 'L')
                             
                             # Print Content
                             pdf.set_xy(x_start + label_w, y_start)
                             pdf.set_font("Arial", "", 9)
                             pdf.multi_cell(content_w, line_height, clean_pdf_text(content), 1, 'L')
                             
                             # Draw border around content block to match height
                             pdf.rect(x_start + label_w, y_start, content_w, total_h)
                             
                             # Reset cursor
                             pdf.set_xy(x_start, y_start + total_h)


                st.session_state.pdf_bytes_pdi = get_pdf_bytes(pdf)
                st.rerun()

            if 'pdf_bytes_pdi' in st.session_state:
                st.download_button("📥 BAIXAR PDI COMPLETO", st.session_state.pdf_bytes_pdi, f"PDI_{data_pdi.get('nome','aluno')}.pdf", "application/pdf", type="primary")

        # --- ABA 5: HISTÓRICO ---
        with tabs[4]:
            st.subheader("Histórico de Atividades")
            df_hist = safe_read("Historico", ["Data_Hora", "Aluno", "Usuario", "Acao", "Detalhes"])
            if not df_hist.empty and data_pdi.get('nome'):
                student_hist = df_hist[df_hist["Aluno"] == data_pdi.get('nome')]
                if not student_hist.empty:
                    st.dataframe(student_hist.iloc[::-1], use_container_width=True, hide_index=True)
                else: st.info("Sem histórico.")
            else: st.info("Histórico vazio.")


    # ESTUDO DE CASO COM FORMULÁRIOS
    elif doc_mode == "Estudo de Caso":
        st.markdown("""<div class="header-box"><div class="header-title">Estudo de Caso</div></div>""", unsafe_allow_html=True)
        
        if 'data_case' not in st.session_state: 
            st.session_state.data_case = {
                'irmaos': [{'nome': '', 'idade': '', 'esc': ''} for _ in range(4)], 
                'checklist': {},
                'clinicas': []
            }
        
        data = st.session_state.data_case
        
        st.markdown("""<style>div[data-testid="stFormSubmitButton"] > button {width: 100%; background-color: #dcfce7; color: #166534; border: 1px solid #166534;}</style>""", unsafe_allow_html=True)

        tabs = st.tabs(["1. Identificação", "2. Família", "3. Histórico", "4. Saúde", "5. Comportamento", "6. Assinaturas", "7. Gerar PDF", "8. Histórico"])

        # --- ABA 1: IDENTIFICAÇÃO ---
        with tabs[0]:
            with st.form("form_caso_identificacao") if not is_monitor else st.container():
                st.subheader("1.1 Dados Gerais do Estudante")
                data['nome'] = st.text_input("Nome Completo", value=data.get('nome', ''), disabled=True)
                
                c1, c2, c3 = st.columns([1, 1, 2])
                data['ano_esc'] = c1.text_input("Ano Escolaridade", value=data.get('ano_esc', ''), disabled=is_monitor)
                
                p_val = data.get('periodo') if data.get('periodo') in ["Manhã", "Tarde", "Integral"] else "Manhã"
                idx_per = ["Manhã", "Tarde", "Integral"].index(p_val)
                data['periodo'] = c2.selectbox("Período", ["Manhã", "Tarde", "Integral"], index=idx_per, disabled=is_monitor)
                data['unidade'] = c3.text_input("Unidade Escolar", value=data.get('unidade', ''), disabled=is_monitor)

                c4, c5 = st.columns([1, 1])
                data['sexo'] = c4.radio("Sexo", ["Feminino", "Masculino"], horizontal=True, index=0 if data.get('sexo') == 'Feminino' else 1, disabled=is_monitor)
                
                d_nasc = data.get('d_nasc')
                if isinstance(d_nasc, str):
                    try: d_nasc = datetime.strptime(d_nasc, '%Y-%m-%d').date()
                    except: d_nasc = date.today()
                data['d_nasc'] = c5.date_input("Data de Nascimento", value=d_nasc if d_nasc else date.today(), format="DD/MM/YYYY", disabled=is_monitor)

                data['endereco'] = st.text_input("Endereço", value=data.get('endereco', ''), disabled=is_monitor)
                c6, c7, c8 = st.columns([2, 2, 2])
                data['bairro'] = c6.text_input("Bairro", value=data.get('bairro', ''), disabled=is_monitor)
                data['cidade'] = c7.text_input("Cidade", value=data.get('cidade', 'Limeira'), disabled=is_monitor)
                data['telefones'] = c8.text_input("Telefones", value=data.get('telefones', ''), disabled=is_monitor)
                
                st.markdown("---")
                if not is_monitor:
                    if st.form_submit_button("💾 Salvar Dados de Identificação"):
                        save_student("CASO", data.get('nome'), data, "Identificação")

        # --- ABA 2: DADOS FAMILIARES ---
        with tabs[1]:
            with st.form("form_caso_familia") if not is_monitor else st.container():
                st.subheader("1.1.2 Dados Familiares")
                
                st.markdown("**Pai**")
                c_p1, c_p2, c_p3, c_p4 = st.columns([3, 2, 2, 2])
                data['pai_nome'] = c_p1.text_input("Nome do Pai", value=data.get('pai_nome', ''), disabled=is_monitor)
                data['pai_prof'] = c_p2.text_input("Profissão Pai", value=data.get('pai_prof', ''), disabled=is_monitor)
                data['pai_esc'] = c_p3.text_input("Escolaridade Pai", value=data.get('pai_esc', ''), disabled=is_monitor)
                data['pai_dn'] = c_p4.text_input("D.N. Pai", value=data.get('pai_dn', ''), disabled=is_monitor) 

                st.markdown("**Mãe**")
                c_m1, c_m2, c_m3, c_m4 = st.columns([3, 2, 2, 2])
                data['mae_nome'] = c_m1.text_input("Nome da Mãe", value=data.get('mae_nome', ''), disabled=is_monitor)
                data['mae_prof'] = c_m2.text_input("Profissão Mãe", value=data.get('mae_prof', ''), disabled=is_monitor)
                data['mae_esc'] = c_m3.text_input("Escolaridade Mãe", value=data.get('mae_esc', ''), disabled=is_monitor)
                data['mae_dn'] = c_m4.text_input("D.N. Mãe", value=data.get('mae_dn', ''), disabled=is_monitor)

                st.divider()
                st.markdown("**Irmãos**")
                if 'irmaos' not in data: data['irmaos'] = [{'nome': '', 'idade': '', 'esc': ''} for _ in range(4)]
                
                for i in range(4):
                    c_i1, c_i2, c_i3 = st.columns([3, 1, 2])
                    data['irmaos'][i]['nome'] = c_i1.text_input(f"Nome Irmão {i+1}", value=data['irmaos'][i]['nome'], disabled=is_monitor)
                    data['irmaos'][i]['idade'] = c_i2.text_input(f"Idade {i+1}", value=data['irmaos'][i]['idade'], disabled=is_monitor)
                    data['irmaos'][i]['esc'] = c_i3.text_input(f"Escolaridade {i+1}", value=data['irmaos'][i]['esc'], disabled=is_monitor)

                data['outros_familia'] = st.text_area("Outros (Moradores da casa):", value=data.get('outros_familia', ''), disabled=is_monitor)
                data['quem_mora'] = st.text_input("Com quem mora?", value=data.get('quem_mora', ''), disabled=is_monitor)
                
                c_conv1, c_conv2 = st.columns([1, 3])
                data['convenio'] = c_conv1.radio("Possui convênio?", ["Sim", "Não"], horizontal=True, index=1 if data.get('convenio') == "Não" else 0, disabled=is_monitor)
                data['convenio_qual'] = c_conv2.text_input("Qual convênio?", value=data.get('convenio_qual', ''), disabled=is_monitor)
                
                c_soc1, c_soc2 = st.columns([1, 3])
                data['social'] = c_soc1.radio("Recebe benefício social?", ["Sim", "Não"], horizontal=True, index=1 if data.get('social') == "Não" else 0, disabled=is_monitor)
                data['social_qual'] = c_soc2.text_input("Qual benefício?", value=data.get('social_qual', ''), disabled=is_monitor)

                st.markdown("---")
                if not is_monitor:
                    if st.form_submit_button("💾 Salvar Dados Familiares"):
                        save_student("CASO", data.get('nome'), data, "Família")

        # --- ABA 3: HISTÓRICO ---
        with tabs[2]:
            with st.form("form_caso_historico") if not is_monitor else st.container():
                st.subheader("1.1.3 História Escolar")
                data['hist_idade_entrou'] = st.text_input("Idade que entrou na escola:", value=data.get('hist_idade_entrou', ''), disabled=is_monitor)
                data['hist_outra_escola'] = st.text_input("Já estudou em outra escola? Quais?", value=data.get('hist_outra_escola', ''), disabled=is_monitor)
                data['hist_motivo_transf'] = st.text_input("Motivo da transferência:", value=data.get('hist_motivo_transf', ''), disabled=is_monitor)
                data['hist_obs'] = st.text_area("Outras observações escolares:", value=data.get('hist_obs', ''), disabled=is_monitor)

                st.divider()
                st.subheader("1.2 Informações sobre Gestação")
                
                c_g1, c_g2 = st.columns(2)
                data['gest_parentesco'] = c_g1.radio("Parentesco entre pais?", ["Sim", "Não"], horizontal=True, index=1 if data.get('gest_parentesco') == "Não" else 0, disabled=is_monitor)
                data['gest_doenca'] = c_g2.text_input("Doença/trauma na gestação? Quais?", value=data.get('gest_doenca', ''), disabled=is_monitor)
                
                c_g3, c_g4 = st.columns(2)
                data['gest_substancias'] = c_g3.radio("Uso de álcool/fumo/drogas?", ["Sim", "Não"], horizontal=True, index=1 if data.get('gest_substancias') == "Não" else 0, disabled=is_monitor)
                data['gest_medicamentos'] = c_g4.text_input("Uso de medicamentos? Quais?", value=data.get('gest_medicamentos', ''), disabled=is_monitor)

                data['parto_ocorrencia'] = st.text_input("Ocorrência no parto? Quais?", value=data.get('parto_ocorrencia', ''), disabled=is_monitor)
                data['parto_incubadora'] = st.text_input("Incubadora? Motivo?", value=data.get('parto_incubadora', ''), disabled=is_monitor)
                
                c_p1, c_p2 = st.columns(2)
                data['parto_prematuro'] = c_p1.radio("Prematuro?", ["Sim", "Não"], horizontal=True, index=1 if data.get('parto_prematuro') == "Não" else 0, disabled=is_monitor)
                data['parto_uti'] = c_p2.radio("Ficou em UTI?", ["Sim", "Não"], horizontal=True, index=1 if data.get('parto_uti') == "Não" else 0, disabled=is_monitor)

                c_d1, c_d2, c_d3 = st.columns(3)
                data['dev_tempo_gest'] = c_d1.text_input("Tempo Gestação", value=data.get('dev_tempo_gest', ''), disabled=is_monitor)
                data['dev_peso'] = c_d2.text_input("Peso", value=data.get('dev_peso', ''), disabled=is_monitor)
                data['dev_normal_1ano'] = c_d3.radio("Desenv. normal 1º ano?", ["Sim", "Não"], horizontal=True, index=0 if data.get('dev_normal_1ano') == "Sim" else 1, disabled=is_monitor)
                
                data['dev_atraso'] = st.text_input("Atraso importante? Quais?", value=data.get('dev_atraso', ''), disabled=is_monitor)
                c_m1, c_m2 = st.columns(2)
                data['dev_idade_andar'] = c_m1.text_input("Idade começou a andar?", value=data.get('dev_idade_andar', ''), disabled=is_monitor)
                data['dev_idade_falar'] = c_m2.text_input("Idade começou a falar?", value=data.get('dev_idade_falar', ''), disabled=is_monitor)

                st.markdown("---")
                data['diag_possui'] = st.text_input("Possui diagnóstico? Qual?", value=data.get('diag_possui', ''), disabled=is_monitor)
                data['diag_reacao'] = st.text_input("Reação da família:", value=data.get('diag_reacao', ''), disabled=is_monitor)
                c_dx1, c_dx2 = st.columns(2)
                data['diag_data'] = c_dx1.text_input("Data do diagnóstico:", value=data.get('diag_data', ''), disabled=is_monitor)
                data['diag_origem'] = c_dx2.text_input("Origem da informação:", value=data.get('diag_origem', ''), disabled=is_monitor)
                
                c_fam1, c_fam2 = st.columns(2)
                data['fam_deficiencia'] = c_fam1.text_input("Pessoa com deficiência na família?", value=data.get('fam_deficiencia', ''), disabled=is_monitor)
                data['fam_altas_hab'] = c_fam2.radio("Pessoa com AH/SD na família?", ["Sim", "Não"], horizontal=True, index=1 if data.get('fam_altas_hab') == "Não" else 0, disabled=is_monitor)
                
                st.markdown("---")
                if not is_monitor:
                    if st.form_submit_button("💾 Salvar Dados de Histórico"):
                        save_student("CASO", data.get('nome'), data, "Histórico")

        # --- ABA 4: SAÚDE ---
        with tabs[3]:
            with st.form("form_caso_saude") if not is_monitor else st.container():
                st.subheader("1.3 Informações sobre Saúde")
                data['saude_prob'] = st.text_input("Problema de saúde? Quais?", value=data.get('saude_prob', ''), disabled=is_monitor)
                data['saude_internacao'] = st.text_input("Internação? Motivos?", value=data.get('saude_internacao', ''), disabled=is_monitor)
                data['saude_restricao'] = st.text_input("Restrição/Seletividade alimentar?", value=data.get('saude_restricao', ''), disabled=is_monitor)
                
                st.markdown("**Medicamentos Controlados**")
                data['med_uso'] = st.radio("Faz uso?", ["Sim", "Não"], horizontal=True, index=1 if data.get('med_uso') == "Não" else 0, disabled=is_monitor)
                data['med_quais'] = st.text_input("Quais medicamentos?", value=data.get('med_quais', ''), disabled=is_monitor)
                c_med1, c_med2, c_med3 = st.columns(3)
                data['med_hor'] = c_med1.text_input("Horário", value=data.get('med_hor', ''), disabled=is_monitor)
                data['med_dos'] = c_med2.text_input("Dosagem", value=data.get('med_dos', ''), disabled=is_monitor)
                data['med_ini'] = c_med3.text_input("Início", value=data.get('med_ini', ''), disabled=is_monitor)

                st.divider()
                c_esf1, c_esf2 = st.columns(2)
                data['esf_urina'] = c_esf1.checkbox("Controla Urina", value=data.get('esf_urina', False), disabled=is_monitor)
                data['esf_fezes'] = c_esf2.checkbox("Controla Fezes", value=data.get('esf_fezes', False), disabled=is_monitor)
                data['esf_idade'] = st.text_input("Com qual idade controlou?", value=data.get('esf_idade', ''), disabled=is_monitor)
                data['sono'] = st.text_input("Dorme bem? Obs:", value=data.get('sono', ''), disabled=is_monitor)
                data['medico_ultimo'] = st.text_input("Última visita ao médico:", value=data.get('medico_ultimo', ''), disabled=is_monitor)

                st.markdown("**Atendimento Clínico Extraescolar**")
                clinicas_opts = ["APAE", "ARIL", "CEMA", "Família Azul", "CAPS", "Amb. Saúde Mental", "João Fischer D.A.", "João Fischer D.V."]
                prof_opts = ["Fonoaudiólogo", "Terapeuta Ocupacional", "Psicólogo", "Psicopedagogo", "Fisioterapeuta"]
                
                data['clinicas'] = st.multiselect("Selecione os atendimentos:", clinicas_opts + prof_opts, default=data.get('clinicas', []), disabled=is_monitor)
                data['clinicas_med_esp'] = st.text_input("Área médica (Especialidade):", value=data.get('clinicas_med_esp', ''), disabled=is_monitor)
                data['clinicas_nome'] = st.text_input("Nome da Clínica/Profissional:", value=data.get('clinicas_nome', ''), disabled=is_monitor)
                
                data['saude_obs_geral'] = st.text_area("Outras observações de saúde:", value=data.get('saude_obs_geral', ''), disabled=is_monitor)

                st.markdown("---")
                if not is_monitor:
                    if st.form_submit_button("💾 Salvar Dados de Saúde"):
                        save_student("CASO", data.get('nome'), data, "Saúde")

# --- ABA 5: COMPORTAMENTO ---
        with tabs[4]:
            with st.form("form_caso_comportamento") if not is_monitor else st.container():
                st.subheader("1.4 Compreensão da Família (Checklist)")
                
                checklist_items = [
                    "Relata fatos do dia a dia? Apresentando boa memória?",
                    "É organizado com seus pertences?",
                    "Aceita regras de forma tranquila?",
                    "Busca e aceita ajuda quando não sabe ou não consegue algo?",
                    "Aceita alterações no ambiente?",
                    "Tem algum medo?",
                    "Tem alguma mania?",
                    "Tem alguma área/assunto, brinquedo ou hiperfoco?",
                    "Prefere brincar sozinho ou com outras crianças? Tem amigos?",
                    "Qual a expectativa da família em relação à escolaridade da criança?"
                ]
                if 'checklist' not in data: data['checklist'] = {}
                
                # Pegamos um ID único do aluno para evitar o cache do Streamlit
                aluno_id = data.get('doc_uuid', data.get('nome', 'novo_aluno'))
                
                for i, item in enumerate(checklist_items):
                    st.markdown(f"**{item}**")
                    col_a, col_b = st.columns([1, 3])
                    
                    key_base = f"itemcomport_{i}" 
                    
                    # Lemos a opção e a observação que estão no JSON (banco de dados)
                    opt_salva = data['checklist'].get(f"{key_base}_opt", "Não")
                    obs_salva = data['checklist'].get(f"{key_base}_obs", "")
                    
                    # Atualizamos o dicionário com o widget, forçando o Streamlit a ler o index/value correto
                    data['checklist'][f"{key_base}_opt"] = col_a.radio(
                        "Opção", 
                        ["Sim", "Não"], 
                        key=f"rad_{aluno_id}_{i}", # ID único impede bug visual
                        horizontal=True, 
                        label_visibility="collapsed", 
                        index=0 if opt_salva == "Sim" else 1, 
                        disabled=is_monitor
                    )
                    
                    data['checklist'][f"{key_base}_obs"] = col_b.text_input(
                        "Obs:", 
                        value=obs_salva, 
                        key=f"obs_{aluno_id}_{i}", # ID único impede bug visual
                        disabled=is_monitor
                    )
                    st.divider()

                st.subheader("Dados da Entrevista")
                c_e1, c_e2, c_e3 = st.columns(3)
                data['entrevista_prof'] = c_e1.text_input("Prof. Responsável", value=data.get('entrevista_prof', ''), disabled=is_monitor)
                data['entrevista_resp'] = c_e2.text_input("Responsável info", value=data.get('entrevista_resp', ''), disabled=is_monitor)
                
                d_ent = data.get('entrevista_data')
                if isinstance(d_ent, str): 
                     try: d_ent = datetime.strptime(d_ent, '%Y-%m-%d').date()
                     except: d_ent = date.today()
                
                input_data = c_e3.date_input("Data", value=d_ent if d_ent else date.today(), format="DD/MM/YYYY", disabled=is_monitor)
                
                # Convertendo a data para string (YYYY-MM-DD) para salvar sem erros
                data['entrevista_data'] = input_data.strftime('%Y-%m-%d') 
                
                data['entrevista_extra'] = st.text_area("Outras informações relevantes:", value=data.get('entrevista_extra', ''), disabled=is_monitor)
                
                st.markdown("---")
                if not is_monitor:
                    if st.form_submit_button("💾 Salvar Comportamento"):
                        save_student("CASO", data.get('nome'), data, "Comportamento")
                        st.success("Dados de comportamento salvos com sucesso!")
                        
        # --- ABA 6: ASSINATURAS (NOVO) ---
        with tabs[5]:
            st.subheader("Assinaturas Digitais")
            st.caption(f"Código Único do Documento: {data.get('doc_uuid', 'Não gerado ainda')}")
            
            # Roles for Caso
            required_roles = []
            if data.get('entrevista_prof'): required_roles.append({'role': 'Prof. Entrevistador', 'name': data.get('entrevista_prof')})
            if data.get('entrevista_resp'): required_roles.append({'role': 'Responsável (Família)', 'name': data.get('entrevista_resp')})
            
            # Show list of signatories
            if required_roles:
                st.markdown("##### Profissionais/Responsáveis Citados")
                for r in required_roles:
                    st.write(f"- **{r['role']}:** {r['name']}")
            else:
                st.info("Nenhum profissional identificado automaticamente.")

            st.divider()
            
            # Current Signatures
            current_signatures = data.get('signatures', [])
            if current_signatures:
                st.success("✅ Documento assinado por:")
                for sig in current_signatures:
                    st.write(f"✍️ **{sig['name']}** ({sig.get('role', 'Profissional')}) em {sig['date']}")
            else:
                st.warning("Nenhuma assinatura registrada.")

            st.divider()
            
            user_name = st.session_state.get('usuario_nome', '')
            match_role = "Profissional"
            already_signed = any(s['name'] == user_name for s in current_signatures)
            
            if already_signed:
                st.info("Você já assinou este documento.")
            else:
                if st.button("🖊️ Assinar Digitalmente", key="btn_sign_caso"):
                    # Tenta descobrir o papel
                    for r in required_roles:
                        if user_name.strip().lower() in r['name'].strip().lower():
                            match_role = r['role']
                            break
                    
                    new_sig = {
                        "name": user_name,
                        "role": match_role,
                        "date": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
                        "hash": str(uuid.uuid4())
                    }
                    if 'signatures' not in data: data['signatures'] = []
                    data['signatures'].append(new_sig)
                    save_student("CASO", data.get('nome'), data, "Assinatura")
                    st.rerun()
# --- ABA 7: GERAR PDF (ESTUDO DE CASO) ---
        with tabs[6]:
            if not is_monitor:
                if st.button("💾 SALVAR ESTUDO DE CASO", type="primary"): 
                    save_student("CASO", data.get('nome', 'aluno'), data, "Completo")
            else:
                st.info("Modo Visualização.")

            if st.button("👁️ GERAR PDF"):
                # Registrar ação de gerar PDF
                log_action(data.get('nome'), "Gerou PDF", "Estudo de Caso")
                
                # --- FUNÇÕES AUXILIARES PARA TABELAS DINÂMICAS ---
                def calc_lines(pdf, text, w):
                    """Calcula quantas linhas o texto vai ocupar na largura especificada."""
                    if not text: return 1
                    lines = 0
                    for p in str(text).split('\n'):
                        words = p.split(' ')
                        line_w = 0
                        for word in words:
                            word_w = pdf.get_string_width(word + ' ')
                            if line_w + word_w > w - 2: # 2 de margem interna
                                lines += 1
                                line_w = word_w
                            else:
                                line_w += word_w
                        lines += 1
                    return max(1, lines)

                def draw_flex_row(pdf, col_data, line_h=6, font_size=9, fill_color=(240, 240, 240)):
                    """
                    Desenha uma linha garantindo que todas as colunas tenham a mesma altura.
                    col_data = [(largura, texto, estilo_fonte, alinhamento, preenchimento_bool), ...]
                    """
                    max_lines = 1
                    x_start_measure = pdf.get_x()
                    
                    # 1. Medir qual coluna precisará de mais linhas
                    for w, text, weight, align, fill in col_data:
                        pdf.set_font("Arial", weight, font_size)
                        real_w = w if w > 0 else (210 - 15 - x_start_measure)
                        lines = calc_lines(pdf, text, real_w)
                        if lines > max_lines: max_lines = lines
                        x_start_measure += real_w
                        
                    row_h = max_lines * line_h
                    
                    # 2. Quebra de página automática se a linha não couber
                    if pdf.get_y() + row_h > 275:
                        pdf.add_page()
                        
                    x_start = pdf.get_x()
                    y_start = pdf.get_y()
                    
                    # 3. Desenhar de fato a linha
                    for w, text, weight, align, fill in col_data:
                        real_w = w if w > 0 else (210 - 15 - x_start)
                        pdf.set_font("Arial", weight, font_size)
                        
                        if fill: pdf.set_fill_color(*fill_color)
                        else: pdf.set_fill_color(255, 255, 255)
                        
                        # Desenha a caixa (borda e fundo) com a altura MÁXIMA da linha
                        pdf.set_xy(x_start, y_start)
                        pdf.cell(real_w, row_h, "", border=1, fill=fill)
                        
                        # Centraliza verticalmente o texto se for apenas 1 linha em uma caixa grande
                        y_text = y_start + 1
                        if max_lines > 1 and calc_lines(pdf, text, real_w) == 1:
                            y_text = y_start + (row_h - line_h) / 2
                            
                        # Insere o texto com multi_cell (sem bordas, já que o cell atrás já fez a borda)
                        pdf.set_xy(x_start + 1, y_text)
                        pdf.multi_cell(real_w - 2, line_h, str(text), border=0, align=align)
                        
                        x_start += real_w
                        
                    # Move o cursor para o início da próxima linha
                    pdf.set_xy(15, y_start + row_h)

                # Cria PDF em Retrato ('P')
                pdf = OfficialPDF('P', 'mm', 'A4')
                pdf.add_page()
                pdf.set_margins(15, 15, 15)
                
                # SET SIGNATURE FOOTER
                pdf.set_signature_footer(data.get('signatures', []), data.get('doc_uuid', ''))
                
                # --- CABEÇALHO ---
                if os.path.exists("logo_prefeitura.png"): pdf.image("logo_prefeitura.png", 15, 10, 25)
                if os.path.exists("logo_escola.png"): pdf.image("logo_escola.png", 170, 6, 25)

                # Títulos Centralizados
                pdf.set_xy(0, 15); pdf.set_font("Arial", "B", 12)
                pdf.cell(210, 6, clean_pdf_text("PREFEITURA MUNICIPAL DE LIMEIRA"), 0, 1, 'C')
                pdf.cell(180, 6, clean_pdf_text("CEIEF RAFAEL AFFONSO LEITE"), 0, 1, 'C')
                pdf.ln(8)
                pdf.set_font("Arial", "B", 16); pdf.cell(0, 10, "ESTUDO DE CASO", 0, 1, 'C')
                pdf.ln(5)
                
                # --- 1.1 DADOS GERAIS ---
                pdf.section_title("1.1 DADOS GERAIS DO ESTUDANTE", width=0)
                pdf.ln(4)
                
                # 1.1.1 IDENTIFICAÇÃO
                pdf.set_fill_color(240, 240, 240)
                pdf.set_font("Arial", "B", 10); pdf.cell(0, 8, "1.1.1 - IDENTIFICAÇÃO", 1, 1, 'L', 1)
                
                draw_flex_row(pdf, [
                    (30, "Nome:", "B", "L", True),
                    (110, clean_pdf_text(data.get('nome', '')), "", "L", False),
                    (15, "D.N.:", "B", "C", True),
                    (25, clean_pdf_text(str(data.get('d_nasc', ''))), "", "C", False)
                ], line_h=7, font_size=10)
                
                draw_flex_row(pdf, [
                    (30, "Escolaridade:", "B", "L", True),
                    (25, clean_pdf_text(data.get('ano_esc', '')), "", "L", False),
                    (20, "Período:", "B", "C", True),
                    (20, clean_pdf_text(data.get('periodo', '')), "", "C", False),
                    (20, "Unidade:", "B", "C", True),
                    (65, clean_pdf_text(data.get('unidade', '')), "", "L", False)
                ], line_h=7, font_size=10)
                
                draw_flex_row(pdf, [
                    (30, "Endereço:", "B", "L", True),
                    (150, clean_pdf_text(data.get('endereco', '')), "", "L", False)
                ], line_h=7, font_size=10)

                draw_flex_row(pdf, [
                    (20, "Bairro:", "B", "L", True),
                    (70, clean_pdf_text(data.get('bairro', '')), "", "L", False),
                    (20, "Cidade:", "B", "C", True),
                    (70, clean_pdf_text(data.get('cidade', '')), "", "L", False)
                ], line_h=7, font_size=10)
                
                draw_flex_row(pdf, [
                    (20, "Telefone:", "B", "L", True),
                    (160, clean_pdf_text(data.get('telefones', '')), "", "L", False)
                ], line_h=7, font_size=10)
                
                # 1.1.2 DADOS FAMILIARES
                pdf.ln(4)
                pdf.set_font("Arial", "B", 10); pdf.cell(0, 8, "1.1.2 - DADOS FAMILIARES", 1, 1, 'L', 1)
                
                draw_flex_row(pdf, [
                    (20, "Pai:", "B", "L", True),
                    (80, clean_pdf_text(data.get('pai_nome', '')), "", "L", False),
                    (25, "Profissão:", "B", "C", True),
                    (55, clean_pdf_text(data.get('pai_prof', '')), "", "L", False)
                ], line_h=7, font_size=10)
                
                draw_flex_row(pdf, [
                    (20, "Mãe:", "B", "L", True),
                    (80, clean_pdf_text(data.get('mae_nome', '')), "", "L", False),
                    (25, "Profissão:", "B", "C", True),
                    (55, clean_pdf_text(data.get('mae_prof', '')), "", "L", False)
                ], line_h=7, font_size=10)
                
                # Irmãos
                pdf.ln(2)
                pdf.set_font("Arial", "B", 10); pdf.cell(0, 8, clean_pdf_text("Irmãos (Nome | Idade | Escolaridade)"), 1, 1, 'L', 1)
                for i, irmao in enumerate(data.get('irmaos', [])):
                    if irmao['nome']:
                        txt = f"{irmao['nome']}  |  {irmao['idade']}  |  {irmao['esc']}"
                        draw_flex_row(pdf, [(180, clean_pdf_text(txt), "", "L", False)], line_h=6, font_size=9)
                
                pdf.ln(2)
                draw_flex_row(pdf, [
                    (40, "Com quem mora:", "B", "L", True),
                    (140, clean_pdf_text(data.get('quem_mora', '')), "", "L", False)
                ], line_h=7, font_size=10)
                
                draw_flex_row(pdf, [
                    (40, "Convênio Médico:", "B", "L", True),
                    (50, clean_pdf_text(data.get('convenio')), "", "L", False),
                    (20, "Qual:", "B", "C", True),
                    (70, clean_pdf_text(data.get('convenio_qual')), "", "L", False)
                ], line_h=7, font_size=10)
                
                draw_flex_row(pdf, [
                    (40, "Benefício Social:", "B", "L", True),
                    (50, clean_pdf_text(data.get('social')), "", "L", False),
                    (20, "Qual:", "B", "C", True),
                    (70, clean_pdf_text(data.get('social_qual')), "", "L", False)
                ], line_h=7, font_size=10)

                # 1.1.3 HISTÓRIA ESCOLAR
                pdf.ln(4)
                pdf.set_font("Arial", "B", 10); pdf.cell(0, 8, clean_pdf_text("1.1.3 - HISTÓRIA ESCOLAR"), 1, 1, 'L', 1)
                
                draw_flex_row(pdf, [(50, "Idade entrou na escola:", "B", "L", True), (130, clean_pdf_text(data.get('hist_idade_entrou')), "", "L", False)], line_h=7, font_size=10)
                draw_flex_row(pdf, [(50, "Outras escolas:", "B", "L", True), (130, clean_pdf_text(data.get('hist_outra_escola')), "", "L", False)], line_h=7, font_size=10)
                draw_flex_row(pdf, [(50, "Motivo transferência:", "B", "L", True), (130, clean_pdf_text(data.get('hist_motivo_transf')), "", "L", False)], line_h=7, font_size=10)
                
                if data.get('hist_obs'):
                    pdf.ln(2)
                    pdf.set_font("Arial", "B", 10); pdf.cell(0, 6, "Observações Escolares:", 0, 1)
                    draw_flex_row(pdf, [(180, clean_pdf_text(data.get('hist_obs')), "", "L", False)], line_h=6, font_size=9)

                # --- 1.2 GESTAÇÃO, PARTO E DESENVOLVIMENTO ---
                pdf.add_page()
                pdf.section_title("1.2 GESTAÇÃO, PARTO E DESENVOLVIMENTO", width=0)
                pdf.ln(4)
                
                # A função print_data_row agora usa o Helper para não encavalar NADA!
                def print_data_row(label, value):
                    draw_flex_row(pdf, [
                        (80, clean_pdf_text(label), "B", "L", True),
                        (100, clean_pdf_text(str(value) if value else ""), "", "L", False)
                    ], line_h=6, font_size=9)

                rows_gest = [
                    ("Parentesco entre pais:", data.get('gest_parentesco')),
                    ("Doença/Trauma na gestação:", data.get('gest_doenca')),
                    ("Uso de substâncias (mãe):", data.get('gest_substancias')),
                    ("Uso de medicamentos (mãe):", data.get('gest_medicamentos')),
                    ("Ocorrência no parto:", data.get('parto_ocorrencia')),
                    ("Necessitou de incubadora:", data.get('parto_incubadora')),
                    ("Prematuro?", f"{data.get('parto_prematuro')}  |  UTI: {data.get('parto_uti')}"),
                    ("Tempo de gestação / Peso:", f"{data.get('dev_tempo_gest')}  /  {data.get('dev_peso')}"),
                    ("Desenvolvimento normal no 1º ano:", data.get('dev_normal_1ano')),
                    ("Apresentou atraso importante?", data.get('dev_atraso')),
                    ("Idade que andou / falou:", f"{data.get('dev_idade_andar')}  /  {data.get('dev_idade_falar')}"),
                    ("Possui diagnóstico?", data.get('diag_possui')),
                    ("Reação da família ao diagnóstico:", data.get('diag_reacao')),
                    ("Data / Origem do diagnóstico:", f"{data.get('diag_data')}  |  {data.get('diag_origem')}"),
                    ("Pessoa com deficiência na família:", data.get('fam_deficiencia')),
                    ("Pessoa com AH/SD na família:", data.get('fam_altas_hab'))
                ]
                
                for label, value in rows_gest:
                    print_data_row(label, value)

                # --- 1.3 INFORMAÇÕES SOBRE SAÚDE ---
                pdf.add_page()
                pdf.section_title("1.3 INFORMAÇÕES SOBRE SAÚDE", width=0)
                pdf.ln(4)
                
                saude_rows = [
                    ("Problemas de saúde:", data.get('saude_prob')),
                    ("Já necessitou de internação:", data.get('saude_internacao')),
                    ("Restrição/Seletividade alimentar:", data.get('saude_restricao')),
                    ("Uso de medicamentos controlados:", f"{data.get('med_uso')} - Quais: {data.get('med_quais')}"),
                    ("Horário / Dosagem / Início:", f"{data.get('med_hor')}  |  {data.get('med_dos')}  |  {data.get('med_ini')}"),
                    ("Qualidade do sono:", data.get('sono')),
                    ("Última visita ao médico:", data.get('medico_ultimo'))
                ]
                for label, value in saude_rows:
                    print_data_row(label, value)
                
                esf = []
                if data.get('esf_urina'): esf.append("Urina")
                if data.get('esf_fezes'): esf.append("Fezes")
                print_data_row("Controle de Esfíncter:", f"{', '.join(esf) if esf else 'Não'}  (Idade: {data.get('esf_idade')})")
                
                pdf.ln(4)
                pdf.set_font("Arial", "B", 10); pdf.set_fill_color(240, 240, 240)
                pdf.cell(0, 8, "Atendimentos Clínicos Extraescolares", 1, 1, 'L', 1)
                
                clins = data.get('clinicas', [])
                print_data_row("Realiza atendimento em:", ", ".join(clins) if clins else "Não realiza")
                print_data_row("Especialidade médica:", data.get('clinicas_med_esp'))
                print_data_row("Nome da Clínica/Profissional:", data.get('clinicas_nome'))
                
                if data.get('saude_obs_geral'):
                    pdf.ln(2)
                    pdf.set_font("Arial", "B", 9); pdf.cell(0, 6, "Outras observações de saúde:", 0, 1)
                    draw_flex_row(pdf, [(180, clean_pdf_text(data.get('saude_obs_geral')), "", "L", False)], line_h=5, font_size=9)

                # --- 1.4 COMPREENSÃO DA FAMÍLIA (CHECKLIST) ---
                pdf.add_page()
                pdf.section_title("1.4 COMPREENSÃO DA FAMÍLIA (CHECKLIST)", width=0)
                pdf.ln(4)
                
                # Cabeçalho da tabela de checklist adaptado
                draw_flex_row(pdf, [
                    (110, "PERGUNTA / ASPECTO OBSERVADO", "B", "C", True),
                    (25, "SIM/NÃO", "B", "C", True),
                    (45, "OBSERVAÇÕES DA FAMÍLIA", "B", "C", True)
                ], line_h=8, font_size=9, fill_color=(220, 220, 220))
                
                checklist_items = [
                    "Relata fatos do dia a dia? Apresentando boa memória?",
                    "É organizado com seus pertences?",
                    "Aceita regras de forma tranquila?",
                    "Busca e aceita ajuda quando não sabe ou não consegue algo?",
                    "Aceita alterações no ambiente?",
                    "Tem algum medo?",
                    "Tem alguma mania?",
                    "Tem alguma área/assunto, brinquedo ou hiperfoco?",
                    "Prefere brincar sozinho ou com outras crianças? Tem amigos?",
                    "Qual a expectativa da família em relação à escolaridade da criança?"
                ]
                
                for i, item in enumerate(checklist_items):
                    key_base = f"itemcomport_{i}"
                    opt = data.get('checklist', {}).get(f"{key_base}_opt", "Não")
                    obs = data.get('checklist', {}).get(f"{key_base}_obs", "")
                    
                    # Usa o Helper e automaticamente ajusta a altura das três colunas de uma vez
                    draw_flex_row(pdf, [
                        (110, clean_pdf_text(item), "", "L", False),
                        (25, clean_pdf_text(opt), "", "C", False),
                        (45, clean_pdf_text(obs), "", "L", False)
                    ], line_h=6, font_size=9)

                # --- FINALIZAÇÃO ---
                pdf.ln(5)
                pdf.set_font("Arial", "B", 10); pdf.set_fill_color(240, 240, 240)
                pdf.cell(0, 8, clean_pdf_text("OUTRAS INFORMAÇÕES RELEVANTES"), 1, 1, 'L', 1)
                
                draw_flex_row(pdf, [(180, clean_pdf_text(data.get('entrevista_extra', '---')), "", "L", False)], line_h=6, font_size=9)
                
                pdf.ln(10)
                if pdf.get_y() > 240: pdf.add_page()
                
                pdf.set_fill_color(240, 240, 240); pdf.set_font("Arial", "B", 10)
                pdf.cell(0, 8, "DADOS DA ENTREVISTA", 1, 1, 'L', 1)
                
                print_data_row("Responsável pelas informações:", data.get('entrevista_resp'))
                print_data_row("Profissional Entrevistador:", data.get('entrevista_prof'))
                print_data_row("Data da Entrevista:", str(data.get('entrevista_data', '')))
                
                pdf.ln(25) 
                
                y = pdf.get_y()
                pdf.line(20, y, 90, y); pdf.line(110, y, 190, y)
                pdf.set_font("Arial", "", 9)
                pdf.set_xy(20, y+2); pdf.cell(70, 5, "Assinatura do Responsável Legal", 0, 0, 'C')
                pdf.set_xy(110, y+2); pdf.cell(80, 5, "Assinatura do Docente/Gestor", 0, 1, 'C')

                st.session_state.pdf_bytes_caso = get_pdf_bytes(pdf)
                st.rerun()

            if 'pdf_bytes_caso' in st.session_state:
                st.download_button("📥 BAIXAR PDF ESTUDO DE CASO", st.session_state.pdf_bytes_caso, f"Caso_{data.get('nome','estudante')}.pdf", "application/pdf", type="primary")
        # --- ABA 8: HISTÓRICO ---
        with tabs[7]:
            st.subheader("Histórico de Atividades")
            st.caption("Registro de alterações, salvamentos e geração de documentos.")
            
            df_hist = safe_read("Historico", ["Data_Hora", "Aluno", "Usuario", "Acao", "Detalhes"])
            
            if not df_hist.empty and data.get('nome'):
                # Filtrar pelo aluno atual
                student_hist = df_hist[df_hist["Aluno"] == data.get('nome')]
                
                if not student_hist.empty:
                    # Ordenar por data (mais recente primeiro)
                    student_hist = student_hist.iloc[::-1]
                    st.dataframe(student_hist, use_container_width=True, hide_index=True)
                else:
                    st.info("Nenhum histórico encontrado para este aluno.")
            else:
                st.info("O histórico está vazio ou aluno não selecionado.")

    # --- PROTOCOLO DE CONDUTA ---
    elif doc_mode == "Protocolo de Conduta":
        st.markdown("""<div class="header-box"><div class="header-title">Protocolo de Conduta</div></div>""", unsafe_allow_html=True)
        st.markdown("""<style>div[data-testid="stFormSubmitButton"] > button {width: 100%; background-color: #dcfce7; color: #166534; border: 1px solid #166534;}</style>""", unsafe_allow_html=True)
        
        tabs = st.tabs(["📝 Preenchimento e Emissão", "🕒 Histórico"])
        
        data_conduta = st.session_state.data_conduta
        data_pei = st.session_state.data_pei
        
        with tabs[0]:
            with st.form("form_conduta") if not is_monitor else st.container():
                st.subheader("Configuração do Protocolo")
                st.caption("Preencha manualmente ou utilize o botão abaixo para importar informações do PEI do aluno, convertendo-as automaticamente para a 1ª pessoa.")
                
                if not is_monitor:
                    if st.form_submit_button("🔄 Preencher Automaticamente com dados do PEI"):
                        # Mapeamento e conversão simples para 1ª pessoa
                        if data_pei:
                            # Sobre Mim
                            defic = data_pei.get('defic_txt', '') or data_pei.get('neuro_txt', '')
                            data_conduta['conduta_sobre_mim'] = f"Olá, meu nome é {data_pei.get('nome', '')}. Tenho {data_pei.get('idade', '')} anos. Estou matriculado no {data_pei.get('ano_esc', '')} ano. {defic}"
                            
                            # Coisas que eu gosto
                            gostos = []
                            if data_pei.get('beh_interesses'): gostos.append(data_pei.get('beh_interesses'))
                            if data_pei.get('beh_objetos_gosta'): gostos.append(data_pei.get('beh_objetos_gosta'))
                            if data_pei.get('beh_atividades'): gostos.append(data_pei.get('beh_atividades'))
                            data_conduta['conduta_gosto'] = "\n".join(gostos)
                            
                            # Coisas que não gosto
                            nao_gosto = []
                            if data_pei.get('beh_objetos_odeia'): nao_gosto.append(data_pei.get('beh_objetos_odeia'))
                            if data_pei.get('beh_gatilhos'): nao_gosto.append(f"Fico chateado/nervoso quando: {data_pei.get('beh_gatilhos')}")
                            data_conduta['conduta_nao_gosto'] = "\n".join(nao_gosto)
                            
                            # Como me comunico
                            data_conduta['conduta_comunico'] = f"Eu me comunico: {data_pei.get('com_tipo', '')}. {data_pei.get('com_alt_espec', '')}"
                            
                            # Como me ajudar
                            ajuda = []
                            if data_pei.get('beh_crise_regula'): ajuda.append(f"Para me regular: {data_pei.get('beh_crise_regula')}")
                            if data_pei.get('beh_calmo'): ajuda.append(f"O que me acalma: {data_pei.get('beh_calmo')}")
                            data_conduta['conduta_ajuda'] = "\n".join(ajuda)
                            
                            # Habilidades
                            habs = []
                            if data_pei.get('hig_banheiro'): habs.append(f"Uso do banheiro: {data_pei.get('hig_banheiro')}")
                            if data_pei.get('hig_dentes'): habs.append(f"Escovação: {data_pei.get('hig_dentes')}")
                            if data_pei.get('dev_tarefas'): habs.append(f"Tarefas: {data_pei.get('dev_tarefas')}")
                            data_conduta['conduta_habilidades'] = "\n".join(habs)
                            
                            st.success("Dados importados do PEI com sucesso! Revise abaixo.")
                        else:
                            st.warning("Dados do PEI não encontrados para este aluno.")

                # Campos do Formulário
                c1, c2 = st.columns([3, 1])
                data_conduta['nome'] = c1.text_input("Nome", value=data_pei.get('nome', data_conduta.get('nome','')), disabled=True)
                
                d_val = data_conduta.get('nasc') or data_pei.get('nasc')
                if isinstance(d_val, str): 
                    try: d_val = datetime.strptime(d_val, '%Y-%m-%d').date()
                    except: d_val = date.today()
                data_conduta['nasc'] = c2.date_input("Nascimento", value=d_val if d_val else date.today(), format="DD/MM/YYYY", disabled=is_monitor)
                
                data_conduta['ano_esc'] = st.text_input("Ano de Escolaridade", value=data_pei.get('ano_esc', data_conduta.get('ano_esc','')), disabled=is_monitor)
                
                st.divider()
                
                c_g, c_s = st.columns(2)
                data_conduta['conduta_gosto'] = c_g.text_area("Coisas que eu gosto (Laranja)", value=data_conduta.get('conduta_gosto', ''), height=150, disabled=is_monitor)
                data_conduta['conduta_sobre_mim'] = c_s.text_area("Sobre mim (Verde)", value=data_conduta.get('conduta_sobre_mim', ''), height=150, disabled=is_monitor)
                
                c_ng, c_com = st.columns(2)
                data_conduta['conduta_nao_gosto'] = c_ng.text_area("Coisas que eu não gosto (Vermelho)", value=data_conduta.get('conduta_nao_gosto', ''), height=150, disabled=is_monitor)
                data_conduta['conduta_comunico'] = c_com.text_area("Como me comunico (Roxo)", value=data_conduta.get('conduta_comunico', ''), height=150, disabled=is_monitor)
                
                c_aj, c_hab = st.columns(2)
                data_conduta['conduta_ajuda'] = c_aj.text_area("Como me ajudar (Azul)", value=data_conduta.get('conduta_ajuda', ''), height=150, disabled=is_monitor)
                data_conduta['conduta_habilidades'] = c_hab.text_area("Habilidades / Eu posso (Amarelo)", value=data_conduta.get('conduta_habilidades', ''), height=150, disabled=is_monitor)

                st.markdown("---")
                c_save, c_pdf = st.columns(2)
                
                if not is_monitor:
                    if c_save.form_submit_button("💾 Salvar Protocolo"):
                        save_student("CONDUTA", data_conduta.get('nome', 'aluno'), data_conduta, "Protocolo")
                
                # Check button type depending on context (Form vs Container)
                gen_pdf = False
                if is_monitor:
                    if c_pdf.button("👁️ Gerar PDF"): gen_pdf = True
                else:
                    if c_pdf.form_submit_button("👁️ Gerar PDF"): gen_pdf = True

                if gen_pdf:
                    log_action(data_conduta.get('nome'), "Gerou PDF", "Protocolo de Conduta")
                    
                    pdf = OfficialPDF('P', 'mm', 'A4')
                    pdf.add_page(); pdf.set_margins(10, 10, 10)
                    
                    # SET SIGNATURE FOOTER
                   # pdf.set_signature_footer(data.get('signatures', []), data.get('doc_uuid', ''))
                    
                    # --- CABEÇALHO ---
                    if os.path.exists("logo_prefeitura.png"): pdf.image("logo_prefeitura.png", 10, 8, 20)
                    pdf.set_xy(35, 10); pdf.set_font("Arial", "", 12)
                    pdf.cell(0, 6, clean_pdf_text("Secretaria Municipal de"), 0, 1)
                    pdf.set_x(35); pdf.set_font("Arial", "B", 16)
                    pdf.cell(0, 8, clean_pdf_text("EDUCAÇÃO"), 0, 1)
                    
                    # Box Titulo
                    pdf.set_xy(130, 8)
                    pdf.set_font("Arial", "", 12)
                    pdf.cell(70, 10, "Protocolo de conduta", 1, 1, 'C')
                    
                    # --- IDENTIFICAÇÃO (FOTO E DADOS) ---
                    start_y = 35
                    
                    # FOTO (Placeholder circular visual - quadrado com label por simplicidade do FPDF)
                    pdf.set_xy(10, start_y)
                    # Tenta carregar foto do PEI se não tiver no conduta (usa mesma ref)
                    foto_b64 = data_pei.get('foto_base64')
                    if foto_b64:
                        try:
                            img_data = base64.b64decode(foto_b64)
                            with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp_file:
                                tmp_file.write(img_data)
                                tmp_path = tmp_file.name
                            pdf.image(tmp_path, 15, start_y, 40, 50) # Imagem retangular
                            os.unlink(tmp_path)
                        except:
                            pdf.rect(15, start_y, 40, 50)
                            pdf.set_xy(15, start_y+20); pdf.set_font("Arial", "", 8); pdf.cell(40, 5, "ERRO FOTO", 0, 0, 'C')
                    else:
                        pdf.rect(15, start_y, 40, 50) # Moldura
                        pdf.set_xy(15, start_y+20); pdf.set_font("Arial", "", 8); pdf.cell(40, 5, "FOTO DO ESTUDANTE", 0, 0, 'C')
                    
                    # Campos ao lado da foto
                    pdf.set_font("Arial", "", 10)
                    
                    # Nome (Borda Vermelha)
                    pdf.set_draw_color(255, 69, 0) # Red
                    pdf.set_line_width(0.8)
                    pdf.set_xy(70, start_y)
                    pdf.cell(130, 8, clean_pdf_text(f"Meu nome: {data_conduta.get('nome','')}"), 1, 1, 'L')
                    
                    # Data Nasc (Borda Azul)
                    pdf.set_draw_color(0, 191, 255) # Cyan/Blue
                    pdf.set_xy(70, start_y + 12)
                    pdf.multi_cell(40, 6, clean_pdf_text(f"Data de\nNascimento:\n{str(data_conduta.get('nasc',''))}"), 1, 'C')
                    
                    # Ano escolar (Borda Rosa)
                    pdf.set_draw_color(255, 105, 180) # Pink
                    pdf.set_xy(115, start_y + 12)
                    pdf.multi_cell(50, 9, clean_pdf_text(f"Ano de escolaridade:\n{data_conduta.get('ano_esc','')}") , 1, 'C')
                    

        # --- CONFIGURAÇÃO PARA CARTAZ DE PÁGINA ÚNICA ---
                    # Desliga a quebra de página automática para termos controle total do espaço
                    pdf.set_auto_page_break(False)
                    
                    # --- CAIXAS DE CONTEÚDO ---
                    
                    def draw_colored_box(x, y, w, target_h, r, g, b, title, content):
                        texto_limpo = clean_pdf_text(str(content) if content else "")
                        pdf.set_font("Arial", "", 9)
                        
                        w_text = w - 4 
                        line_height = 5
                        linhas = 0
                        
                        for paragrafo in texto_limpo.split('\n'):
                            largura = pdf.get_string_width(paragrafo)
                            if largura == 0:
                                linhas += 1
                            else:
                                linhas += int(largura / w_text) + 1
                                
                        h_texto = 8 + (max(1, linhas) * line_height) + 4
                        h_final = max(target_h, h_texto)
                        
                        # Removemos o add_page() manual daqui para forçar a ficar na mesma página
                            
                        # Desenha o Retângulo Externo
                        pdf.set_draw_color(r, g, b)
                        pdf.set_line_width(0.8)
                        pdf.rect(x, y, w, h_final)
                        
                        # Imprime o Título
                        pdf.set_xy(x, y+2)
                        pdf.set_text_color(0, 0, 0)
                        pdf.set_font("Arial", "B", 10)
                        pdf.cell(w, 5, clean_pdf_text(title), 0, 1, 'C')
                        
                        # Imprime o Conteúdo
                        pdf.set_xy(x+2, y+8)
                        pdf.set_font("Arial", "", 9)
                        pdf.multi_cell(w_text, line_height, texto_limpo, 0, 'L')
                        
                        # Retorna a posição da próxima caixa (com 3mm de respiro para alinhamento perfeito)
                        return y + h_final + 3

                    # --- LÓGICA DE ORGANIZAÇÃO PARA PREENCHER O A4 ---
                    
                    y_esquerdo = 90 
                    y_direito = 75 
                    
                    # LADO DIREITO (Altura ideal reduzida para 65mm para não bater na margem limite)
                    y_direito = draw_colored_box(100, y_direito, 100, 65, 154, 205, 50, "Sobre mim", data_conduta.get('conduta_sobre_mim', ''))
                    y_direito = draw_colored_box(130, y_direito, 70, 65, 255, 69, 0, "Coisas que eu não gosto", data_conduta.get('conduta_nao_gosto', ''))
                    y_direito = draw_colored_box(130, y_direito, 70, 65, 255, 215, 0, "Habilidades (eu posso...)", data_conduta.get('conduta_habilidades', ''))
                    
                    # LADO ESQUERDO (Altura ideal reduzida para 60mm)
                    y_esquerdo = draw_colored_box(10, y_esquerdo, 85, 60, 255, 165, 0, "Coisas que eu gosto", data_conduta.get('conduta_gosto', ''))
                    y_esquerdo = draw_colored_box(10, y_esquerdo, 110, 60, 147, 112, 219, "Como me comunico", data_conduta.get('conduta_comunico', ''))
                    y_esquerdo = draw_colored_box(10, y_esquerdo, 110, 60, 0, 191, 255, "Como me ajudar", data_conduta.get('conduta_ajuda', ''))

                    # Religando a quebra de página por precaução para não afetar o resto do app
                    pdf.set_auto_page_break(True, margin=15)

                    st.session_state.pdf_bytes_conduta = get_pdf_bytes(pdf)
                    st.rerun()

            if 'pdf_bytes_conduta' in st.session_state:
                st.download_button("📥 BAIXAR PROTOCOLO PDF", st.session_state.pdf_bytes_conduta, f"Conduta_{data_conduta.get('nome','aluno')}.pdf", "application/pdf", type="primary")
        # --- ABA 7: HISTÓRICO ---
        with tabs[1]:
            st.subheader("Histórico de Atividades")
            st.caption("Registro de alterações, salvamentos e geração de documentos.")
            
            df_hist = safe_read("Historico", ["Data_Hora", "Aluno", "Usuario", "Acao", "Detalhes"])
            
            # CORREÇÃO DE BUG: Usar data_conduta ao invés de data
            if not df_hist.empty and data_conduta.get('nome'):
                # Filtrar pelo aluno atual
                student_hist = df_hist[df_hist["Aluno"] == data_conduta.get('nome')]
                
                if not student_hist.empty:
                    # Ordenar por data (mais recente primeiro)
                    student_hist = student_hist.iloc[::-1]
                    st.dataframe(student_hist, use_container_width=True, hide_index=True)
                else:
                    st.info("Nenhum histórico encontrado para este aluno.")
            else:
                st.info("O histórico está vazio ou aluno não selecionado.")


    # --- AVALIAÇÃO PEDAGÓGICA ---
    elif doc_mode == "Avaliação de Apoio":
        st.markdown("""<div class="header-box"><div class="header-title">Avaliação Pedagógica: Apoio Escolar</div></div>""", unsafe_allow_html=True)
        st.markdown("""<style>div[data-testid="stFormSubmitButton"] > button {width: 100%; background-color: #dcfce7; color: #166534; border: 1px solid #166534;}</style>""", unsafe_allow_html=True)
        
        tabs = st.tabs(["📝 Preenchimento e Emissão", "🕒 Histórico"])
        
        # Inicialização de variáveis de estado se não existirem
        if 'data_avaliacao' not in st.session_state: st.session_state.data_avaliacao = {}
        if 'data_pei' not in st.session_state: st.session_state.data_pei = {}
        if 'data_case' not in st.session_state: st.session_state.data_case = {}
        
        data_aval = st.session_state.data_avaliacao
        data_pei = st.session_state.data_pei
        data_caso = st.session_state.data_case
        
        # --- DEFINIÇÃO DAS LISTAS DE OPÇÕES (GLOBAL PARA O CONTEXTO) ---
        defs_opts = ["Deficiência auditiva/surdez", "Deficiência física", "Deficiência intelectual", "Deficiência múltipla", "Deficiência visual", "Transtorno do Espectro Autista", "Síndrome de Down"]
        
        opts_alim = ["É independente.", "Necessita de apoio parcial.", "Necessita de apoio total."]
        opts_hig = ["É independente.", "Usa fralda.", "Necessita de apoio parcial.", "Necessita de apoio total."]
        opts_loc = ["é independente.", "cai ou tropeça com frequência.", "faz uso de cadeira de rodas de forma independente", "faz uso de cadeira de rodas, necessitando ser conduzido.", "possui prótese/órtese.", "faz uso de andador.", "faz uso de bengala."]
        
        opts_comp = [
            "Demonstra comportamento adequado em relação às situações escolares cotidianas (sala de aula, refeitório, quadra etc).",
            "Apresenta alguns comportamentos inadequados (choro, recusa verbal, se jogar no chão) em momentos específicos , mas a recuperação é rápida.",
            "diariamente apresenta comportamentos inadequados que envolvem choro, recusa verbal, birras, saídas sem autorização, correr incontido não atendimento às solicitações dos docentes e funcionários.",
            "Frequentemente a criança emite comportamento inadequado severo que é perigoso a si própria ou outras pessoas (ex: agressões, autolesivos)."
        ]
        
        opts_part = [
            "participa de atividades em grupo da rotina escolar, interagindo com os estudantes",
            "é capaz de participar de atividades em grupo somente em momentos de curta duração",
            "não é capaz de participar de atividades em grupo de forma autônoma, dependendo de apoio para essa interação",
            "Mesmo com apoio, não é capaz de participar de atividades em grupo."
        ]
        
        opts_int = ["Adequada com as crianças e adultos.", "Satisfatória.", "Inadequada.", "Outros"]
        
        opts_rot = [
            "Compreende e atende as orientações oferecidas pelo docente de forma autônoma",
            "Precisa de intervenções pontuais do docente para compreender e atender as orientações.",
            "Mesmo com apoio apresenta severas dificuldades quanto à compreensão para atendimento de solicitações."
        ]
        
        opts_ativ = [
            "não há necessidade de flexibilização curricular",
            "precisa de flexibilização curricular em relação à metodologia de ensino, mantendo-se os conteúdos previstos para o ano de escolaridade",
            "precisa de flexibilização curricular em relação à metodologia de ensino e ao conteúdo curricular, adequando às potencialidades do estudantes",
            "há a necessidade de um currículo funcional, envolvendo as atividades de vida prática e diária."
        ]
        
        opts_at_sust = [
            "Mantém atenção por longo período de tempo.",
            "Mantém atenção por longo período de tempo com apoio.",
            "Não mantém atenção por longo período de tempo."
        ]
        
        opts_at_div = [
            "Mantém atenção em dois estímulos diferentes.",
            "Mantém atenção em dois estímulos diferentes em algumas situações.",
            "Não mantém atenção em dois estímulos differentes."
        ]
        
        opts_at_sel = [
            "Mantém atenção na tarefa ignorando estímulos externos.",
            "Mantém atenção na tarefa ignorando estímulos externos com apoio.",
            "Não mantém atenção na tarefa com a presença de outros"
        ]
        
        opts_ling = [
            "Faz uso de palavras para se comunicar, expressando seus pensamentos e desejos.",
            "Faz uso de palavras para se comunicar, apresentando trocas fonéticas orais.",
            "Utiliza palavras e frases desconexas, não conseguindo se expressar.",
            "Não faz uso de palavras para se comunicar, expressando seus desejos por meio de gestos e comportamentos",
            "Não faz uso de palavras e de gestos para se comunicar."
        ]

        with tabs[0]:
            with st.form("form_avaliacao") if not is_monitor else st.container():
                st.subheader("Configuração da Avaliação")
                st.caption("Utilize o botão abaixo para importar informações já preenchidas no PEI e Estudo de Caso.")
                
                if not is_monitor:
                    if st.form_submit_button("🔄 Preencher Automaticamente"):
                        if data_pei or data_caso:
                            data_aval['nome'] = data_pei.get('nome') or data_caso.get('nome', '')
                            data_aval['nasc'] = data_pei.get('nasc') or data_caso.get('d_nasc', '')
                            data_aval['ano_esc'] = data_pei.get('ano_esc') or data_caso.get('ano_esc', '')
                            
                            # --- CORREÇÃO 1: FILTRAGEM INTELIGENTE DE DIAGNÓSTICO ---
                            diag_tipo_pei = data_pei.get('diag_tipo', [])
                            # Mantém apenas os que batem exatamente com as opções da avaliação
                            data_aval['defic_chk'] = [d for d in diag_tipo_pei if d in defs_opts]
                            
                            # Puxa os textos descritivos do PEI para o campo "Outra"
                            descricoes_outras = []
                            if "Deficiência" in diag_tipo_pei and data_pei.get('defic_txt'):
                                descricoes_outras.append(data_pei.get('defic_txt'))
                            if "Transtorno do Neurodesenvolvimento" in diag_tipo_pei and data_pei.get('neuro_txt'):
                                descricoes_outras.append(data_pei.get('neuro_txt'))
                            if "Transtornos Aprendizagem" in diag_tipo_pei and data_pei.get('aprend_txt'):
                                descricoes_outras.append(data_pei.get('aprend_txt'))
                            
                            if descricoes_outras:
                                data_aval['defic_outra'] = " / ".join(descricoes_outras)
                            # --------------------------------------------------------
                            
                            aspectos = []
                            if data_pei.get('prof_poli'): aspectos.append(f"Polivalente: {data_pei.get('prof_poli')}")
                            if data_pei.get('prof_aee'): aspectos.append(f"AEE: {data_pei.get('prof_aee')}")
                            if data_pei.get('flex_matrix'): aspectos.append("Possui flexibilização curricular registrada no PEI.")
                            data_aval['aspectos_gerais'] = "\n".join(aspectos)
                            
                            if data_pei.get('beh_autonomia_agua') == 'Sim': data_aval['alim_nivel'] = opts_alim[0]
                            if data_pei.get('hig_banheiro') == 'Sim': data_aval['hig_nivel'] = opts_hig[0]
                            if data_pei.get('loc_reduzida') == 'Não': data_aval['loc_nivel'] = [opts_loc[0]]
                            
                            st.success("Dados importados com sucesso!")
                        else:
                            st.warning("Sem dados prévios para importar.")

                # --- CAMPOS DO FORMULÁRIO ---
                st.markdown("### Identificação")
                c_nom, c_ano = st.columns([3, 1])
                data_aval['nome'] = c_nom.text_input("Estudante", value=data_aval.get('nome', ''), disabled=True)
                data_aval['ano_esc'] = c_ano.text_input("Ano Escolaridade", value=data_aval.get('ano_esc', ''), disabled=is_monitor)
                
                st.markdown("**Deficiências (Marque as opções):**")
                
                # --- CORREÇÃO 2: BLINDAGEM DO WIDGET MULTISELECT ---
                valores_salvos = data_aval.get('defic_chk', [])
                if not isinstance(valores_salvos, list): 
                    valores_salvos = []
                # Garante que os defaults passados ao Streamlit existam em defs_opts
                valores_validos = [v for v in valores_salvos if v in defs_opts]
                
                data_aval['defic_chk'] = st.multiselect("Selecione:", defs_opts, default=valores_validos, disabled=is_monitor)
                # ---------------------------------------------------
                
                data_aval['defic_outra'] = st.text_input("Outra:", value=data_aval.get('defic_outra', ''), disabled=is_monitor)
                
                st.markdown("---")
                st.markdown("### Aspectos Gerais da Vida Escolar")
                
                with st.expander("Parte I - Habilidades de Vida Diária", expanded=True):
                    c_a, c_h = st.columns(2)
                    with c_a:
                        st.markdown("**1. Alimentação**")
                        idx_alim = opts_alim.index(data_aval.get('alim_nivel')) if data_aval.get('alim_nivel') in opts_alim else 0
                        data_aval['alim_nivel'] = st.radio("Nível Alimentação", opts_alim, index=idx_alim, key="rad_alim", disabled=is_monitor)
                        data_aval['alim_obs'] = st.text_input("Obs Alimentação:", value=data_aval.get('alim_obs', ''), disabled=is_monitor)
                    
                    with c_h:
                        st.markdown("**2. Higiene**")
                        idx_hig = opts_hig.index(data_aval.get('hig_nivel')) if data_aval.get('hig_nivel') in opts_hig else 0
                        data_aval['hig_nivel'] = st.radio("Nível Higiene", opts_hig, index=idx_hig, key="rad_hig", disabled=is_monitor)
                        data_aval['hig_obs'] = st.text_input("Obs Higiene:", value=data_aval.get('hig_obs', ''), disabled=is_monitor)
                    
                    st.markdown("**3. Locomoção (Selecione todos que se aplicam)**")
                    data_aval['loc_nivel'] = st.multiselect("Itens:", opts_loc, default=data_aval.get('loc_nivel', []), disabled=is_monitor)
                    data_aval['loc_obs'] = st.text_input("Obs Locomoção:", value=data_aval.get('loc_obs', ''), disabled=is_monitor)

                with st.expander("Parte II - Habilidades Sociais e de Interação"):
                    st.markdown("**4. Comportamento**")
                    idx_comp = opts_comp.index(data_aval.get('comportamento')) if data_aval.get('comportamento') in opts_comp else 0
                    data_aval['comportamento'] = st.radio("Nível Comportamento", opts_comp, index=idx_comp, disabled=is_monitor)
                    data_aval['comp_obs'] = st.text_input("Obs Comportamento:", value=data_aval.get('comp_obs', ''), disabled=is_monitor)
                    
                    st.divider()
                    st.markdown("**5. Participação em Grupo**")
                    idx_part = opts_part.index(data_aval.get('part_grupo')) if data_aval.get('part_grupo') in opts_part else 0
                    data_aval['part_grupo'] = st.radio("Nível Participação", opts_part, index=idx_part, disabled=is_monitor)
                    data_aval['part_obs'] = st.text_input("Obs Participação:", value=data_aval.get('part_obs', ''), disabled=is_monitor)
                    
                    st.divider()
                    st.markdown("**6. Interação**")
                    idx_int = opts_int.index(data_aval.get('interacao')) if data_aval.get('interacao') in opts_int else 0
                    data_aval['interacao'] = st.radio("Nível Interação", opts_int, index=idx_int, disabled=is_monitor)
                    if data_aval['interacao'] == "Outros":
                        data_aval['interacao_outros'] = st.text_input("Especifique (Interação):", value=data_aval.get('interacao_outros', ''), disabled=is_monitor)

                with st.expander("Parte III - Habilidades Pedagógicas"):
                    st.markdown("**7. Rotina Sala de Aula**")
                    idx_rot = opts_rot.index(data_aval.get('rotina')) if data_aval.get('rotina') in opts_rot else 0
                    data_aval['rotina'] = st.radio("Nível Rotina", opts_rot, index=idx_rot, disabled=is_monitor)
                    data_aval['rotina_obs'] = st.text_input("Obs Rotina:", value=data_aval.get('rotina_obs', ''), disabled=is_monitor)
                    
                    st.divider()
                    st.markdown("**8. Atividades Pedagógicas**")
                    idx_ativ = opts_ativ.index(data_aval.get('ativ_pedag')) if data_aval.get('ativ_pedag') in opts_ativ else 0
                    data_aval['ativ_pedag'] = st.radio("Nível Atividades", opts_ativ, index=idx_ativ, disabled=is_monitor)

                with st.expander("Parte IV - Habilidades de Comunicação e Atenção"):
                    c_com1, c_com2 = st.columns(2)
                    with c_com1:
                        st.markdown("**9. Atenção Sustentada**")
                        idx_as = opts_at_sust.index(data_aval.get('atencao_sust')) if data_aval.get('atencao_sust') in opts_at_sust else 0
                        data_aval['atencao_sust'] = st.radio("Sustentada", opts_at_sust, index=idx_as, key="at_sust", disabled=is_monitor)
                        
                        st.markdown("**11. Atenção Seletiva**")
                        idx_asel = opts_at_sel.index(data_aval.get('atencao_sel')) if data_aval.get('atencao_sel') in opts_at_sel else 0
                        data_aval['atencao_sel'] = st.radio("Seletiva", opts_at_sel, index=idx_asel, key="at_sel", disabled=is_monitor)
                    
                    with c_com2:
                        st.markdown("**10. Atenção Dividida**")
                        idx_ad = opts_at_div.index(data_aval.get('atencao_div')) if data_aval.get('atencao_div') in opts_at_div else 0
                        data_aval['atencao_div'] = st.radio("Dividida", opts_at_div, index=idx_ad, key="at_div", disabled=is_monitor)
                    
                    st.divider()
                    st.markdown("**12. Linguagem (Marque todas que se aplicam)**")
                    data_aval['linguagem'] = st.multiselect("Linguagem:", opts_ling, default=data_aval.get('linguagem', []), disabled=is_monitor)
                    data_aval['ling_obs'] = st.text_input("Obs Linguagem:", value=data_aval.get('ling_obs', ''), disabled=is_monitor)

                st.markdown("### Conclusão e Responsáveis")
                data_aval['conclusao_nivel'] = st.selectbox("Nível de Apoio Concluído", ["Não necessita de apoio", "Nível 1", "Nível 2", "Nível 3"], index=0, disabled=is_monitor)
                data_aval['apoio_existente'] = st.text_input("Se este apoio já é oferecido, explicitar aqui:", value=data_aval.get('apoio_existente', ''), disabled=is_monitor)
                
                c_resp1, c_resp2 = st.columns(2)
                data_aval['resp_sala'] = c_resp1.text_input("Prof. Sala Regular", value=data_aval.get('resp_sala', ''), disabled=is_monitor)
                data_aval['resp_arte'] = c_resp2.text_input("Prof. Arte", value=data_aval.get('resp_arte', ''), disabled=is_monitor)
                data_aval['resp_ef'] = c_resp1.text_input("Prof. Ed. Física", value=data_aval.get('resp_ef', ''), disabled=is_monitor)
                data_aval['resp_ee'] = c_resp2.text_input("Prof. Ed. Especial", value=data_aval.get('resp_ee', ''), disabled=is_monitor)
                data_aval['resp_dir'] = c_resp1.text_input("Direção Escolar", value=data_aval.get('resp_dir', ''), disabled=is_monitor)
                data_aval['resp_coord'] = c_resp2.text_input("Coordenação", value=data_aval.get('resp_coord', ''), disabled=is_monitor)
                
                data_aval['data_emissao'] = st.date_input("Data Emissão", value=date.today(), format="DD/MM/YYYY", disabled=is_monitor)

                st.markdown("---")
                c_sv, c_pd = st.columns(2)
                if not is_monitor:
                    if c_sv.form_submit_button("💾 Salvar Avaliação"):
                        save_student("AVALIACAO", data_aval.get('nome', 'aluno'), data_aval, "Avaliação")
                
                gen_pdf_aval = False
                if is_monitor:
                    if c_pd.button("👁️ Gerar PDF Avaliação"): gen_pdf_aval = True
                else:
                    if c_pd.form_submit_button("👁️ Gerar PDF Avaliação"): gen_pdf_aval = True

                if gen_pdf_aval:
                    # --- PDF GENERATION EXPERT MODE ---
                    pdf = OfficialPDF('P', 'mm', 'A4')
                    pdf.add_page(); pdf.set_margins(15, 15, 15)
                    
                    # SET SIGNATURE FOOTER
                    #pdf.set_signature_footer(data.get('signatures', []), data.get('doc_uuid', ''))
                    
                    # 1. HEADER (FIXED CEIEF RAFAEL AFFONSO LEITE)
                    if os.path.exists("logo_prefeitura.png"): pdf.image("logo_prefeitura.png", 15, 10, 25)
                    if os.path.exists("logo_escola.png"): pdf.image("logo_escola.png", 170, 6, 25)

                    pdf.set_xy(0, 15); pdf.set_font("Arial", "B", 12)
                    pdf.cell(210, 6, clean_pdf_text("PREFEITURA MUNICIPAL DE LIMEIRA"), 0, 1, 'C')
                    pdf.cell(180, 6, clean_pdf_text("CEIEF RAFAEL AFFONSO LEITE"), 0, 1, 'C')
                    pdf.ln(8)
                    pdf.set_font("Arial", "B", 12); pdf.cell(0, 10, clean_pdf_text("AVALIAÇÃO PEDAGÓGICA: APOIO ESCOLAR PARA ESTUDANTE COM DEFICIÊNCIA"), 0, 1, 'C')
                    pdf.ln(5)
                    
                    # 2. IDENTIFICATION
                    pdf.set_font("Arial", "B", 10); pdf.cell(20, 6, "Estudante:", 0, 0)
                    pdf.set_font("Arial", "", 10); pdf.cell(100, 6, clean_pdf_text(data_aval.get('nome', '')), "B", 0)
                    pdf.set_font("Arial", "B", 10); pdf.cell(35, 6, "Ano escolaridade:", 0, 0)
                    pdf.set_font("Arial", "", 10); pdf.cell(0, 6, clean_pdf_text(data_aval.get('ano_esc', '')), "B", 1)
                    pdf.ln(4)
                    
                    # 3. DEFICIENCIES
                    pdf.set_font("Arial", "", 9)
                    selected_defs = data_aval.get('defic_chk', [])
                    
                    def draw_check_option_simple(pdf, text, checked):
                        pdf.set_x(15) 
                        x, y = pdf.get_x(), pdf.get_y()
                        pdf.set_draw_color(0,0,0)
                        pdf.rect(x, y + 1, 3, 3)
                        if checked:
                            pdf.line(x, y + 1, x + 3, y + 4)
                            pdf.line(x, y + 4, x + 3, y + 1)
                        pdf.set_xy(x + 5, y)
                        # Width 175 ensures it ends at 15+5+175 = 195 (Right margin boundary)
                        pdf.multi_cell(175, 5, clean_pdf_text(text), 0, 'L')

                    if selected_defs:
                        for d in selected_defs:
                            draw_check_option_simple(pdf, d, True)
                        if data_aval.get('defic_outra'):
                            draw_check_option_simple(pdf, f"Outra: {data_aval.get('defic_outra')}", True)
                    else:
                        pdf.cell(0, 5, clean_pdf_text("Nenhuma deficiência selecionada."), 0, 1)
                    pdf.ln(3)
                    
                    # 4. LEGAL TEXT (INTEGRAL) - Justified
                    pdf.set_font("Arial", "B", 10)
                    pdf.cell(0, 6, clean_pdf_text("PRESSUPOSTOS LEGAIS:"), 0, 1, 'L')
                    pdf.set_font("Arial", "", 8)
                    
                    # Full width (0) uses 180mm. 
                    pdf.multi_cell(0, 4, clean_pdf_text("1- Lei nº 12.764/2012, em seu artigo 3º que trata dos direitos da pessoa com transtorno do espectro autista indica:"), 0, 'J')
                    
                    pdf.set_x(25)
                    # Indent 25 (10 more than margin). Max width to right margin (195): 195 - 25 = 170.
                    pdf.multi_cell(170, 4, clean_pdf_text("Parágrafo único. Em casos de comprovada necessidade, a pessoa com transtorno do espectro autista incluída nas classes comuns de ensino regular, nos termos do inciso IV do art. 2º, terá direito a acompanhante especializado."), 0, 'J')
                    pdf.ln(2)

                    pdf.multi_cell(0, 4, clean_pdf_text("2- Lei Brasileira de Inclusão da Pessoa com Deficiência (LBI) no art. 3º, inciso XIII, descreve as ações referentes ao apoio:"), 0, 'J')
                    
                    pdf.set_x(25)
                    pdf.multi_cell(170, 4, clean_pdf_text("XIII - profissional de apoio escolar: pessoa que exerce atividades de alimentação, higiene e locomoção do estudante com deficiência e atua em todas as atividades escolares nas quais se fizer necessária, em todos os níveis e modalidades de ensino, em instituições públicas e privadas, excluídas as técnicas ou os procedimentos identificados com profissões legalmente estabelecidas;"), 0, 'J')
                    pdf.ln(2)

                    pdf.multi_cell(0, 4, clean_pdf_text("3- CNE/CEB nº 02/01, do Conselho Nacional de Educação, que Instituiu as Diretrizes Nacionais para a Educação Especial na Educação Básica, cujo artigo 6º assim dispõe:"), 0, 'J')
                    
                    pdf.set_x(25)
                    pdf.multi_cell(170, 4, clean_pdf_text("Art. 6º Para a identificação das necessidades educacionais especiais dos alunos e a tomada de decisões quanto ao atendimento necessário, a escola deve realizar, com assessoramento técnico, avaliação do aluno no processo de ensino e aprendizagem, contando, para tal, com:"), 0, 'J')
                    
                    pdf.set_x(35)
                    # Indent 35. Max width to right margin (195): 195 - 35 = 160.
                    pdf.multi_cell(160, 4, clean_pdf_text("I - a experiência de seu corpo docente, seus diretores, coordenadores, orientadores e supervisores educacionais;\nII - o setor responsável pela educação especial do respectivo sistema;\nIII - a colaboração da família e a cooperação dos serviços de Saúde, Assistência Social, Trabalho, Justiça e Esporte, bem como do Ministério Público, quando necessário."), 0, 'J')
                    pdf.ln(4)

                    # 5. GENERAL ASPECTS
                    pdf.set_fill_color(240, 240, 240)
                    pdf.set_font("Arial", "B", 10)
                    pdf.cell(0, 7, clean_pdf_text("ASPECTOS GERAIS DA VIDA ESCOLAR DO ESTUDANTE"), 1, 1, 'L', True)
                    pdf.set_font("Arial", "", 10); pdf.set_fill_color(255, 255, 255)
                    text_general = data_aval.get('aspectos_gerais') if data_aval.get('aspectos_gerais') else " "
                    # Use 0 for auto width (margin to margin), Justified 'J'
                    pdf.multi_cell(0, 5, clean_pdf_text(text_general), 1, 'J')
                    pdf.ln(5)

                    def print_section_header_fix(pdf, title):
                        pdf.set_fill_color(240, 240, 240); pdf.set_font("Arial", "B", 10)
                        pdf.cell(0, 8, clean_pdf_text(title), 1, 1, 'L', True)
                        pdf.ln(1)

                    def print_question_options_fix(pdf, question_title, options, selected_value, obs=None):
                        pdf.set_x(15)
                        pdf.set_font("Arial", "B", 10)
                        pdf.cell(0, 6, clean_pdf_text(question_title), 0, 1)
                        pdf.set_font("Arial", "", 10)
                        for opt in options:
                            is_checked = (selected_value == opt) or (isinstance(selected_value, list) and opt in selected_value)
                            pdf.set_x(15)
                            x, y = pdf.get_x(), pdf.get_y()
                            pdf.rect(x, y+1, 3, 3)
                            if is_checked:
                                pdf.line(x, y+1, x+3, y+4)
                                pdf.line(x, y+4, x+3, y+1)
                            pdf.set_xy(x + 5, y)
                            pdf.multi_cell(175, 5, clean_pdf_text(opt), 0, 'L')
                        if obs:
                            pdf.set_x(15)
                            # Obs uses full width (0) and Justified (J)
                            pdf.multi_cell(0, 5, clean_pdf_text(f"Obs: {obs}"), 0, 'J')
                        pdf.ln(2)

                    # PART I
                    print_section_header_fix(pdf, "PARTE I - HABILIDADES DE VIDA DIÁRIA")
                    print_question_options_fix(pdf, "1. ALIMENTAÇÃO:", opts_alim, data_aval.get('alim_nivel'), data_aval.get('alim_obs'))
                    print_question_options_fix(pdf, "2. HIGIENE:", opts_hig, data_aval.get('hig_nivel'), data_aval.get('hig_obs'))
                    print_question_options_fix(pdf, "3. LOCOMOÇÃO:", opts_loc, data_aval.get('loc_nivel'), data_aval.get('loc_obs'))
                    
                    # PART II
                    if pdf.get_y() > 220: pdf.add_page()
                    print_section_header_fix(pdf, "PARTE II - HABILIDADE SOCIAIS E DE INTERAÇÃO")
                    print_question_options_fix(pdf, "4. COMPORTAMENTO:", opts_comp, data_aval.get('comportamento'), data_aval.get('comp_obs'))
                    if pdf.get_y() > 230: pdf.add_page()
                    print_question_options_fix(pdf, "5. PARTICIPAÇÃO EM GRUPO:", opts_part, data_aval.get('part_grupo'), data_aval.get('part_obs'))
                    
                    pdf.set_font("Arial", "B", 10); pdf.cell(0, 6, "6. INTERAÇÃO:", 0, 1)
                    pdf.set_font("Arial", "", 10)
                    for opt in opts_int[:-1]:
                        draw_check_option_simple(pdf, opt, data_aval.get('interacao') == opt)
                    is_outros = (data_aval.get('interacao') == "Outros")
                    txt_outros = f"Outros: {data_aval.get('interacao_outros') if data_aval.get('interacao_outros') else '____________________'}"
                    draw_check_option_simple(pdf, txt_outros, is_outros)
                    pdf.ln(4)

                    # PART III
                    if pdf.get_y() > 230: pdf.add_page()
                    print_section_header_fix(pdf, "PARTE III - HABILIDADES PEDAGÓGICAS")
                    print_question_options_fix(pdf, "7. ROTINA EM SALA:", opts_rot, data_aval.get('rotina'), data_aval.get('rotina_obs'))
                    print_question_options_fix(pdf, "8. ATIVIDADES PEDAGÓGICAS:", opts_ativ, data_aval.get('ativ_pedag'))

                    # PART IV
                    if pdf.get_y() > 220: pdf.add_page()
                    print_section_header_fix(pdf, "PARTE IV - HABILIDADES DE COMUNICAÇÃO E ATENÇÃO")
                    print_question_options_fix(pdf, "9. ATENÇÃO SUSTENTADA:", opts_at_sust, data_aval.get('atencao_sust'))
                    print_question_options_fix(pdf, "10. ATENÇÃO DIVIDIDA:", opts_at_div, data_aval.get('atencao_div'))
                    if pdf.get_y() > 240: pdf.add_page()
                    print_question_options_fix(pdf, "11. ATENÇÃO SELETIVA:", opts_at_sel, data_aval.get('atencao_sel'))
                    print_question_options_fix(pdf, "12. LINGUAGEM:", opts_ling, data_aval.get('linguagem'), data_aval.get('ling_obs'))

                    # 6. ZEBRA STRIPED TABLE - IMPROVED
                    if pdf.get_y() > 200: pdf.add_page()
                    pdf.ln(2); pdf.set_font("Arial", "B", 10)
                    pdf.set_fill_color(200, 200, 200)
                    # Use width 180 total (60+120)
                    pdf.cell(60, 8, clean_pdf_text("NÍVEIS DE APOIO"), 1, 0, 'C', True)
                    pdf.cell(120, 8, clean_pdf_text("CARACTERÍSTICAS"), 1, 1, 'C', True)
                    
                    def print_zebra_row_fix(pdf, col1, col2, fill):
                        # Approximate line counting for better cell height
                        # Col1 width 60mm. approx 28 chars per line (Arial 9).
                        # Col2 width 120mm. approx 65 chars per line (Arial 9).
                        
                        lines_left = max(1, len(col1) // 28 + (1 if len(col1) % 28 > 0 else 0))
                        lines_right = max(1, len(col2) // 65 + (1 if len(col2) % 65 > 0 else 0))
                        
                        # Adjust for known texts to ensure clean look
                        if "Não há necessidade" in col1: lines_right = 3
                        if "Nível 1" in col1: lines_right = 2
                        if "Nível 2" in col1: lines_left = 2; lines_right = 1
                        if "Nível 3" in col1: lines_right = 2

                        max_lines = max(lines_left, lines_right)
                        row_height = max_lines * 5 + 4 # 5mm per line + 4mm padding
                        
                        x, y = 15, pdf.get_y()
                        # Check page break
                        if y + row_height > 270:
                            pdf.add_page()
                            y = pdf.get_y()
                        
                        pdf.set_fill_color(240, 240, 240) if fill else pdf.set_fill_color(255, 255, 255)
                        
                        # Draw Backgrounds
                        pdf.rect(x, y, 60, row_height, 'F'); pdf.rect(x, y, 60, row_height)
                        pdf.rect(x+60, y, 120, row_height, 'F'); pdf.rect(x+60, y, 120, row_height)
                        
                        # Print Left (Centered Vertically and Horizontally)
                        pdf.set_font("Arial", "B", 9)
                        y_off1 = (row_height - (lines_left * 5)) / 2
                        pdf.set_xy(x, y + y_off1)
                        pdf.multi_cell(60, 5, clean_pdf_text(col1), 0, 'C')
                        
                        # Print Right (Centered Vertically, Justified)
                        pdf.set_font("Arial", "", 9)
                        y_off2 = (row_height - (lines_right * 5)) / 2
                        pdf.set_xy(x+60, y + y_off2)
                        pdf.multi_cell(120, 5, clean_pdf_text(col2), 0, 'J')
                        
                        pdf.set_xy(x, y + row_height)

                    print_zebra_row_fix(pdf, "Não há necessidade de apoio", "O estudante apresenta autonomia. As ações disponibilizadas aos demais estudantes são suficientes, acrescidas de ações do AEE.", False)
                    print_zebra_row_fix(pdf, "Nível 1 - Apoio pouco substancial", "Não há necessidade de apoio constante, apenas em ações pontuais.", True)
                    print_zebra_row_fix(pdf, "Nível 2 - Apoio substancial (sala de aula)", "Há necessidade de apoio constante ao estudante.", False)
                    print_zebra_row_fix(pdf, "Nível 3 - Apoio muito substancial", "Casos severos com necessidade de monitor e ações específicas: flexibilização de horário e espaços.", True)

                    pdf.ln(5)
                    pdf.set_font("Arial", "B", 11); pdf.cell(0, 8, clean_pdf_text("CONCLUSÃO DA EQUIPE PEDAGÓGICA"), 0, 1)
                    pdf.set_font("Arial", "", 10)
                    pdf.multi_cell(0, 5, clean_pdf_text("Diante dos aspectos avaliados, a equipe pedagógica verificou que o estudante corresponde ao Nível:"), 0, 'L')
                    
                    level_result = data_aval.get('conclusao_nivel', 'NÃO NECESSITA DE APOIO').upper()
                    pdf.set_font("Arial", "B", 12); pdf.ln(2); pdf.cell(0, 8, clean_pdf_text(level_result), 1, 1, 'C')
                    
                    pdf.ln(3); pdf.set_font("Arial", "", 10)
                    apoio_txt = data_aval.get('apoio_existente') if data_aval.get('apoio_existente') else "______________________________________________________"
                    pdf.multi_cell(0, 5, clean_pdf_text(f"Profissional de Apoio Escolar (se houver): {apoio_txt}"), 0, 'L')

                    pdf.ln(10)
                    if pdf.get_y() > 240: pdf.add_page()
                    pdf.set_font("Arial", "B", 10); pdf.cell(0, 6, clean_pdf_text("Responsáveis pela avaliação:"), 0, 1); pdf.ln(5)
                    
                    # Signatures formatted with Name on one line, Role below
                    def draw_signature_block(pdf, x, y, width, name, role):
                        pdf.line(x, y, x + width, y)
                        pdf.set_xy(x, y + 2)
                        pdf.set_font("Arial", "", 9)
                        pdf.multi_cell(width, 4, clean_pdf_text(name), 0, 'C')
                        pdf.set_xy(x, pdf.get_y())
                        pdf.set_font("Arial", "I", 8)
                        pdf.multi_cell(width, 4, clean_pdf_text(role), 0, 'C')

                    y_sig_1 = pdf.get_y()
                    draw_signature_block(pdf, 10, y_sig_1, 55, data_aval.get('resp_sala',''), "Prof. Sala Regular")
                    draw_signature_block(pdf, 75, y_sig_1, 55, data_aval.get('resp_ef',''), "Prof. Ed. Física")
                    draw_signature_block(pdf, 140, y_sig_1, 55, data_aval.get('resp_arte',''), "Prof. Arte")
                    
                    # Add space for next row
                    pdf.set_xy(10, y_sig_1 + 25)
                    y_sig_2 = pdf.get_y()
                    
                    draw_signature_block(pdf, 10, y_sig_2, 55, data_aval.get('resp_dir',''), "Equipe Gestora")
                    draw_signature_block(pdf, 75, y_sig_2, 55, data_aval.get('resp_ee',''), "Prof. Ed. Especial")
                    draw_signature_block(pdf, 140, y_sig_2, 55, data_aval.get('resp_coord',''), "Coordenação")
                    
                    pdf.ln(25); pdf.set_font("Arial", "", 10)
                    # Left aligned date ('L')
                    pdf.cell(0, 6, clean_pdf_text(f"Limeira, {data_aval.get('data_emissao', date.today()).strftime('%d/%m/%Y')}."), 0, 1, 'L')

                    st.session_state.pdf_bytes_aval = get_pdf_bytes(pdf)
                    st.rerun()

            if 'pdf_bytes_aval' in st.session_state:
                st.download_button("📥 BAIXAR PDF AVALIAÇÃO", st.session_state.pdf_bytes_aval, f"Avaliacao_{data_aval.get('nome','aluno')}.pdf", "application/pdf", type="primary")

        # --- ABA HISTÓRICO ---
        with tabs[1]:
            st.subheader("Histórico de Atividades")
            df_hist = safe_read("Historico", ["Data_Hora", "Aluno", "Usuario", "Acao", "Detalhes"])
            if not df_hist.empty and data_aval.get('nome'):
                student_hist = df_hist[df_hist["Aluno"] == data_aval.get('nome')]
                if not student_hist.empty:
                    st.dataframe(student_hist.iloc[::-1], use_container_width=True, hide_index=True)
                else: st.info("Sem histórico.")
            else: st.info("Histórico vazio.")
            

     # --- RELATÓRIO DIÁRIO ---
    elif doc_mode == "Relatório de Acompanhamento":
        st.markdown("""<div class="header-box"><div class="header-title">Relatório Diário de Acompanhamento</div></div>""", unsafe_allow_html=True)
        st.markdown("""<style>div[data-testid="stFormSubmitButton"] > button {width: 100%; background-color: #dcfce7; color: #166534; border: 1px solid #166534;}</style>""", unsafe_allow_html=True)
        
        # Inicializa se não existir
        if 'data_diario' not in st.session_state: st.session_state.data_diario = {}
        data_diario = st.session_state.data_diario
        if 'logs' not in data_diario: data_diario['logs'] = {}
        
        data_pei = st.session_state.data_pei # Para puxar dados automáticos
        
        tab_fill, tab_gen = st.tabs(["📝 Registro de Atividades", "🖨️ Emissão Mensal"])
        
        with tab_fill:
            with st.form("form_diario_registro"):
                st.subheader("1. Dados Gerais (Configuração)")
                st.caption("Estes dados serão usados no cabeçalho do relatório.")
                
                # Importar dados básicos
                if st.form_submit_button("🔄 Importar Dados do Aluno"):
                    if data_pei:
                        data_diario['nome'] = data_pei.get('nome', '')
                        data_diario['ano_esc'] = data_pei.get('ano_esc', '')
                        data_diario['escola'] = "CEIEF Rafael Affonso Leite"
                        st.success("Dados importados!")
                    else:
                        st.warning("Sem dados PEI para importar.")

                c1, c2 = st.columns(2)
                data_diario['escola'] = c1.text_input("Escola", value=data_diario.get('escola', 'CEIEF Rafael Affonso Leite'))
                data_diario['nome'] = c2.text_input("Estudante", value=data_diario.get('nome', data_pei.get('nome','')), disabled=True)
                
                c3, c4 = st.columns(2)
                data_diario['ano_esc'] = c3.text_input("Ano de Escolaridade", value=data_diario.get('ano_esc', data_pei.get('ano_esc','')))
                data_diario['periodo'] = c4.selectbox("Período", ["Manhã", "Tarde", "Integral"], index=0 if data_diario.get('periodo') == "Manhã" else (1 if data_diario.get('periodo') == "Tarde" else 2))
                
                data_diario['acompanhante'] = st.text_input("Acompanhante (Profissional)", value=data_diario.get('acompanhante', st.session_state.get('usuario_nome','')))
                
                st.divider()
                st.subheader("2. Registro do Dia")
                
                # Seleção da Data para Registro
                col_d_sel, col_info = st.columns([1, 2])
                data_selecionada = col_d_sel.date_input("Selecione a Data", value=date.today(), format="DD/MM/YYYY")
                data_str = data_selecionada.strftime("%Y-%m-%d")
                
                # Recuperar dados existentes para esta data
                log_atual = data_diario['logs'].get(data_str, {})
                
                # Checkbox Falta
                falta_val = log_atual.get('falta', False)
                falta = st.checkbox("Estudante Faltou?", value=falta_val)
                
                # Descrição
                desc_val = log_atual.get('descricao', '')
                descricao = st.text_area("Descrição das atividades realizadas:", value=desc_val, height=150, help="Descreva as atividades ou ocorrências deste dia.")
                
                st.markdown("---")
                # Botão de Salvar
                if st.form_submit_button("💾 Salvar Registro do Dia"):
                    # Atualiza o log no dicionário
                    data_diario['logs'][data_str] = {
                        'falta': falta,
                        'descricao': descricao
                    }
                    # Salva no banco de dados (persistência)
                    save_student("DIARIO", data_diario.get('nome', 'aluno'), data_diario, f"Diário {data_selecionada.strftime('%d/%m')}")
                    st.success(f"Registro de {data_selecionada.strftime('%d/%m/%Y')} salvo com sucesso!")
                    time.sleep(1)
                    st.rerun()

            # Visualização rápida dos últimos registros
            if data_diario['logs']:
                st.divider()
                st.markdown("##### 📅 Registros Recentes")
                # Converter para DF para mostrar
                lista_logs = []
                for d, info in data_diario['logs'].items():
                    lista_logs.append({
                        "Data": datetime.strptime(d, "%Y-%m-%d").date(),
                        "Presença": "Faltou" if info.get('falta') else "Presente",
                        "Resumo Atividade": info.get('descricao', '')[:100] + "..."
                    })
                if lista_logs:
                    df_logs = pd.DataFrame(lista_logs).sort_values("Data", ascending=False)
                    st.dataframe(df_logs, use_container_width=True, hide_index=True)

        with tab_gen:
            st.subheader("Emissão de Relatório Mensal")
            st.caption(f"Código Único do Documento: {data_diario.get('doc_uuid', 'Será gerado na emissão')}")
            
            c_m, c_y = st.columns(2)
            meses = {1:"Janeiro", 2:"Fevereiro", 3:"Março", 4:"Abril", 5:"Maio", 6:"Junho", 7:"Julho", 8:"Agosto", 9:"Setembro", 10:"Outubro", 11:"Novembro", 12:"Dezembro"}
            mes_sel = c_m.selectbox("Mês", list(meses.keys()), format_func=lambda x: meses[x], index=date.today().month - 1)
            ano_sel = c_y.number_input("Ano", min_value=2020, max_value=2030, value=date.today().year)
            
            if st.button("👁️ Gerar PDF Mensal", type="primary"):
                # Filtra logs do mês/ano selecionado
                logs_mensais = {}
                for d_str, info in data_diario['logs'].items():
                    try:
                        d_obj = datetime.strptime(d_str, "%Y-%m-%d").date()
                        if d_obj.month == mes_sel and d_obj.year == ano_sel:
                            logs_mensais[d_str] = info
                    except: pass
                
                if not logs_mensais:
                    st.warning("Não há registros salvos para o período selecionado.")
                else:
                    # Garantir UUID se não tiver
                    if 'doc_uuid' not in data_diario or not data_diario['doc_uuid']:
                        data_diario['doc_uuid'] = str(uuid.uuid4()).upper()
                        save_student("DIARIO", data_diario.get('nome', 'aluno'), data_diario, "Geração UUID")

                    log_action(data_diario.get('nome'), "Gerou PDF", f"Relatório Mensal {mes_sel}/{ano_sel}")
                    
                    # Cria PDF em Retrato ('P')
                    pdf = OfficialPDF('P', 'mm', 'A4')
                    pdf.add_page(); pdf.set_margins(15, 15, 15)
                    
                    # SET SIGNATURE FOOTER (Diario has different signature handling, but let's standardize verification)
                    # For Diário, signatures are usually just the accompanying professional printed
                    signatures_mock = []
                    if data_diario.get('acompanhante'):
                        signatures_mock.append({'name': data_diario.get('acompanhante'), 'role': 'Acompanhante'})
                    pdf.set_signature_footer(signatures_mock, data_diario.get('doc_uuid'))
                    
                    # --- CABEÇALHO ---
                    if os.path.exists("logo_prefeitura.png"): pdf.image("logo_prefeitura.png", 15, 10, 25)
                    if os.path.exists("logo_escola.png"): pdf.image("logo_escola.png", 170, 6, 25)

                    # Títulos Centralizados
                    pdf.set_xy(0, 15); pdf.set_font("Arial", "B", 12)
                    pdf.cell(210, 6, clean_pdf_text("PREFEITURA MUNICIPAL DE LIMEIRA"), 0, 1, 'C')
                    pdf.cell(180, 6, clean_pdf_text("CEIEF RAFAEL AFFONSO LEITE"), 0, 1, 'C')
                    pdf.ln(8)
                    pdf.set_font("Arial", "B", 16); pdf.cell(0, 10, clean_pdf_text("RELATÓRIO DIÁRIO DE AÇÕES DE ACOMPANHAMENTO ESCOLAR"), 0, 1, 'C')
                    pdf.ln(5)
                    
                    # Dados do Cabeçalho
                    pdf.set_font("Arial", "B", 10)
                    
                    # Linha 1
                    pdf.cell(15, 6, "Escola:", 0, 0)
                    pdf.set_font("Arial", "", 10)
                    pdf.cell(110, 6, clean_pdf_text(data_diario.get('escola', '')), "B", 0)
                    
                    pdf.set_font("Arial", "B", 10)
                    pdf.cell(25, 6, clean_pdf_text("Data (Ref):"), 0, 0)
                    pdf.set_font("Arial", "", 10)
                    pdf.cell(0, 6, f"{meses[mes_sel]}/{ano_sel}", "B", 1)
                    pdf.ln(2)
                    
                    # Linha 2
                    pdf.set_font("Arial", "B", 10)
                    pdf.cell(20, 6, "Estudante:", 0, 0)
                    pdf.set_font("Arial", "", 10)
                    pdf.cell(0, 6, clean_pdf_text(data_diario.get('nome', '')), "B", 1)
                    pdf.ln(2)
                    
                    # Linha 3
                    pdf.set_font("Arial", "B", 10)
                    pdf.cell(35, 6, "Ano Escolaridade:", 0, 0)
                    pdf.set_font("Arial", "", 10)
                    pdf.cell(60, 6, clean_pdf_text(data_diario.get('ano_esc', '')), "B", 0)
                    
                    pdf.set_font("Arial", "B", 10)
                    pdf.cell(20, 6, clean_pdf_text("Período:"), 0, 0)
                    pdf.set_font("Arial", "", 10)
                    pdf.cell(0, 6, clean_pdf_text(data_diario.get('periodo', '')), "B", 1)
                    pdf.ln(2)
                    
                    # Linha 4
                    pdf.set_font("Arial", "B", 10)
                    pdf.cell(30, 6, "Acompanhante:", 0, 0)
                    pdf.set_font("Arial", "", 10)
                    pdf.cell(0, 6, clean_pdf_text(data_diario.get('acompanhante', '')), "B", 1)
                    
                    pdf.ln(8)
                    
                    # Tabela
                    pdf.set_font("Arial", "B", 11)
                    pdf.set_fill_color(200, 200, 200)
                    pdf.cell(0, 8, clean_pdf_text("Descrição das atividades realizadas com o estudante"), 1, 1, 'C', True)
                    
                    # Cabeçalho da Tabela
                    pdf.set_font("Arial", "B", 10)
                    pdf.set_fill_color(240, 240, 240)
                    pdf.cell(25, 8, "DATA", 1, 0, 'C', True)
                    pdf.cell(0, 8, clean_pdf_text("ATIVIDADES / OCORRÊNCIAS"), 1, 1, 'C', True)
                   
                    # Conteúdo (Loop)
                    pdf.set_font("Arial", "", 10)
                    
                    # Ordenar dias
                    dias_ordenados = sorted(logs_mensais.keys())
                    
                    for d_str in dias_ordenados:
                        info = logs_mensais[d_str]
                        d_obj = datetime.strptime(d_str, "%Y-%m-%d")
                        d_fmt = d_obj.strftime("%d/%m")
                        
                        texto = info.get('descricao', '')
                        if info.get('falta'):
                            texto = "[ESTUDANTE FALTOU] " + texto
                        
                        pdf.set_x(15)
                        x_start = pdf.get_x()
                        y_start = pdf.get_y()
                        
                        # --- CÁLCULO DE ALTURA CORRIGIDO ---
                        texto_limpo = clean_pdf_text(texto)
                        line_height = 5
                        linhas_totais = 0
                        
                        # Divide o texto pelos "Enters" (\n) e calcula as linhas reais
                        for paragrafo in texto_limpo.split('\n'):
                            largura_paragrafo = pdf.get_string_width(paragrafo)
                            if largura_paragrafo == 0:
                                linhas_totais += 1  # Conta as linhas totalmente em branco
                            else:
                                # Adiciona as quebras automáticas que o FPDF vai fazer por falta de espaço
                                linhas_totais += int(largura_paragrafo / 150) + 1
                                
                        # Calcula a altura final baseada no número real de linhas (multiplicado pela altura da linha)
                        h_row = max(8, (linhas_totais * line_height) + 4) 
                        # -----------------------------------

                        # Check page break
                        if y_start + h_row > 270:
                            pdf.add_page()
                            y_start = pdf.get_y()
                            x_start = pdf.get_x() # Atualiza o X por segurança na nova página
                            
                        # Draw Cells
                        pdf.rect(x_start, y_start, 25, h_row) # Box Data
                        pdf.rect(x_start + 25, y_start, 155, h_row) # Box Desc
                        
                        # Print Data
                        pdf.set_xy(x_start, y_start)
                        pdf.cell(25, h_row, d_fmt, 0, 0, 'C')
                        
                        # Print Desc
                        pdf.set_xy(x_start + 27, y_start + 2)
                        pdf.multi_cell(151, line_height, texto_limpo, 0, 'J')
                        
                        # Move cursor
                        pdf.set_xy(x_start, y_start + h_row)
                        
                    # Assinaturas
                    pdf.ln(10)
                    if pdf.get_y() > 250: pdf.add_page()
                    
                    y = pdf.get_y()
                    pdf.line(15, y+10, 105, y+10)
                    pdf.line(115, y+10, 195, y+10)
                    
                    pdf.set_xy(15, y+11)
                    pdf.set_font("Arial", "", 9)
                    pdf.cell(90, 5, "Assinatura do Acompanhante", 0, 0, 'C')
                    pdf.cell(80, 5, clean_pdf_text("                       Visto da Coordenação/Direção"), 0, 1, 'C')
                    
                    st.session_state.pdf_bytes_diario_mes = get_pdf_bytes(pdf)
                    st.rerun()

            if 'pdf_bytes_diario_mes' in st.session_state:
                file_name_clean = data_diario.get('nome','aluno').replace(" ", "_")
                st.download_button(
                    "📥 BAIXAR RELATÓRIO MENSAL (PDF)", 
                    st.session_state.pdf_bytes_diario_mes, 
                    f"Diario_{file_name_clean}_{mes_sel}_{ano_sel}.pdf", 
                    "application/pdf", 
                    type="primary"
                )

# --- DECLARAÇÃO DE MATRÍCULA (NOVO) ---
    elif doc_mode == "Declaração de Matrícula":
        st.markdown(f"""<div class="header-box"><div class="header-title">Declaração de Matrícula e Atendimento</div></div>""", unsafe_allow_html=True)
        
        data_dec = st.session_state.data_declaracao
        data_pei = st.session_state.data_pei
        data_case = st.session_state.data_case
        data_pdi = st.session_state.data_pdi
        data_aval = st.session_state.get('data_avaliacao', {})
        
        # Helper safe get
        def get_d(d, k, default=""):
            return d.get(k, default) if d.get(k) else default

        with st.form("form_declaracao"):
            st.subheader("Dados da Declaração")
            st.caption("Os dados são pré-carregados dos outros documentos (PEI, PDI, Avaliação, Estudo de Caso) se disponíveis. Verifique e complemente se necessário.")
            
            # --- LÓGICA DE DADOS PADRÃO (AUTOPREENCHIMENTO) ---
            # Se o campo já estiver salvo em data_dec, usa ele. Senão, tenta buscar nos outros docs.
            
            # Nome
            val_nome = data_dec.get('nome') or get_d(data_pei, 'nome') or get_d(data_case, 'nome') or get_d(data_aval, 'nome') or st.session_state.get('aluno_selecionado', '')
            data_dec['nome'] = val_nome

            # Turma/Ano
            val_turma = data_dec.get('turma') or get_d(data_pei, 'ano_esc') or get_d(data_case, 'ano_esc') or get_d(data_aval, 'ano_esc')
            
            # Período
            val_periodo = data_dec.get('periodo') or get_d(data_case, 'periodo', 'Manhã')
            
            # Deficiência
            val_defic = data_dec.get('deficiencia')
            if not val_defic:
                if data_pei.get('defic_txt'): val_defic = data_pei['defic_txt']
                elif data_pei.get('diag_tipo'): val_defic = ", ".join(data_pei['diag_tipo'])
                elif data_aval.get('defic_chk'): val_defic = ", ".join(data_aval['defic_chk'])
            
            # Professores (Prioridade: PEI -> Avaliação -> Vazio)
            val_poli = data_dec.get('prof_poli') or get_d(data_pei, 'prof_poli') or get_d(data_aval, 'resp_sala')
            val_arte = data_dec.get('prof_arte') or get_d(data_pei, 'prof_arte') or get_d(data_aval, 'resp_arte')
            val_ef = data_dec.get('prof_ef') or get_d(data_pei, 'prof_ef') or get_d(data_aval, 'resp_ef')
            val_tec = data_dec.get('prof_tec') or get_d(data_pei, 'prof_tec') # Linguagens e Tecnologias
            val_aee = data_dec.get('prof_aee') or get_d(data_pei, 'prof_aee') or get_d(data_aval, 'resp_ee')
            
            # AEE Detalhes (Prioridade: PDI -> Vazio)
            val_aee_mod = data_dec.get('aee_modalidade') or get_d(data_pdi, 'aee_tipo')
            val_aee_comp = data_dec.get('aee_composicao') or get_d(data_pdi, 'aee_comp')
            val_aee_tempo = data_dec.get('aee_tempo') or get_d(data_pdi, 'aee_tempo', '50 minutos')
            
            # Apoio Escolar (Prioridade: Avaliação de Apoio -> Vazio)
            val_tem_apoio = data_dec.get('tem_apoio')
            val_nome_apoio = data_dec.get('nome_apoio')
            
            if not val_tem_apoio:
                # Inferência automática baseada na Avaliação de Apoio
                nivel = data_aval.get('conclusao_nivel', '')
                apoio_ex = data_aval.get('apoio_existente', '')
                if "Nível 2" in nivel or "Nível 3" in nivel or apoio_ex:
                    val_tem_apoio = 'Sim'
                    if not val_nome_apoio: val_nome_apoio = apoio_ex
                else:
                    val_tem_apoio = 'Não'

            # --- RENDERIZAÇÃO DOS CAMPOS ---
            
            c1, c2 = st.columns([3, 1])
            data_dec['nome'] = c1.text_input("Nome do Estudante", value=val_nome, disabled=True)
            data_dec['turma'] = c2.text_input("Turma/Ano", value=val_turma)
            
            c3, c4 = st.columns([1, 2])
            per_opts = ["Manhã", "Tarde", "Integral"]
            p_idx = per_opts.index(val_periodo) if val_periodo in per_opts else 0
            data_dec['periodo'] = c3.selectbox("Período", per_opts, index=p_idx)
            data_dec['deficiencia'] = c4.text_input("Deficiência / Transtorno", value=val_defic)
            
            st.divider()
            st.markdown("##### Quadro Docente")
            d1, d2 = st.columns(2)
            data_dec['prof_poli'] = d1.text_input("Professor(a) Regente", value=val_poli)
            data_dec['prof_arte'] = d2.text_input("Professor(a) Arte", value=val_arte)
            d3, d4 = st.columns(2)
            data_dec['prof_ef'] = d3.text_input("Professor(a) Ed. Física", value=val_ef)
            data_dec['prof_tec'] = d4.text_input("Professor(a) Linguagens e Tecnologias", value=val_tec)
            
            st.divider()
            st.markdown("##### Atendimento Educacional Especializado (AEE)")
            data_dec['prof_aee'] = st.text_input("Professor(a) Sala de Recursos", value=val_aee)
            
            a1, a2 = st.columns(2)
            data_dec['aee_modalidade'] = a1.text_input("Modalidade", value=val_aee_mod, help="Ex: Sala de Recursos, Colaborativo")
            data_dec['aee_composicao'] = a2.text_input("Forma de Atendimento", value=val_aee_comp, help="Ex: Individual, Grupo")
            
            a3, a4 = st.columns(2)
            data_dec['aee_tempo'] = a3.text_input("Tempo por atendimento", value=val_aee_tempo)
            data_dec['aee_freq'] = a4.text_input("Qtd. Atendimentos Semanais", value=data_dec.get('aee_freq', ''))
            
            st.divider()
            st.markdown("##### Apoio Escolar")
            has_apoio_idx = 0 if val_tem_apoio == 'Sim' else 1
            data_dec['tem_apoio'] = st.radio("Possui Profissional de Apoio?", ["Sim", "Não"], index=has_apoio_idx, horizontal=True)
            
            if data_dec['tem_apoio'] == 'Sim':
                data_dec['nome_apoio'] = st.text_input("Nome do Profissional de Apoio", value=val_nome_apoio)
            else:
                data_dec['nome_apoio'] = "" # Limpa se não tiver

            st.divider()
            
            if not is_monitor:
                if st.form_submit_button("🔄 Atualizar dados (re-importar)"):
                    # Ao submeter, os valores recalculados acima serão usados nos widgets e salvos automaticamente no session_state pelo streamlit
                    # Apenas exibimos uma mensagem
                    st.toast("Dados atualizados com base nos documentos!", icon="🔄")
                
                # Botão Salvar (Para persistir no banco)
                if st.form_submit_button("💾 Salvar Declaração"):
                    save_student("DECLARACAO", data_dec['nome'], data_dec, "Geral")
            else:
                st.info("Modo visualização (Monitor).")

        # Signatures section for Declaration
        st.divider()
        st.subheader("Assinaturas Digitais")
        st.caption(f"Código Único: {data_dec.get('doc_uuid', 'Salvar para gerar')}")
        
        current_signatures = data_dec.get('signatures', [])
        if current_signatures:
            for sig in current_signatures:
                st.success(f"Assinado por {sig['name']} em {sig['date']}")
        
        user_name = st.session_state.get('usuario_nome', '')
        already_signed = any(s['name'] == user_name for s in current_signatures)
        
        if not already_signed and not is_monitor:
            if st.button("🖊️ Assinar Declaração"):
                new_sig = {
                    "name": user_name,
                    "role": "Profissional",
                    "date": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
                    "hash": str(uuid.uuid4())
                }
                if 'signatures' not in data_dec: data_dec['signatures'] = []
                data_dec['signatures'].append(new_sig)
                save_student("DECLARACAO", data_dec.get('nome'), data_dec, "Assinatura")
                st.rerun()

        # PDF Button
        if st.button("👁️ GERAR DECLARAÇÃO (PDF)"):
            log_action(data_dec.get('nome'), "Gerou PDF", "Declaração")
            
            pdf = OfficialPDF('P', 'mm', 'A4')
            pdf.add_page(); pdf.set_margins(20, 20, 20)
            pdf.set_signature_footer(data_dec.get('signatures', []), data_dec.get('doc_uuid', ''))
            
            if os.path.exists("logo_prefeitura.png"): pdf.image("logo_prefeitura.png", 20, 10, 25)
            if os.path.exists("logo_escola.png"): pdf.image("logo_escola.png", 165, 10, 25)
            
            pdf.set_xy(0, 20)
            pdf.set_font("Arial", "B", 14)
            pdf.cell(0, 8, clean_pdf_text("            PREFEITURA MUNICIPAL DE LIMEIRA"), 0, 1, 'C')
            pdf.cell(0, 8, clean_pdf_text("SECRETARIA MUNICIPAL DE EDUCAÇÃO"), 0, 1, 'C')
            
            pdf.ln(20)
            pdf.set_font("Arial", "B", 16)
            pdf.cell(0, 10, clean_pdf_text("DECLARAÇÃO DE MATRÍCULA E ATENDIMENTO"), 0, 1, 'C')
            pdf.ln(10)
            
            pdf.set_font("Arial", "", 12)
            texto_inicial = (
                f"Declaramos para os devidos fins que o(a) estudante {data_dec.get('nome', '').upper()}, "
                f"matriculado(a) na turma {data_dec.get('turma', '')}, período {data_dec.get('periodo', '').upper()}, "
                f"desta unidade escolar, frequenta as aulas regularmente."
            )
            pdf.multi_cell(0, 8, clean_pdf_text(texto_inicial))
            pdf.ln(5)
            
            texto_defic = f"O(A) estudante apresenta {data_dec.get('deficiencia', 'não informado')} e recebe acompanhamento pedagógico dos seguintes docentes:"
            pdf.multi_cell(0, 8, clean_pdf_text(texto_defic))
            pdf.ln(2)
            
            pdf.set_x(30)
            pdf.cell(0, 8, clean_pdf_text(f"- Professor(a) Regente: {data_dec.get('prof_poli', '')}"), 0, 1)
            pdf.set_x(30)
            pdf.cell(0, 8, clean_pdf_text(f"- Professor(a) Arte: {data_dec.get('prof_arte', '')}"), 0, 1)
            pdf.set_x(30)
            pdf.cell(0, 8, clean_pdf_text(f"- Professor(a) Ed. Física: {data_dec.get('prof_ef', '')}"), 0, 1)
            if data_dec.get('prof_tec'):
                pdf.set_x(30)
                pdf.cell(0, 8, clean_pdf_text(f"- Professor(a) Linguagens e Tecnologias: {data_dec.get('prof_tec', '')}"), 0, 1)
            
            pdf.ln(5)
            texto_aee = (
                f"No que tange ao Atendimento Educacional Especializado (AEE), o estudante é atendido pelo(a) "
                f"professor(a) {data_dec.get('prof_aee', '')}, na modalidade {data_dec.get('aee_modalidade', '')}, "
                f"de forma {data_dec.get('aee_composicao', '')}, com duração de {data_dec.get('aee_tempo', '')}, "
                f"{data_dec.get('aee_freq', '')} vezes por semana."
            )
            pdf.multi_cell(0, 8, clean_pdf_text(texto_aee))
            
            if data_dec.get('tem_apoio') == 'Sim':
                pdf.ln(5)
                pdf.multi_cell(0, 8, clean_pdf_text(f"Conta com o acompanhamento do Profissional de Apoio Escolar: {data_dec.get('nome_apoio', '')}."))
            
            pdf.ln(20)
            pdf.cell(0, 8, clean_pdf_text(f"Limeira, {datetime.now().strftime('%d/%m/%Y')}."), 0, 1, 'R')
            
            pdf.ln(30)
            pdf.cell(0, 8, "___________________________________________________", 0, 1, 'C')
            pdf.cell(0, 8, "Assinatura do Responsável / Direção", 0, 1, 'C')

            st.session_state.pdf_bytes_dec = get_pdf_bytes(pdf)
            st.rerun()

        if 'pdf_bytes_dec' in st.session_state:
            st.download_button("📥 BAIXAR DECLARAÇÃO", st.session_state.pdf_bytes_dec, f"Declaracao_{data_dec.get('nome','aluno')}.pdf", "application/pdf", type="primary")


elif modulo_atuacao == "🏫 Ensino Regular":
    
    # ==============================================================================
    # CARREGAMENTO GLOBAL DE CONFIGURAÇÕES (O CÉREBRO DO SISTEMA)
    # ==============================================================================
    df_config = safe_read("Config_Ata", ["chave", "valor"])
    
    def get_config(chave, padrao):
        if not df_config.empty and chave in df_config["chave"].values:
            return df_config.loc[df_config["chave"] == chave, "valor"].values[0]
        return padrao

    # --- Textos do Fundamental ---
    texto_base_padrao_ef = "Com base: na Resolução SME nº 07/2024, considerando as orientações da Resolução nº 02/2025 que atualiza o calendário escolar da Rede Municipal em decorrência da portaria nº 729 de 21 de fevereiro de 2025, que dispõe sobre o Calendário Escolar do ano de 2026 das Escolas da Rede Municipal de Ensino de Limeira, e no inciso V do artigo 5º, faz a indicação sobre a realização do Conselho de Ciclo/ Educação Infantil e Educação de Jovens e Adultos; no plano de trabalho para o ano de 2026, produzido no Conselho de Ciclo do 3º trimestre de 2025; na avaliação diagnóstica elaborada em fevereiro de 2026 e nas avaliações realizadas na unidade escolar no primeiro trimestre de 2026. Essa ata possibilita a análise sobre aprendizagem e desempenho dos estudantes e os resultados das estratégias de ensino empregadas."
    texto_base_ata_ef = get_config("texto_base_ata", texto_base_padrao_ef)
    
    propostas_padrao_ef = """1. Recuperação contínua de aprendizagem dos estudantes;
2. Intervenções pontuais e individuais;
3. Organização de recursos pedagógicos e situações didáticas eficientes e coerentes;
4. Encaminhamento à Direção/Serviço Social Escolar para busca ativa de estudantes com baixa frequência;
5. Proposta de compensação de ausências para o próximo trimestre;
6. Informar as famílias dos alunos com desempenho insuficiente e/ou baixa frequência visando a conscientização;
7. Indicar o aluno para Ação Pedagógica Complementar;
8. Propor atividades interdisciplinares objetivando o avanço do processo de aprendizagem;
9. Emitir relatórios solicitando suporte e avaliação de profissionais da saúde;
10. Sistematizar atividades para consolidação dos conteúdos;"""
    propostas_ata_ef = get_config("propostas_ata", propostas_padrao_ef)

    # --- Textos e Conteúdos da Educação Infantil ---
    texto_base_padrao_inf = "Com base na Resolução SME nº 07/24, considerando as orientações da Resolução nº 02/2025 que atualiza o calendário escolar da Rede Municipal em decorrência da portaria nº 729 de 21 de fevereiro de 2025, que dispõe sobre o Calendário Escolar do ano de 2026 das Escolas da Rede Municipal de Ensino de Limeira, especialmente no inciso V do artigo 5º que indica a realização do Conselho de Educação Infantil, na avaliação diagnóstica produzida em fevereiro de 2026 e nas avaliações realizadas na unidade escolar no primeiro trimestre de 2026. Essa ata possibilita a análise sobre aprendizagem e desempenho dos estudantes e os resultados das estratégias de ensino empregadas."
    texto_base_ata_inf = get_config("texto_base_ata_inf", texto_base_padrao_inf)
    
    propostas_padrao_inf = "1. \n2. \n3. \n4. \n5. \n6. "
    propostas_ata_inf = get_config("propostas_ata_inf", propostas_padrao_inf)

    def get_criterios_infantil(etapa):
        # Define os padrões para a 1ª Etapa (como Semente)
        padrao_lv = "Oralidade: (Pronúncia correta das palavras; Participação atenta nas exposições orais escutando com atenção, respondendo e elaborando questões); Leitura: (Compreensão do significado das palavras; Socialização de critérios de escolha e de apreciação estética de leituras; Leitura de sílabas canônica e não canônicas; Localização de Informações explícitas); Análise Linguística: (Escrita de palavras utilizando a direção convencional; Reconhecimento e utilização das letras do alfabeto para a produção escrita; Traçado de modo convencional as letras com o auxílio do(a) professor(a)); Produção: (Produção oral de textos com destino escrito considerando gênero trabalhado (silhueta), (interlocutor) (sentido a partir de uma situação dada))."
        padrao_lm = "Álgebra (Classificação por semelhanças e diferenças); Estatística (Identificação de informações em gráficos de colunas); Geometria (Noções de: direcionalidade, proximidade, interioridade, exterioridade, reconhecimento de figuras geométricas planas e espaciais); Grandezas e medidas (Noções de: tempo, massa, capacidade e comprimento); Números e operações (Sistema de numeração decimal: récita numérica, reconhecimento da representação simbólica, contagem, associação quantidade/número e Ideias das operações: resolução de situações-problema com ideia de juntar - com apoio de material manipulativo)."
        padrao_is = "Sistema biológico: Saúde (Princípios de higiene pessoal: banho diário, cuidado com os dentes, lavagem das mãos); Sujeito histórico: Dados pessoais, Identidade e Escola (Nome e Sobrenome; nomes dos colegas e educadores; regras de convivência no ambiente escolar)."
        padrao_arte = "Identificação e nomeação de formas geométricas básicas; Identificação e nomeação de cores; Identificação de instrumentos e suas famílias; Relação do som com a fonte sonora; Expressão de forma oral, gestual, utilizando a imaginação na representação teatral."
        padrao_ccm = "Vivência de diversas formas de deslocamento nas situações de brincadeira (andar para frente, andar para trás, quadrupejar, saltar com um dos pés, saltitar, correr); Participação em brincadeira(s) cantada(s), realizando os movimentos sugeridos; Participação em brincadeira envolvendo a imitação, utilizando a linguagem corporal; Reconhecimento em si das diversas partes do corpo; Identificação da relação entre seu corpo e o espaço: Experimentação de movimentos estáticos e dinâmicos de equilíbrio."
        padrao_lt = "Exploração de mídias e tecnologias; Interação com diferentes linguagens midiáticas; Uso criativo de recursos tecnológicos de forma orientada e lúdica."
        padrao_libras = "Contato inicial com a Língua Brasileira de Sinais; Percepção visual e espacial; Reconhecimento e imitação de sinais básicos e expressões faciais do cotidiano escolar."
        
        # Se for outra etapa e não tiver configurado ainda, volta vazio para a escola preencher
        if etapa != "1ª Etapa":
            padrao_lv, padrao_lm, padrao_is, padrao_arte, padrao_ccm, padrao_lt, padrao_libras = "", "", "", "", "", "", ""
            
        return {
            "LV": get_config(f"crit_lv_{etapa}", padrao_lv),
            "LM": get_config(f"crit_lm_{etapa}", padrao_lm),
            "IS": get_config(f"crit_is_{etapa}", padrao_is),
            "Arte": get_config(f"crit_arte_{etapa}", padrao_arte),
            "CCM": get_config(f"crit_ccm_{etapa}", padrao_ccm),
            "LT": get_config(f"crit_lt_{etapa}", padrao_lt),
            "LIBRAS": get_config(f"crit_libras_{etapa}", padrao_libras)
        }
    
    # Matriz de Professores Padrão (Semente)
    MATRIZ_SEED = [
        {"Ciclo": "Ciclo I (1º ao 3º ano)", "Turma": "1º Ano 1", "Disciplina": "Polivalente", "Professor": "Juliana Aparecida da Silva"},
        {"Ciclo": "Ciclo I (1º ao 3º ano)", "Turma": "1º Ano 1", "Disciplina": "Artes", "Professor": "Jordana Lima Alvez"},
        {"Ciclo": "Ciclo I (1º ao 3º ano)", "Turma": "1º Ano 1", "Disciplina": "Educação Física", "Professor": "Michel Luciano de Lima"},
        {"Ciclo": "Ciclo I (1º ao 3º ano)", "Turma": "1º Ano 1", "Disciplina": "Linguagens e Tecnologias", "Professor": "Fernando Indig Bongiovanni"},
        {"Ciclo": "Ciclo I (1º ao 3º ano)", "Turma": "1º Ano 2", "Disciplina": "Polivalente", "Professor": "Bruna Thais Bernini Guedes"},
        {"Ciclo": "Ciclo I (1º ao 3º ano)", "Turma": "1º Ano 2", "Disciplina": "Artes", "Professor": "Jordana Lima Alvez"},
        {"Ciclo": "Ciclo I (1º ao 3º ano)", "Turma": "1º Ano 2", "Disciplina": "Educação Física", "Professor": "Fernando Indig Bongiovanni"},
        {"Ciclo": "Ciclo I (1º ao 3º ano)", "Turma": "1º Ano 2", "Disciplina": "Linguagens e Tecnologias", "Professor": "Fernando Indig Bongiovanni"},
        {"Ciclo": "Ciclo I (1º ao 3º ano)", "Turma": "1º Ano 3", "Disciplina": "Polivalente", "Professor": "Marcela Buck de Gaspari"},
        {"Ciclo": "Ciclo I (1º ao 3º ano)", "Turma": "1º Ano 3", "Disciplina": "Artes", "Professor": "Jordana Lima Alvez"},
        {"Ciclo": "Ciclo I (1º ao 3º ano)", "Turma": "1º Ano 3", "Disciplina": "Educação Física", "Professor": "Fernando Indig Bongiovanni"},
        {"Ciclo": "Ciclo I (1º ao 3º ano)", "Turma": "1º Ano 3", "Disciplina": "Linguagens e Tecnologias", "Professor": "Fernando Indig Bongiovanni"},
        {"Ciclo": "Ciclo I (1º ao 3º ano)", "Turma": "2º Ano 1", "Disciplina": "Polivalente", "Professor": "Iara Cristina Galdino"},
        {"Ciclo": "Ciclo I (1º ao 3º ano)", "Turma": "2º Ano 1", "Disciplina": "Artes", "Professor": "Jordana Lima Alvez"},
        {"Ciclo": "Ciclo I (1º ao 3º ano)", "Turma": "2º Ano 1", "Disciplina": "Educação Física", "Professor": "Fernando Indig Bongiovanni"},
        {"Ciclo": "Ciclo I (1º ao 3º ano)", "Turma": "2º Ano 1", "Disciplina": "Linguagens e Tecnologias", "Professor": "Fernando Indig Bongiovanni"},
        {"Ciclo": "Ciclo I (1º ao 3º ano)", "Turma": "2º Ano 2", "Disciplina": "Polivalente", "Professor": "Natália dos Santos Lima Fula"},
        {"Ciclo": "Ciclo I (1º ao 3º ano)", "Turma": "2º Ano 2", "Disciplina": "Artes", "Professor": "Jordana Lima Alvez"},
        {"Ciclo": "Ciclo I (1º ao 3º ano)", "Turma": "2º Ano 2", "Disciplina": "Educação Física", "Professor": "Josiane Modesto da Silva"},
        {"Ciclo": "Ciclo I (1º ao 3º ano)", "Turma": "2º Ano 2", "Disciplina": "Linguagens e Tecnologias", "Professor": "Fernando Indig Bongiovanni"},
        {"Ciclo": "Ciclo I (1º ao 3º ano)", "Turma": "2º Ano 3", "Disciplina": "Polivalente", "Professor": "Amanda Mussi"},
        {"Ciclo": "Ciclo I (1º ao 3º ano)", "Turma": "2º Ano 3", "Disciplina": "Artes", "Professor": "Jordana Lima Alvez"},
        {"Ciclo": "Ciclo I (1º ao 3º ano)", "Turma": "2º Ano 3", "Disciplina": "Educação Física", "Professor": "Josiane Modesto da Silva"},
        {"Ciclo": "Ciclo I (1º ao 3º ano)", "Turma": "2º Ano 3", "Disciplina": "Linguagens e Tecnologias", "Professor": "Fernando Indig Bongiovanni"},
        {"Ciclo": "Ciclo I (1º ao 3º ano)", "Turma": "3º Ano 1", "Disciplina": "Polivalente", "Professor": "Marcia Regina Biserra Branco"},
        {"Ciclo": "Ciclo I (1º ao 3º ano)", "Turma": "3º Ano 1", "Disciplina": "Artes", "Professor": "Jordana Lima Alvez"},
        {"Ciclo": "Ciclo I (1º ao 3º ano)", "Turma": "3º Ano 1", "Disciplina": "Educação Física", "Professor": "Josiane Modesto da Silva"},
        {"Ciclo": "Ciclo I (1º ao 3º ano)", "Turma": "3º Ano 1", "Disciplina": "Linguagens e Tecnologias", "Professor": "Elaine Cristina Neves Fahl"},
        {"Ciclo": "Ciclo I (1º ao 3º ano)", "Turma": "3º Ano 2", "Disciplina": "Polivalente", "Professor": "Alessandra Rigon Ribeiro"},
        {"Ciclo": "Ciclo I (1º ao 3º ano)", "Turma": "3º Ano 2", "Disciplina": "Artes", "Professor": "Jordana Lima Alvez"},
        {"Ciclo": "Ciclo I (1º ao 3º ano)", "Turma": "3º Ano 2", "Disciplina": "Educação Física", "Professor": "Josiane Modesto da Silva"},
        {"Ciclo": "Ciclo I (1º ao 3º ano)", "Turma": "3º Ano 2", "Disciplina": "Linguagens e Tecnologias", "Professor": "Elaine Cristina Neves Fahl"},
        {"Ciclo": "Ciclo I (1º ao 3º ano)", "Turma": "3º Ano 3", "Disciplina": "Polivalente", "Professor": "Regiane Faustino"},
        {"Ciclo": "Ciclo I (1º ao 3º ano)", "Turma": "3º Ano 3", "Disciplina": "Artes", "Professor": "Jordana Lima Alvez"},
        {"Ciclo": "Ciclo I (1º ao 3º ano)", "Turma": "3º Ano 3", "Disciplina": "Educação Física", "Professor": "Josiane Modesto da Silva"},
        {"Ciclo": "Ciclo I (1º ao 3º ano)", "Turma": "3º Ano 3", "Disciplina": "Linguagens e Tecnologias", "Professor": "Elaine Cristina Neves Fahl"},
        
        {"Ciclo": "Ciclo II (4º e 5º ano)", "Turma": "4º Ano 1", "Disciplina": "Língua Portuguesa", "Professor": "Eliana Cristina de Carvalho Gabriel"},
        {"Ciclo": "Ciclo II (4º e 5º ano)", "Turma": "4º Ano 1", "Disciplina": "Matemática", "Professor": "Daiane Luzia de Matos Bueno"},
        {"Ciclo": "Ciclo II (4º e 5º ano)", "Turma": "4º Ano 1", "Disciplina": "Ciências, Hist. e Geo.", "Professor": "Débora Sara Ferreira"},
        {"Ciclo": "Ciclo II (4º e 5º ano)", "Turma": "4º Ano 1", "Disciplina": "Artes", "Professor": "Bruna Thais Bernini Guedes"},
        {"Ciclo": "Ciclo II (4º e 5º ano)", "Turma": "4º Ano 1", "Disciplina": "Educação Física", "Professor": "Michel Luciano de Lima"},
        {"Ciclo": "Ciclo II (4º e 5º ano)", "Turma": "4º Ano 1", "Disciplina": "Linguagens e Tecnologias", "Professor": "Josiane Modesto da Silva"},
        {"Ciclo": "Ciclo II (4º e 5º ano)", "Turma": "4º Ano 2", "Disciplina": "Língua Portuguesa", "Professor": "Eliana Cristina de Carvalho Gabriel"},
        {"Ciclo": "Ciclo II (4º e 5º ano)", "Turma": "4º Ano 2", "Disciplina": "Matemática", "Professor": "Daiane Luzia de Matos Bueno"},
        {"Ciclo": "Ciclo II (4º e 5º ano)", "Turma": "4º Ano 2", "Disciplina": "Ciências, Hist. e Geo.", "Professor": "Débora Sara Ferreira"},
        {"Ciclo": "Ciclo II (4º e 5º ano)", "Turma": "4º Ano 2", "Disciplina": "Artes", "Professor": "Bruna Thais Bernini Guedes"},
        {"Ciclo": "Ciclo II (4º e 5º ano)", "Turma": "4º Ano 2", "Disciplina": "Educação Física", "Professor": "Michel Luciano de Lima"},
        {"Ciclo": "Ciclo II (4º e 5º ano)", "Turma": "4º Ano 2", "Disciplina": "Linguagens e Tecnologias", "Professor": "Josiane Modesto da Silva"},
        {"Ciclo": "Ciclo II (4º e 5º ano)", "Turma": "4º Ano 3", "Disciplina": "Língua Portuguesa", "Professor": "Eliana Cristina de Carvalho Gabriel"},
        {"Ciclo": "Ciclo II (4º e 5º ano)", "Turma": "4º Ano 3", "Disciplina": "Matemática", "Professor": "Daiane Luzia de Matos Bueno"},
        {"Ciclo": "Ciclo II (4º e 5º ano)", "Turma": "4º Ano 3", "Disciplina": "Ciências, Hist. e Geo.", "Professor": "Débora Sara Ferreira"},
        {"Ciclo": "Ciclo II (4º e 5º ano)", "Turma": "4º Ano 3", "Disciplina": "Artes", "Professor": "Bruna Thais Bernini Guedes"},
        {"Ciclo": "Ciclo II (4º e 5º ano)", "Turma": "4º Ano 3", "Disciplina": "Educação Física", "Professor": "Michel Luciano de Lima"},
        {"Ciclo": "Ciclo II (4º e 5º ano)", "Turma": "4º Ano 3", "Disciplina": "Linguagens e Tecnologias", "Professor": "Josiane Modesto da Silva"},
        {"Ciclo": "Ciclo II (4º e 5º ano)", "Turma": "5º Ano 1", "Disciplina": "Polivalente", "Professor": "Elaine Cristina Neves Fahl"},
        {"Ciclo": "Ciclo II (4º e 5º ano)", "Turma": "5º Ano 1", "Disciplina": "Artes", "Professor": "Bruna Thais Bernini Guedes"},
        {"Ciclo": "Ciclo II (4º e 5º ano)", "Turma": "5º Ano 1", "Disciplina": "Educação Física", "Professor": "Michel Luciano de Lima"},
        {"Ciclo": "Ciclo II (4º e 5º ano)", "Turma": "5º Ano 1", "Disciplina": "Linguagens e Tecnologias", "Professor": "Bruna Thais Bernini Guedes"},
        {"Ciclo": "Ciclo II (4º e 5º ano)", "Turma": "5º Ano 2", "Disciplina": "Polivalente", "Professor": "Nathalia Teixeira Marcal Ribeiro"},
        {"Ciclo": "Ciclo II (4º e 5º ano)", "Turma": "5º Ano 2", "Disciplina": "Artes", "Professor": "Bruna Thais Bernini Guedes"},
        {"Ciclo": "Ciclo II (4º e 5º ano)", "Turma": "5º Ano 2", "Disciplina": "Educação Física", "Professor": "Michel Luciano de Lima"},
        {"Ciclo": "Ciclo II (4º e 5º ano)", "Turma": "5º Ano 2", "Disciplina": "Linguagens e Tecnologias", "Professor": "Bruna Thais Bernini Guedes"},
        {"Ciclo": "Ciclo II (4º e 5º ano)", "Turma": "5º Ano 3", "Disciplina": "Polivalente", "Professor": "Denise Teixeira Coelho Soffiati"},
        {"Ciclo": "Ciclo II (4º e 5º ano)", "Turma": "5º Ano 3", "Disciplina": "Artes", "Professor": "Bruna Thais Bernini Guedes"},
        {"Ciclo": "Ciclo II (4º e 5º ano)", "Turma": "5º Ano 3", "Disciplina": "Educação Física", "Professor": "Michel Luciano de Lima"},
        {"Ciclo": "Ciclo II (4º e 5º ano)", "Turma": "5º Ano 3", "Disciplina": "Linguagens e Tecnologias", "Professor": "Bruna Thais Bernini Guedes"},
        
        # --- EDUCAÇÃO INFANTIL ---
        {"Ciclo": "1ª Etapa", "Turma": "1ª Etapa 1", "Disciplina": "Professor de Ed. Infantil", "Professor": "Luciana Lopes Faber"},
        {"Ciclo": "1ª Etapa", "Turma": "1ª Etapa 1", "Disciplina": "Artes", "Professor": "Karen Cristina Fernandes Donatti"},
        {"Ciclo": "1ª Etapa", "Turma": "1ª Etapa 1", "Disciplina": "Educação Física", "Professor": "Michel Luciano de Lima"},
        {"Ciclo": "1ª Etapa", "Turma": "1ª Etapa 1", "Disciplina": "Linguagens e Tecnologias", "Professor": "Karina Brum Rivabene Gracini"},
        {"Ciclo": "1ª Etapa", "Turma": "1ª Etapa 2", "Disciplina": "Professor de Ed. Infantil", "Professor": "Sandra Maria Ribeiro dos Santos Brito"},
        {"Ciclo": "1ª Etapa", "Turma": "1ª Etapa 2", "Disciplina": "Artes", "Professor": "Karen Cristina Fernandes Donatti"},
        {"Ciclo": "1ª Etapa", "Turma": "1ª Etapa 2", "Disciplina": "Educação Física", "Professor": "Fernando Indig Bongiovanni"},
        {"Ciclo": "1ª Etapa", "Turma": "1ª Etapa 2", "Disciplina": "Linguagens e Tecnologias", "Professor": "Fernando Indig Bongiovanni"},
        {"Ciclo": "1ª Etapa", "Turma": "1ª Etapa 3", "Disciplina": "Professor de Ed. Infantil", "Professor": "Amanda Mussi"},
        {"Ciclo": "1ª Etapa", "Turma": "1ª Etapa 3", "Disciplina": "Artes", "Professor": "Karen Cristina Fernandes Donatti"},
        {"Ciclo": "1ª Etapa", "Turma": "1ª Etapa 3", "Disciplina": "Educação Física", "Professor": "Fernando Indig Bongiovanni"},
        {"Ciclo": "1ª Etapa", "Turma": "1ª Etapa 3", "Disciplina": "Linguagens e Tecnologias", "Professor": "Fernando Indig Bongiovanni"},
        
        {"Ciclo": "2ª Etapa", "Turma": "2ª Etapa 1", "Disciplina": "Professor de Ed. Infantil", "Professor": "Keila Maria Ribeiro dos Santos"},
        {"Ciclo": "2ª Etapa", "Turma": "2ª Etapa 1", "Disciplina": "Artes", "Professor": "Jordana Lima Alvez"},
        {"Ciclo": "2ª Etapa", "Turma": "2ª Etapa 1", "Disciplina": "Educação Física", "Professor": "Fernando Indig Bongiovanni"},
        {"Ciclo": "2ª Etapa", "Turma": "2ª Etapa 1", "Disciplina": "Linguagens e Tecnologias", "Professor": "Elaine Cristina Neves Fahl"},
        {"Ciclo": "2ª Etapa", "Turma": "2ª Etapa 2", "Disciplina": "Professor de Ed. Infantil", "Professor": "Sandra Maria Ribeiro dos Santos Brito"},
        {"Ciclo": "2ª Etapa", "Turma": "2ª Etapa 2", "Disciplina": "Artes", "Professor": "Jordana Lima Alvez"},
        {"Ciclo": "2ª Etapa", "Turma": "2ª Etapa 2", "Disciplina": "Educação Física", "Professor": "Fernando Indig Bongiovanni"},
        {"Ciclo": "2ª Etapa", "Turma": "2ª Etapa 2", "Disciplina": "Linguagens e Tecnologias", "Professor": "Elaine Cristina Neves Fahl"},
        {"Ciclo": "2ª Etapa", "Turma": "2ª Etapa 3", "Disciplina": "Professor de Ed. Infantil", "Professor": "Adriana Mello Costa"},
        {"Ciclo": "2ª Etapa", "Turma": "2ª Etapa 3", "Disciplina": "Artes", "Professor": "Jordana Lima Alvez"},
        {"Ciclo": "2ª Etapa", "Turma": "2ª Etapa 3", "Disciplina": "Educação Física", "Professor": "Fernando Indig Bongiovanni"},
        {"Ciclo": "2ª Etapa", "Turma": "2ª Etapa 3", "Disciplina": "Linguagens e Tecnologias", "Professor": "Elaine Cristina Neves Fahl"},
        
        {"Ciclo": "Maternal II", "Turma": "Maternal II 1", "Disciplina": "Professor de Ed. Infantil", "Professor": "Fernanda Dumit Graf"},
        {"Ciclo": "Maternal II", "Turma": "Maternal II 1", "Disciplina": "Artes", "Professor": "Bruna Thais Bernini Guedes"},
        {"Ciclo": "Maternal II", "Turma": "Maternal II 1", "Disciplina": "Educação Física", "Professor": "Fernando Indig Bongiovanni"},
        {"Ciclo": "Maternal II", "Turma": "Maternal II 1", "Disciplina": "Linguagens e Tecnologias", "Professor": "Karina Brum Rivabene Gracini"},
        {"Ciclo": "Maternal II", "Turma": "Maternal II 2", "Disciplina": "Professor de Ed. Infantil", "Professor": "Lucineide Almeida da Silva"},
        {"Ciclo": "Maternal II", "Turma": "Maternal II 2", "Disciplina": "Artes", "Professor": ""},
        {"Ciclo": "Maternal II", "Turma": "Maternal II 2", "Disciplina": "Educação Física", "Professor": "Fernando Indig Bongiovanni"},
        {"Ciclo": "Maternal II", "Turma": "Maternal II 2", "Disciplina": "Linguagens e Tecnologias", "Professor": "Elaine Cristina Neves Fahl"}
    ]
    matriz_json = get_config("matriz_professores", "")
    df_matriz = pd.DataFrame(json.loads(matriz_json)) if matriz_json else pd.DataFrame(MATRIZ_SEED)
    
    # Matriz Gestão Semente
    GESTAO_SEED = [
        {"Nome": "Luciana Lopes Faber", "Cargo": "Prof. Coordenador"},
        {"Nome": "Oelen Fernando Pedro", "Cargo": "Prof. Coordenador"},
        {"Nome": "Luciana Martinati Tetzner", "Cargo": "Vice-Diretor"},
        {"Nome": "Noreh Cristina Heldt Aldrigui", "Cargo": "Vice-Diretor"},
        {"Nome": "Marília Motta Camargo dos Reis", "Cargo": "Vice-Diretor"},
        {"Nome": "José Victor Souza Gallo", "Cargo": "Diretor de Escola"}
    ]
    gestao_json = get_config("matriz_gestao", "")
    df_gestao = pd.DataFrame(json.loads(gestao_json)) if gestao_json else pd.DataFrame(GESTAO_SEED)


    # ==============================================================================
    # 1. TELA: NOVA ATA DE CONSELHO
    # ==============================================================================
    if app_mode_regular == "📝 Nova Ata de Conselho":
        st.markdown(f"""<div class="header-box"><div class="header-title">Conselho de Classe / Termo</div><div class="header-subtitle">{modalidade_ata}</div></div>""", unsafe_allow_html=True)
        
        # ------------------------------------------------------------------------------
        # MÓDULO: ENSINO FUNDAMENTAL
        # ------------------------------------------------------------------------------
        if "Fundamental" in modalidade_ata:
            if 'data_ata_ef' not in st.session_state:
                st.session_state.data_ata_ef = {
                    'abaixo_basico': [{"Estudante": "", "LP": "", "M": "", "H": "", "G": "", "C": "", "A": "", "EF": "", "LT": "", "LIBRAS": ""}],
                    'basico': [{"Estudante": "", "Ações (LP e Mat)": ""}],
                    'obs_especiais': [{"Estudante": "", "Desempenho/Observação": ""}],
                    'encaminhamentos': [{"Estudante": "", "Motivo": ""}],
                    'mat_tardia': [{"Estudante": "", "Data Matrícula": "", "Total Frequência": ""}],
                    'obs_outras': "",
                    'assinaturas': [{"Nome": "", "Cargo/Atuação": ""}]
                }
                
            if 'ata_turma_confirmada' not in st.session_state:
                st.session_state.ata_turma_confirmada = None
                st.session_state.ata_ciclo_confirmado = None
            
            data_ata = st.session_state.data_ata_ef
            
            for key in ['abaixo_basico', 'basico', 'obs_especiais', 'encaminhamentos', 'mat_tardia', 'assinaturas']:
                if isinstance(data_ata.get(key), pd.DataFrame):
                    data_ata[key] = data_ata[key].to_dict('records')
            
            # --- PORTÃO DE ENTRADA (GATE) ---
            if not st.session_state.ata_turma_confirmada:
                st.markdown("""
                <div style='background-color: white; padding: 2rem; border-radius: 10px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); border: 1px solid #e2e8f0;'>
                    <h3 style='color: #1e293b; margin-top:0;'>🚪 Selecione a Turma</h3>
                    <p style='color: #64748b;'>Para iniciar o preenchimento de uma nova ata, selecione abaixo o Ciclo e a Turma.</p>
                </div>
                <br>
                """, unsafe_allow_html=True)
                
                c_c, c_t = st.columns(2)
                ciclo_sel = c_c.selectbox("1. Selecione o Ciclo:", ["Ciclo I (1º ao 3º ano)", "Ciclo II (4º e 5º ano)"])
                
                turmas_bd = df_matriz[df_matriz['Ciclo'] == ciclo_sel]['Turma'].unique().tolist()
                turmas_disp = turmas_bd + ["Outra Turma..."]
                
                turma_sel = c_t.selectbox("2. Selecione a Turma:", turmas_disp)
                
                if turma_sel == "Outra Turma...":
                    turma_sel = st.text_input("Digite o nome da turma:")
                
                st.write("")
                if st.button("✅ Confirmar e Acessar Formulário", type="primary", use_container_width=True):
                    if turma_sel:
                        st.session_state.ata_ciclo_confirmado = ciclo_sel
                        st.session_state.ata_turma_confirmada = turma_sel
                        data_ata['ciclo'] = ciclo_sel
                        data_ata['turma'] = turma_sel
                        st.rerun()
                    else:
                        st.warning("⚠️ Por favor, informe o nome da turma.")
            
            # --- FORMULÁRIO DO FUNDAMENTAL ---
            else:
                c_info, c_btn = st.columns([4, 1])
                c_info.success(f"📌 **Ata em edição:** {st.session_state.ata_ciclo_confirmado} - {st.session_state.ata_turma_confirmada}")
                if c_btn.button("⬅️ Trocar Turma", use_container_width=True):
                    st.session_state.ata_turma_confirmada = None
                    st.rerun()
                    
                tabs = st.tabs(["1. Identificação", "2. Síntese", "3. Plano de Ação", "4. Observações", "5. Finalização"])
                
                with tabs[0]:
                    st.subheader("Dados da Unidade e Ciclo")
                    c1, c2, c3 = st.columns([2, 1, 1])
                    data_ata['escola'] = c1.text_input("Unidade Escolar", value=data_ata.get('escola', "CEIEF Rafael Affonso Leite"))
                    
                    tri_opts = ["1º Trimestre", "2º Trimestre", "3º Trimestre"]
                    tri_idx = tri_opts.index(data_ata.get('trimestre', "1º Trimestre")) if data_ata.get('trimestre') in tri_opts else 0
                    data_ata['trimestre'] = c2.selectbox("Trimestre", tri_opts, index=tri_idx)
                    
                    data_ata['ano_letivo'] = c3.text_input("Ano Letivo", value=data_ata.get('ano_letivo', str(date.today().year)))
                    
                    st.markdown("---")
                    c4, c5 = st.columns(2)
                    c4.text_input("Turma/Ano", value=st.session_state.ata_turma_confirmada, disabled=True)
                    c5.text_input("Ciclo", value=st.session_state.ata_ciclo_confirmado, disabled=True)

                with tabs[1]:
                    st.subheader("Síntese Avaliativa da Classe")
                    st.info("Descreva o desempenho alcançado pela classe em cada componente curricular no trimestre atual.")
                    
                    c_lp, c_mat = st.columns(2)
                    data_ata['sin_lp'] = c_lp.text_area("Língua Portuguesa", value=data_ata.get('sin_lp', ''), height=120)
                    data_ata['sin_mat'] = c_mat.text_area("Matemática", value=data_ata.get('sin_mat', ''), height=120)
                    
                    c_h, c_g = st.columns(2)
                    data_ata['sin_hist'] = c_h.text_area("História", value=data_ata.get('sin_hist', ''), height=120)
                    data_ata['sin_geo'] = c_g.text_area("Geografia", value=data_ata.get('sin_geo', ''), height=120)
                    
                    c_c, c_a = st.columns(2)
                    data_ata['sin_cien'] = c_c.text_area("Ciências", value=data_ata.get('sin_cien', ''), height=120)
                    data_ata['sin_arte'] = c_a.text_area("Arte", value=data_ata.get('sin_arte', ''), height=120)
                    
                    c_ef, c_lt = st.columns(2)
                    data_ata['sin_ef'] = c_ef.text_area("Educação Física", value=data_ata.get('sin_ef', ''), height=120)
                    data_ata['sin_lt'] = c_lt.text_area("Linguagens e Tecnologias", value=data_ata.get('sin_lt', ''), height=120)
                    
                    data_ata['sin_libras'] = st.text_area("Libras", value=data_ata.get('sin_libras', ''), height=120)

                with tabs[2]:
                    st.subheader("Plano de Ação (Abaixo do Básico)")
                    st.caption("Insira os **números das propostas de recuperação** nas disciplinas correspondentes (ex: 1, 3, 10).")
                    
                    for i, row in enumerate(data_ata['abaixo_basico']):
                        with st.container():
                            st.markdown(f"**Estudante {i+1}**")
                            c_est, c_del = st.columns([11, 1])
                            row['Estudante'] = c_est.text_input(f"Nome do Estudante", value=row.get('Estudante', ''), key=f"ab_est_{i}", label_visibility="collapsed", placeholder="Nome do Aluno")
                            if c_del.button("🗑️", key=f"del_ab_{i}", help="Excluir Linha"):
                                data_ata['abaixo_basico'].pop(i); st.rerun()
                            
                            cc = st.columns(9)
                            row['LP'] = cc[0].text_input("LP", value=row.get('LP', ''), key=f"ab_lp_{i}")
                            row['M'] = cc[1].text_input("M", value=row.get('M', ''), key=f"ab_m_{i}")
                            row['H'] = cc[2].text_input("H", value=row.get('H', ''), key=f"ab_h_{i}")
                            row['G'] = cc[3].text_input("G", value=row.get('G', ''), key=f"ab_g_{i}")
                            row['C'] = cc[4].text_input("C", value=row.get('C', ''), key=f"ab_c_{i}")
                            row['A'] = cc[5].text_input("A", value=row.get('A', ''), key=f"ab_a_{i}")
                            row['EF'] = cc[6].text_input("EF", value=row.get('EF', ''), key=f"ab_ef_{i}")
                            row['LT'] = cc[7].text_input("LT", value=row.get('LT', ''), key=f"ab_lt_{i}")
                            row['LIBRAS'] = cc[8].text_input("LIB", value=row.get('LIBRAS', ''), key=f"ab_lib_{i}")
                            st.markdown("<hr style='margin: 10px 0;'>", unsafe_allow_html=True)
                    
                    if st.button("➕ Adicionar Novo Estudante", key="add_ab"):
                        data_ata['abaixo_basico'].append({"Estudante": "", "LP": "", "M": "", "H": "", "G": "", "C": "", "A": "", "EF": "", "LT": "", "LIBRAS": ""})
                        st.rerun()
                    
                    st.markdown("<br>", unsafe_allow_html=True)
                    st.markdown("**Propostas de Recuperação da Gestão:**")
                    st.markdown(propostas_ata_ef)
                    
                    st.divider()
                    st.subheader("Plano de Ação (Básico)")
                    
                    for i, row in enumerate(data_ata['basico']):
                        c1, c2, c3 = st.columns([4, 6, 1])
                        row['Estudante'] = c1.text_input("Estudante", value=row.get('Estudante', ''), key=f"bas_est_{i}", placeholder="Nome")
                        row['Ações (LP e Mat)'] = c2.text_area("Ações (LP e Matemática)", value=row.get('Ações (LP e Mat)', ''), key=f"bas_ac_{i}", height=68)
                        if c3.button("🗑️", key=f"del_bas_{i}"):
                            data_ata['basico'].pop(i); st.rerun()
                            
                    if st.button("➕ Adicionar Estudante no Básico", key="add_bas"):
                        data_ata['basico'].append({"Estudante": "", "Ações (LP e Mat)": ""})
                        st.rerun()

                with tabs[3]:
                    st.subheader("3. Observações Gerais")
                    
                    st.markdown("**a) Desempenho de alunos especiais (laudados)**")
                    for i, row in enumerate(data_ata['obs_especiais']):
                        c1, c2, c3 = st.columns([3, 6, 1])
                        row['Estudante'] = c1.text_input("Estudante Especial", value=row.get('Estudante', ''), key=f"obs_est_{i}")
                        row['Desempenho/Observação'] = c2.text_area("Desempenho/Observações", value=row.get('Desempenho/Observação', ''), key=f"obs_des_{i}", height=68)
                        if c3.button("🗑️", key=f"del_obs_{i}"):
                            data_ata['obs_especiais'].pop(i); st.rerun()
                    if st.button("➕ Adicionar Aluno Especial", key="add_obs"):
                        data_ata['obs_especiais'].append({"Estudante": "", "Desempenho/Observação": ""})
                        st.rerun()
                    
                    st.divider()
                    st.markdown("**b) Alunos encaminhados (Conselho Tutelar ou Serviço Social)**")
                    for i, row in enumerate(data_ata['encaminhamentos']):
                        c1, c2, c3 = st.columns([3, 6, 1])
                        row['Estudante'] = c1.text_input("Estudante Encaminhado", value=row.get('Estudante', ''), key=f"enc_est_{i}")
                        row['Motivo'] = c2.text_area("Motivo do Encaminhamento", value=row.get('Motivo', ''), key=f"enc_mot_{i}", height=68)
                        if c3.button("🗑️", key=f"del_enc_{i}"):
                            data_ata['encaminhamentos'].pop(i); st.rerun()
                    if st.button("➕ Adicionar Encaminhamento", key="add_enc"):
                        data_ata['encaminhamentos'].append({"Estudante": "", "Motivo": ""})
                        st.rerun()

                    st.divider()
                    st.markdown("**c) Estudantes Matriculados Tardiamente**")
                    for i, row in enumerate(data_ata['mat_tardia']):
                        c1, c2, c3, c4 = st.columns([4, 3, 3, 1])
                        row['Estudante'] = c1.text_input("Estudante", value=row.get('Estudante', ''), key=f"mat_est_{i}")
                        row['Data Matrícula'] = c2.text_input("Data da Matrícula", value=row.get('Data Matrícula', ''), key=f"mat_data_{i}")
                        row['Total Frequência'] = c3.text_input("Frequência (Dias)", value=row.get('Total Frequência', ''), key=f"mat_freq_{i}")
                        if c4.button("🗑️", key=f"del_mat_{i}"):
                            data_ata['mat_tardia'].pop(i); st.rerun()
                    if st.button("➕ Adicionar Matrícula Tardia", key="add_mat"):
                        data_ata['mat_tardia'].append({"Estudante": "", "Data Matrícula": "", "Total Frequência": ""})
                        st.rerun()
                        
                    st.divider()
                    st.markdown("**d) Outras Observações**")
                    data_ata['obs_outras'] = st.text_area("Campo livre para quaisquer outras observações da turma:", value=data_ata.get('obs_outras', ''), height=120)

                with tabs[4]:
                    st.subheader("Finalização e Assinaturas")
                    
                    if st.button("🤖 Preencher Assinaturas Automaticamente", type="primary"):
                        ciclo_atual = st.session_state.ata_ciclo_confirmado
                        turma_atual = st.session_state.ata_turma_confirmada
                        
                        df_turma = df_matriz[(df_matriz['Ciclo'] == ciclo_atual) & (df_matriz['Turma'] == turma_atual)]
                        
                        if not df_turma.empty:
                            lista_final = []
                            professores_adicionados = set()
                            
                            for _, row in df_turma.iterrows():
                                materia = row['Disciplina']
                                nome_prof = row['Professor']
                                if nome_prof and nome_prof not in professores_adicionados:
                                    cargo_formatado = "Prof. Polivalente" if materia == "Polivalente" else f"Prof. de {materia}"
                                    lista_final.append({"Nome": nome_prof, "Cargo/Atuação": f"{cargo_formatado} (Atuante na Turma)"})
                                    professores_adicionados.add(nome_prof)
                                    
                            lista_final.append({"Nome": "", "Cargo/Atuação": "Prof. de Libras (Atuante na Turma)"})
                            
                            df_ciclo = df_matriz[(df_matriz['Ciclo'] == ciclo_atual) & (df_matriz['Turma'] != turma_atual)]
                            for _, row in df_ciclo.iterrows():
                                materia = row['Disciplina']
                                nome_prof = row['Professor']
                                if nome_prof and nome_prof not in professores_adicionados:
                                    cargo_formatado = "Prof. Polivalente" if materia == "Polivalente" else f"Prof. de {materia}"
                                    lista_final.append({"Nome": nome_prof, "Cargo/Atuação": f"{cargo_formatado} (Atuante no Ciclo)"})
                                    professores_adicionados.add(nome_prof)
                                            
                            for _, row in df_gestao.iterrows():
                                if row['Nome']:
                                    lista_final.append({"Nome": row['Nome'], "Cargo/Atuação": row['Cargo']})
                            
                            data_ata['assinaturas'] = lista_final
                            st.success("✅ Grade de assinaturas preenchida com sucesso para a turma selecionada!")
                            st.rerun()
                        else:
                            st.error("A turma atual não foi encontrada na Matriz de Automação (Acesse a aba 'Configurações' para verificar).")

                    st.divider()
                    st.markdown("**Participantes da Reunião**")
                    st.caption("Você pode usar as setinhas (⬆️/⬇️) para mudar a ordem que os nomes vão aparecer na folha do PDF.")
                    
                    if 'assinaturas' not in data_ata:
                        data_ata['assinaturas'] = [{"Nome": "", "Cargo/Atuação": ""}]
                        
                    for i, row in enumerate(data_ata['assinaturas']):
                        c1, c2, c3 = st.columns([5, 4, 2])
                        row['Nome'] = c1.text_input("Nome", value=row.get('Nome', ''), key=f"sig_nome_{i}", placeholder="Ex: Ana Silva", label_visibility="collapsed")
                        row['Cargo/Atuação'] = c2.text_input("Cargo", value=row.get('Cargo/Atuação', ''), key=f"sig_cargo_{i}", placeholder="Ex: Diretor de Escola", label_visibility="collapsed")
                        
                        b1, b2, b3 = c3.columns(3)
                        if b1.button("⬆️", key=f"up_sig_{i}", disabled=(i == 0), help="Mover para cima"):
                            data_ata['assinaturas'][i], data_ata['assinaturas'][i-1] = data_ata['assinaturas'][i-1], data_ata['assinaturas'][i]
                            st.rerun()
                        if b2.button("⬇️", key=f"dw_sig_{i}", disabled=(i == len(data_ata['assinaturas']) - 1), help="Mover para baixo"):
                            data_ata['assinaturas'][i], data_ata['assinaturas'][i+1] = data_ata['assinaturas'][i+1], data_ata['assinaturas'][i]
                            st.rerun()
                        if b3.button("🗑️", key=f"del_sig_{i}", help="Excluir assinatura"):
                            data_ata['assinaturas'].pop(i)
                            st.rerun()
                            
                    if st.button("➕ Adicionar Participante Manualmente", key="add_sig"):
                        data_ata['assinaturas'].append({"Nome": "", "Cargo/Atuação": ""})
                        st.rerun()
                        
                    st.divider()
                    
                    if st.button("💾 Salvar Ata", use_container_width=True, type="secondary"):
                        try:
                            dados_para_salvar = {}
                            for key, value in data_ata.items():
                                if isinstance(value, pd.DataFrame):
                                    dados_para_salvar[key] = value.to_dict(orient='records')
                                else:
                                    dados_para_salvar[key] = value
                            
                            novo_json = json.dumps(dados_para_salvar, ensure_ascii=False)
                            id_ata = f"{data_ata.get('turma', 'SemTurma')} - {data_ata.get('trimestre', 'SemTri')} ({modalidade_ata})"
                            df_atas = safe_read("Atas_Conselho", ["id_ata", "modalidade", "turma", "dados_json"])
                            
                            if not df_atas.empty and "id_ata" in df_atas.columns and id_ata in df_atas["id_ata"].values:
                                df_atas.loc[df_atas["id_ata"] == id_ata, "dados_json"] = novo_json
                            else:
                                novo_registro = {"id_ata": id_ata, "modalidade": modalidade_ata, "turma": data_ata.get('turma', ''), "dados_json": novo_json}
                                df_atas = pd.concat([df_atas, pd.DataFrame([novo_registro])], ignore_index=True)
                            
                            safe_update("Atas_Conselho", df_atas)
                            st.success(f"✅ Ata salva com sucesso!")
                        except Exception as e:
                            st.error(f"Erro ao salvar: {e}")

                    if st.button("👁️ GERAR ATA COMPLETA (PDF)", type="primary", use_container_width=True):
                        try:
                            pdf = OfficialPDF('P', 'mm', 'A4')
                            pdf.doc_type = "Ata" 
                            pdf.set_margins(15, 35, 15)
                            pdf.set_auto_page_break(auto=True, margin=20)
                            pdf.add_page()
                            
                            def calc_lines(txt, w):
                                if not txt: return 1
                                lines = 0
                                for par in txt.split('\n'):
                                    words = par.split(' ')
                                    curr_w = 0
                                    for word in words:
                                        word_w = pdf.get_string_width(word + ' ')
                                        if curr_w + word_w > w:
                                            lines += 1; curr_w = word_w
                                        else:
                                            curr_w += word_w
                                    lines += 1
                                return lines
                            
                            # --- CABEÇALHO ---
                            pdf.set_font("Arial", "B", 10)
                            escola_nome = data_ata.get('escola', 'CEIEF Rafael Affonso Leite').upper()
                            pdf.set_x(15)
                            pdf.cell(180, 5, clean_pdf_text(escola_nome), 0, 1, 'C')
                            
                            trimestre = data_ata.get('trimestre', '').upper()
                            ano = data_ata.get('ano_letivo', '')
                            pdf.set_x(15)
                            pdf.cell(180, 5, clean_pdf_text(f"ATA DESCRITIVA DO CONSELHO DE CICLO/TERMO - {trimestre} DE {ano}"), 0, 1, 'C')
                            
                            turma = data_ata.get('turma', '')
                            ciclo = data_ata.get('ciclo', '')
                            pdf.set_x(15)
                            pdf.cell(180, 5, clean_pdf_text(f"Turma: {turma} | Ciclo: {ciclo}"), 0, 1, 'C')
                            pdf.ln(5)
                            
                            # ==============================================================================
                            # INÍCIO DA SUPER CAIXA
                            # ==============================================================================
                            
                            # --- 1. SÍNTESE AVALIATIVA ---
                            pdf.set_font("Arial", "B", 10)
                            pdf.set_fill_color(220, 220, 220)
                            pdf.set_x(15)
                            pdf.cell(180, 6, "SÍNTESE AVALIATIVA", "LTR", 1, 'C', True)
                            
                            pdf.set_x(15)
                            pdf.cell(180, 2, "", "LR", 1) 
                            
                            pdf.set_font("Arial", "", 10)
                            pdf.set_x(15)
                            pdf.multi_cell(180, 5, clean_pdf_text(texto_base_ata_ef), "LR", 'J')
                            
                            pdf.set_x(15)
                            pdf.cell(180, 4, "", "LR", 1)
                            
                            pdf.set_font("Arial", "B", 10)
                            pdf.set_x(15)
                            pdf.cell(180, 5, clean_pdf_text("1- Síntese avaliativa da classe:"), "LR", 1, 'L')
                            
                            texto_sint = "a partir dos diferentes instrumentos avaliativos e da análise dos resultados, o desempenho alcançado pela classe em cada componente curricular no trimestre atual se apresenta da seguinte forma:"
                            pdf.set_font("Arial", "", 10)
                            pdf.set_x(15)
                            pdf.multi_cell(180, 5, clean_pdf_text(texto_sint), "LR", 'J')
                            
                            pdf.set_x(15)
                            pdf.cell(180, 4, "", "LR", 1)
                            
                            disciplinas = [
                                ("Língua Portuguesa", data_ata.get('sin_lp', '')),
                                ("Matemática", data_ata.get('sin_mat', '')),
                                ("História", data_ata.get('sin_hist', '')),
                                ("Geografia", data_ata.get('sin_geo', '')),
                                ("Ciências", data_ata.get('sin_cien', '')),
                                ("Arte", data_ata.get('sin_arte', '')),
                                ("Educação Física", data_ata.get('sin_ef', '')),
                                ("Linguagens e Tecnologias", data_ata.get('sin_lt', '')),
                                ("Libras", data_ata.get('sin_libras', ''))
                            ]
                            
                            for i, (nome, texto) in enumerate(disciplinas):
                                if texto.strip() != "":
                                    pdf.set_font("Arial", "B", 10)
                                    pdf.set_x(15)
                                    pdf.cell(180, 5, clean_pdf_text(f"  {chr(149)}  {nome}:"), "LR", 1, 'L')
                                    
                                    pdf.set_font("Arial", "", 10)
                                    pdf.set_x(15)
                                    pdf.multi_cell(180, 5, clean_pdf_text(f"  {texto}"), "LR", 'J')
                                
                                if i < len(disciplinas) - 1:
                                    pdf.set_x(15)
                                    pdf.cell(180, 4, "", "LR", 1) 
                            
                            # --- 2. PLANO DE AÇÃO ---
                            pdf.set_x(15)
                            pdf.cell(180, 5, "", "LR", 1) 
                            
                            if pdf.get_y() > 230: pdf.add_page()
                            
                            pdf.set_font("Arial", "B", 10)
                            pdf.set_x(15)
                            pdf.cell(180, 5, clean_pdf_text("2- Plano de Ação para os estudantes de acordo com desempenho:"), "LR", 1, 'L')
                            
                            pdf.set_x(15)
                            pdf.cell(180, 3, "", "LR", 1)
                            
                            pdf.set_font("Arial", "B", 10)
                            pdf.set_x(15)
                            pdf.cell(180, 5, clean_pdf_text("-Estudantes com desempenho Abaixo do Básico:"), "LR", 1, 'L')
                            
                            pdf.set_x(15)
                            pdf.cell(180, 2, "", "LR", 1)
                            
                            pdf.set_font("Arial", "B", 10)
                            col_w = [54, 14, 14, 14, 14, 14, 14, 14, 14, 14]
                            headers = ["Estudante", "LP", "M", "H", "G", "C", "A", "EF", "LT", "LIB"]
                            pdf.set_x(15)
                            for i, h in enumerate(headers):
                                pdf.cell(col_w[i], 6, h, 1, 0, 'C')
                            pdf.ln()
                            
                            pdf.set_font("Arial", "", 10)
                            lista_abaixo = data_ata.get('abaixo_basico', [])
                                
                            def truncate_str(texto, max_w):
                                while pdf.get_string_width(texto) > max_w - 2: texto = texto[:-1]
                                return texto

                            for row in lista_abaixo:
                                estudante = str(row.get('Estudante', '')).strip()
                                if estudante: 
                                    pdf.set_x(15)
                                    estudante_seguro = truncate_str(estudante, col_w[0])
                                    pdf.cell(col_w[0], 6, clean_pdf_text(estudante_seguro), 1, 0, 'L')
                                    pdf.cell(col_w[1], 6, clean_pdf_text(str(row.get('LP', ''))), 1, 0, 'C')
                                    pdf.cell(col_w[2], 6, clean_pdf_text(str(row.get('M', ''))), 1, 0, 'C')
                                    pdf.cell(col_w[3], 6, clean_pdf_text(str(row.get('H', ''))), 1, 0, 'C')
                                    pdf.cell(col_w[4], 6, clean_pdf_text(str(row.get('G', ''))), 1, 0, 'C')
                                    pdf.cell(col_w[5], 6, clean_pdf_text(str(row.get('C', ''))), 1, 0, 'C')
                                    pdf.cell(col_w[6], 6, clean_pdf_text(str(row.get('A', ''))), 1, 0, 'C')
                                    pdf.cell(col_w[7], 6, clean_pdf_text(str(row.get('EF', ''))), 1, 0, 'C')
                                    pdf.cell(col_w[8], 6, clean_pdf_text(str(row.get('LT', ''))), 1, 0, 'C')
                                    pdf.cell(col_w[9], 6, clean_pdf_text(str(row.get('LIBRAS', ''))), 1, 1, 'C')
                            
                            pdf.set_x(15)
                            pdf.cell(180, 3, "", "LR", 1) 
                            
                            pdf.set_font("Arial", "B", 10)
                            pdf.set_x(15)
                            pdf.cell(180, 5, clean_pdf_text("*Propostas de Recuperação:"), "LR", 1, 'L')
                            pdf.set_font("Arial", "", 10)
                            
                            for prop in propostas_ata_ef.split('\n'):
                                if prop.strip():
                                    pdf.set_x(15)
                                    pdf.multi_cell(180, 5, clean_pdf_text(prop.strip()), "LR", 'J')

                            pdf.set_x(15)
                            pdf.cell(180, 5, "", "LR", 1) 
                            
                            if pdf.get_y() > 230: pdf.add_page()
                            
                            pdf.set_font("Arial", "B", 10)
                            pdf.set_x(15)
                            pdf.cell(180, 5, clean_pdf_text("-Estudantes com desempenho Básico:"), "LR", 1, 'L')
                            
                            pdf.set_x(15)
                            pdf.cell(180, 2, "", "LR", 1) 
                            
                            pdf.set_font("Arial", "B", 10)
                            y = pdf.get_y()
                            pdf.rect(15, y, 60, 10)
                            pdf.set_xy(15, y+2)
                            pdf.cell(60, 6, "Estudantes", 0, 0, 'C')
                            
                            pdf.rect(75, y, 120, 10)
                            pdf.set_xy(75, y+1)
                            pdf.multi_cell(120, 4, clean_pdf_text("Ações que serão desenvolvidas nas áreas de Língua Portuguesa e Matemática."), 0, 'C')
                            pdf.set_xy(15, y+10)
                            
                            pdf.set_font("Arial", "", 10)
                            lista_basico = data_ata.get('basico', [])

                            for row in lista_basico:
                                estudante = str(row.get('Estudante', '')).strip()
                                if estudante:
                                    y = pdf.get_y()
                                    texto_acao = str(row.get('Ações (LP e Mat)', ''))
                                    
                                    linhas_est = calc_lines(estudante, 58)
                                    linhas_acao = calc_lines(texto_acao, 118)
                                    h_row = max(6, max(linhas_est, linhas_acao) * 5 + 4)
                                    
                                    if y + h_row > 265:
                                        pdf.add_page()
                                        y = pdf.get_y()
                                    
                                    pdf.rect(15, y, 60, h_row)
                                    pdf.rect(75, y, 120, h_row)
                                    pdf.set_xy(15, y+2)
                                    pdf.multi_cell(60, 5, clean_pdf_text(estudante), 0, 'L')
                                    pdf.set_xy(75, y+2)
                                    pdf.multi_cell(120, 5, clean_pdf_text(texto_acao), 0, 'J')
                                    pdf.set_xy(15, y + h_row)

                            # --- 3. OBSERVAÇÕES GERAIS ---
                            pdf.set_x(15)
                            pdf.cell(180, 5, "", "LR", 1) 
                            
                            if pdf.get_y() > 230: pdf.add_page()
                            
                            pdf.set_font("Arial", "B", 10)
                            pdf.set_x(15)
                            pdf.cell(180, 6, clean_pdf_text("3- Observações Gerais:"), "LR", 1, 'L')
                            pdf.set_font("Arial", "", 10)
                            
                            prefix_code = 97 
                            
                            lista_esp = data_ata.get('obs_especiais', [])
                            esp_valid = [r for r in lista_esp if str(r.get('Estudante', '')).strip()]
                            if esp_valid:
                                pdf.set_font("Arial", "B", 10)
                                pdf.set_x(15)
                                pdf.cell(180, 5, clean_pdf_text(f"{chr(prefix_code)}) Desempenho de alunos especiais (laudados):"), "LR", 1, 'L')
                                prefix_code += 1
                                for row in esp_valid:
                                    est = str(row.get('Estudante', '')).strip()
                                    obs = str(row.get('Desempenho/Observação', '')).strip()
                                    pdf.set_font("Arial", "B", 10)
                                    pdf.set_x(15)
                                    pdf.cell(180, 5, clean_pdf_text(f"  {chr(149)}  {est}:"), "LR", 1, 'L')
                                    pdf.set_font("Arial", "", 10)
                                    pdf.set_x(15)
                                    pdf.multi_cell(180, 5, clean_pdf_text(f"  {obs}"), "LR", 'J')
                                pdf.set_x(15)
                                pdf.cell(180, 2, "", "LR", 1)
                            
                            lista_enc = data_ata.get('encaminhamentos', [])
                            enc_valid = [r for r in lista_enc if str(r.get('Estudante', '')).strip()]
                            if enc_valid:
                                pdf.set_font("Arial", "B", 10)
                                pdf.set_x(15)
                                pdf.cell(180, 5, clean_pdf_text(f"{chr(prefix_code)}) Alunos encaminhados (Conselho Tutelar/Serv. Social):"), "LR", 1, 'L')
                                prefix_code += 1
                                for row in enc_valid:
                                    est = str(row.get('Estudante', '')).strip()
                                    mot = str(row.get('Motivo', '')).strip()
                                    pdf.set_font("Arial", "B", 10)
                                    pdf.set_x(15)
                                    pdf.cell(180, 5, clean_pdf_text(f"  {chr(149)}  {est}:"), "LR", 1, 'L')
                                    pdf.set_font("Arial", "", 10)
                                    pdf.set_x(15)
                                    pdf.multi_cell(180, 5, clean_pdf_text(f"  {mot}"), "LR", 'J')
                                pdf.set_x(15)
                                pdf.cell(180, 2, "", "LR", 1)

                            lista_tardia = data_ata.get('mat_tardia', [])
                            tardia_valid = [r for r in lista_tardia if str(r.get('Estudante', '')).strip()]
                            if len(tardia_valid) > 0:
                                pdf.set_font("Arial", "B", 10)
                                pdf.set_x(15)
                                pdf.cell(180, 5, clean_pdf_text(f"{chr(prefix_code)}) Estudantes Matriculados Tardiamente:"), "LR", 1, 'L')
                                prefix_code += 1
                                for row in tardia_valid:
                                    est = str(row.get('Estudante', '')).strip()
                                    mat = str(row.get('Data Matrícula', '')).strip()
                                    freq = str(row.get('Total Frequência', '')).strip()
                                    pdf.set_font("Arial", "B", 10)
                                    pdf.set_x(15)
                                    pdf.cell(180, 5, clean_pdf_text(f"  {chr(149)}  {est}:"), "LR", 1, 'L')
                                    texto_tardio = f"Matriculado(a) nesta sala em {mat}. Portanto, obteve um total de frequência de {freq} dias letivos."
                                    pdf.set_font("Arial", "", 10)
                                    pdf.set_x(15)
                                    pdf.multi_cell(180, 5, clean_pdf_text(f"  {texto_tardio}"), "LR", 'J')
                            else:
                                pdf.set_font("Arial", "", 10)
                                pdf.set_x(15)
                                pdf.cell(180, 5, f" {chr(prefix_code)}) Sem matrículas tardias registradas no período.", "LR", 1)
                                prefix_code += 1

                            pdf.set_x(15)
                            pdf.cell(180, 2, "", "LR", 1)
                            
                            obs_outras = data_ata.get('obs_outras', '').strip()
                            if obs_outras:
                                pdf.set_font("Arial", "B", 10)
                                pdf.set_x(15)
                                pdf.cell(180, 5, clean_pdf_text(f"{chr(prefix_code)}) Outras Observações:"), "LR", 1, 'L')
                                pdf.set_font("Arial", "", 10)
                                pdf.set_x(15)
                                pdf.multi_cell(180, 5, clean_pdf_text(f"  {obs_outras}"), "LR", 'J')
                                
                            pdf.set_x(15)
                            pdf.cell(180, 3, "", "LRB", 1) 

                            # --- ASSINATURAS ---
                            pdf.ln(5)
                            if pdf.get_y() > 220: pdf.add_page()
                            
                            pdf.set_font("Arial", "B", 10)
                            pdf.set_fill_color(220, 220, 220)
                            pdf.set_x(15)
                            pdf.cell(180, 6, "ASSINATURA DOS PARTICIPANTES NA REUNIÃO DO CONSELHO DE CICLO", 1, 1, 'C', True)
                            
                            lista_assinaturas = data_ata.get('assinaturas', [])
                            sigs_validas = [s for s in lista_assinaturas if str(s.get('Nome', '')).strip()]
                            
                            if not sigs_validas:
                                pdf.set_x(15)
                                pdf.cell(180, 20, "Nenhuma assinatura cadastrada para esta ata.", 1, 1, 'C')
                            else:
                                cols = 4
                                cell_w = 180 / cols
                                cell_h = 24 
                                
                                x_start = 15
                                y = pdf.get_y()
                                
                                for i, sig in enumerate(sigs_validas):
                                    col = i % cols
                                    if col == 0 and i > 0:
                                        y += cell_h
                                    
                                    if y + cell_h > 275:
                                        pdf.add_page()
                                        y = pdf.get_y()
                                    
                                    x = x_start + (col * cell_w)
                                    pdf.rect(x, y, cell_w, cell_h)
                                    
                                    pdf.line(x + 4, y + 10, x + cell_w - 4, y + 10)
                                    
                                    nome = str(sig.get('Nome', '')).strip()
                                    cargo_full = str(sig.get('Cargo/Atuação', '')).strip()
                                    
                                    cargo = cargo_full
                                    atuacao = ""
                                    if "(" in cargo_full:
                                        parts = cargo_full.split("(")
                                        cargo = parts[0].strip()
                                        atuacao = parts[1].replace(")", "").strip()
                                    
                                    font_size = 7
                                    pdf.set_font("Arial", "B", font_size)
                                    while pdf.get_string_width(nome) > cell_w - 2 and font_size > 4.5:
                                        font_size -= 0.5
                                        pdf.set_font("Arial", "B", font_size)
                                    
                                    pdf.set_xy(x + 1, y + 11) 
                                    pdf.multi_cell(cell_w - 2, 3, clean_pdf_text(nome), 0, 'C')
                                    
                                    pdf.set_font("Arial", "", 6)
                                    curr_y = pdf.get_y()
                                    pdf.set_xy(x + 1, curr_y)
                                    pdf.multi_cell(cell_w - 2, 3, clean_pdf_text(cargo), 0, 'C')
                                    
                                    if atuacao:
                                        curr_y = pdf.get_y()
                                        pdf.set_xy(x + 1, curr_y)
                                        pdf.multi_cell(cell_w - 2, 3, clean_pdf_text(atuacao), 0, 'C')
                                
                                pdf.set_y(y + cell_h)

                            st.session_state.pdf_bytes_ata = get_pdf_bytes(pdf)
                            st.success("✅ PDF gerado com sucesso!")
                        except Exception as e:
                            st.error(f"Erro ao desenhar o PDF: {e}")

                if 'pdf_bytes_ata' in st.session_state:
                    turma_limpa = str(data_ata.get('turma', 'Turma')).replace('/', '-').replace('\\', '-')
                    trimestre_limpo = str(data_ata.get('trimestre', 'Trimestre')).replace('/', '-')
                    st.download_button("📥 BAIXAR ATA", st.session_state.pdf_bytes_ata, f"Ata_{turma_limpa}_{trimestre_limpo}.pdf", "application/pdf", type="primary")

        # ------------------------------------------------------------------------------
        # MÓDULO: EDUCAÇÃO INFANTIL
        # ------------------------------------------------------------------------------
        elif "Infantil" in modalidade_ata:
            if 'data_ata_inf' not in st.session_state:
                st.session_state.data_ata_inf = {
                    'abaixo_basico': [{"Estudante": "", "LV": "", "LM": "", "IS": "", "A": "", "CCM": "", "LT": "", "LIBRAS": ""}],
                    'obs_especiais': [{"Estudante": "", "Desempenho/Observação": ""}],
                    'encaminhamentos': [{"Estudante": "", "Motivo": ""}],
                    'mat_tardia': [{"Estudante": "", "Data Matrícula": "", "Total Frequência": ""}],
                    'obs_outras': "",
                    'assinaturas': [{"Nome": "", "Cargo/Atuação": ""}]
                }
                
            if 'ata_turma_confirmada_inf' not in st.session_state:
                st.session_state.ata_turma_confirmada_inf = None
                st.session_state.ata_ciclo_confirmado_inf = None
            
            data_inf = st.session_state.data_ata_inf
            
            for key in ['abaixo_basico', 'obs_especiais', 'encaminhamentos', 'mat_tardia', 'assinaturas']:
                if isinstance(data_inf.get(key), pd.DataFrame):
                    data_inf[key] = data_inf[key].to_dict('records')
            
            # --- PORTÃO DE ENTRADA (GATE) ---
            if not st.session_state.ata_turma_confirmada_inf:
                st.markdown("""
                <div style='background-color: #fdf4ff; padding: 2rem; border-radius: 10px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); border: 1px solid #f5d0fe;'>
                    <h3 style='color: #86198f; margin-top:0;'>🧸 Selecione a Turma (Educação Infantil)</h3>
                    <p style='color: #a21caf;'>Para iniciar o preenchimento da ata do Infantil, selecione a Etapa e a Turma.</p>
                </div>
                <br>
                """, unsafe_allow_html=True)
                
                c_c, c_t = st.columns(2)
                ciclo_sel = c_c.selectbox("1. Selecione a Fase/Etapa:", ["Maternal II", "1ª Etapa", "2ª Etapa", "Educação Infantil"])
                
                if ciclo_sel == "Educação Infantil":
                    turmas_bd = df_matriz[df_matriz['Ciclo'].str.contains("Infantil|Etapa|Maternal", na=False)]['Turma'].unique().tolist()
                else:
                    turmas_bd = df_matriz[df_matriz['Ciclo'] == ciclo_sel]['Turma'].unique().tolist()
                
                turmas_disp = turmas_bd + ["Outra Turma..."]
                
                turma_sel = c_t.selectbox("2. Selecione a Turma:", turmas_disp)
                if turma_sel == "Outra Turma...":
                    turma_sel = st.text_input("Digite o nome da turma:")
                
                st.write("")
                if st.button("✅ Confirmar e Acessar Formulário do Infantil", type="primary", use_container_width=True):
                    if turma_sel:
                        st.session_state.ata_ciclo_confirmado_inf = ciclo_sel
                        st.session_state.ata_turma_confirmada_inf = turma_sel
                        data_inf['ciclo'] = ciclo_sel
                        data_inf['turma'] = turma_sel
                        st.rerun()
                    else:
                        st.warning("⚠️ Por favor, informe o nome da turma.")
                        
            # --- FORMULÁRIO DA EDUCAÇÃO INFANTIL ---
            else:
                c_info, c_btn = st.columns([4, 1])
                c_info.success(f"🧸 **Ata do Infantil em edição:** {st.session_state.ata_ciclo_confirmado_inf} - {st.session_state.ata_turma_confirmada_inf}")
                if c_btn.button("⬅️ Trocar Turma", use_container_width=True, key="btn_trocar_inf"):
                    st.session_state.ata_turma_confirmada_inf = None
                    st.rerun()
                
                criterios_etapa = get_criterios_infantil(st.session_state.ata_ciclo_confirmado_inf)
                    
                tabs = st.tabs(["1. Identificação", "2. Síntese Avaliativa", "3. Plano de Ação", "4. Observações", "5. Finalização"])
                
                with tabs[0]:
                    st.subheader("Dados da Unidade Escolar")
                    c1, c2, c3 = st.columns([2, 1, 1])
                    data_inf['escola'] = c1.text_input("Unidade Escolar", value=data_inf.get('escola', "CEIEF Rafael Affonso Leite"), key="inf_esc")
                    
                    tri_opts = ["1º Trimestre", "2º Trimestre", "3º Trimestre"]
                    tri_idx = tri_opts.index(data_inf.get('trimestre', "1º Trimestre")) if data_inf.get('trimestre') in tri_opts else 0
                    data_inf['trimestre'] = c2.selectbox("Trimestre", tri_opts, index=tri_idx, key="inf_tri")
                    
                    data_inf['ano_letivo'] = c3.text_input("Ano Letivo", value=data_inf.get('ano_letivo', str(date.today().year)), key="inf_ano")
                    
                    st.markdown("---")
                    c4, c5 = st.columns(2)
                    c4.text_input("Turma", value=st.session_state.ata_turma_confirmada_inf, disabled=True, key="inf_turma")
                    c5.text_input("Etapa/Fase", value=st.session_state.ata_ciclo_confirmado_inf, disabled=True, key="inf_ciclo")

                with tabs[1]:
                    st.subheader("Síntese Avaliativa da Classe - Campos de Experiência e Disciplinas")
                    st.info("Abaixo de cada campo, digite a síntese correspondente à turma. Os conteúdos avaliados (configurados para esta etapa) já estão listados como referência e constarão no PDF gerado.")
                    
                    st.markdown("**Linguagem Verbal:** descrever o desenvolvimento da classe referente a:")
                    if criterios_etapa["LV"]: st.caption(f"_{criterios_etapa['LV']}_")
                    data_inf['sin_lv'] = st.text_area("Síntese - Linguagem Verbal", value=data_inf.get('sin_lv', ''), height=100, label_visibility="collapsed", key="txt_lv")
                    
                    st.markdown("**Linguagem Matemática:** descrever o desenvolvimento da classe referente a:")
                    if criterios_etapa["LM"]: st.caption(f"_{criterios_etapa['LM']}_")
                    data_inf['sin_lm'] = st.text_area("Síntese - Linguagem Matemática", value=data_inf.get('sin_lm', ''), height=100, label_visibility="collapsed", key="txt_lm")
                    
                    st.markdown("**Indivíduo e Sociedade:** descrever o desenvolvimento da classe em relação a:")
                    if criterios_etapa["IS"]: st.caption(f"_{criterios_etapa['IS']}_")
                    data_inf['sin_is'] = st.text_area("Síntese - Indivíduo e Sociedade", value=data_inf.get('sin_is', ''), height=100, label_visibility="collapsed", key="txt_is")
                    
                    st.markdown("**Arte:** descrever o desenvolvimento da classe referente a:")
                    if criterios_etapa["Arte"]: st.caption(f"_{criterios_etapa['Arte']}_")
                    data_inf['sin_arte'] = st.text_area("Síntese - Arte", value=data_inf.get('sin_arte', ''), height=100, label_visibility="collapsed", key="txt_arte")
                    
                    st.markdown("**Cultura Corporal e Movimento:** descrever o desenvolvimento da classe em relação a:")
                    if criterios_etapa["CCM"]: st.caption(f"_{criterios_etapa['CCM']}_")
                    data_inf['sin_ccm'] = st.text_area("Síntese - Cultura Corporal", value=data_inf.get('sin_ccm', ''), height=100, label_visibility="collapsed", key="txt_ccm")

                    st.markdown("**Linguagens e Tecnologias:** descrever o desenvolvimento da classe em relação a:")
                    if criterios_etapa["LT"]: st.caption(f"_{criterios_etapa['LT']}_")
                    data_inf['sin_lt'] = st.text_area("Síntese - Linguagens e Tecnologias", value=data_inf.get('sin_lt', ''), height=100, label_visibility="collapsed", key="txt_lt_inf")

                    st.markdown("**Libras:** descrever o desenvolvimento da classe em relação a:")
                    if criterios_etapa["LIBRAS"]: st.caption(f"_{criterios_etapa['LIBRAS']}_")
                    data_inf['sin_libras'] = st.text_area("Síntese - Libras", value=data_inf.get('sin_libras', ''), height=100, label_visibility="collapsed", key="txt_libras_inf")

                with tabs[2]:
                    st.subheader("Plano de Ação")
                    st.caption("Estudantes com desenvolvimento abaixo do esperado em relação aos conteúdos do currículo.")
                    
                    for i, row in enumerate(data_inf['abaixo_basico']):
                        with st.container():
                            st.markdown(f"**Estudante {i+1}**")
                            c_est, c_del = st.columns([11, 1])
                            row['Estudante'] = c_est.text_input(f"Nome", value=row.get('Estudante', ''), key=f"inf_ab_est_{i}", label_visibility="collapsed")
                            if c_del.button("🗑️", key=f"inf_del_ab_{i}"):
                                data_inf['abaixo_basico'].pop(i); st.rerun()
                            
                            cc = st.columns(7)
                            row['LV'] = cc[0].text_input("LV", value=row.get('LV', ''), key=f"inf_ab_lv_{i}")
                            row['LM'] = cc[1].text_input("LM", value=row.get('LM', ''), key=f"inf_ab_lm_{i}")
                            row['IS'] = cc[2].text_input("IS", value=row.get('IS', ''), key=f"inf_ab_is_{i}")
                            row['A'] = cc[3].text_input("A", value=row.get('A', ''), key=f"inf_ab_a_{i}")
                            row['CCM'] = cc[4].text_input("CCM", value=row.get('CCM', ''), key=f"inf_ab_ccm_{i}")
                            row['LT'] = cc[5].text_input("LT", value=row.get('LT', ''), key=f"inf_ab_lt_{i}")
                            row['LIBRAS'] = cc[6].text_input("LIB", value=row.get('LIBRAS', ''), key=f"inf_ab_lib_{i}")
                            st.markdown("<hr style='margin: 10px 0;'>", unsafe_allow_html=True)
                    
                    if st.button("➕ Adicionar Estudante", key="inf_add_ab"):
                        data_inf['abaixo_basico'].append({"Estudante": "", "LV": "", "LM": "", "IS": "", "A": "", "CCM": "", "LT": "", "LIBRAS": ""})
                        st.rerun()
                    
                    st.markdown("<br>", unsafe_allow_html=True)
                    st.markdown("**Propostas para Intervenção/Recuperação de aprendizagem:**")
                    st.markdown(propostas_ata_inf)

                with tabs[3]:
                    st.subheader("3. Observações Gerais")
                    
                    st.markdown("**a) Desempenho de alunos especiais (laudados)**")
                    for i, row in enumerate(data_inf['obs_especiais']):
                        c1, c2, c3 = st.columns([3, 6, 1])
                        row['Estudante'] = c1.text_input("Estudante Especial", value=row.get('Estudante', ''), key=f"inf_obs_est_{i}")
                        row['Desempenho/Observação'] = c2.text_area("Desempenho/Observações", value=row.get('Desempenho/Observação', ''), key=f"inf_obs_des_{i}", height=68)
                        if c3.button("🗑️", key=f"inf_del_obs_{i}"):
                            data_inf['obs_especiais'].pop(i); st.rerun()
                    if st.button("➕ Adicionar Aluno Especial", key="inf_add_obs"):
                        data_inf['obs_especiais'].append({"Estudante": "", "Desempenho/Observação": ""})
                        st.rerun()
                    
                    st.divider()
                    st.markdown("**b) Alunos encaminhados (Conselho Tutelar ou Serviço Social)**")
                    for i, row in enumerate(data_inf['encaminhamentos']):
                        c1, c2, c3 = st.columns([3, 6, 1])
                        row['Estudante'] = c1.text_input("Estudante Encaminhado", value=row.get('Estudante', ''), key=f"inf_enc_est_{i}")
                        row['Motivo'] = c2.text_area("Motivo do Encaminhamento", value=row.get('Motivo', ''), key=f"inf_enc_mot_{i}", height=68)
                        if c3.button("🗑️", key=f"inf_del_enc_{i}"):
                            data_inf['encaminhamentos'].pop(i); st.rerun()
                    if st.button("➕ Adicionar Encaminhamento", key="inf_add_enc"):
                        data_inf['encaminhamentos'].append({"Estudante": "", "Motivo": ""})
                        st.rerun()

                    st.divider()
                    st.markdown("**c) Estudantes Matriculados Tardiamente**")
                    for i, row in enumerate(data_inf['mat_tardia']):
                        c1, c2, c3, c4 = st.columns([4, 3, 3, 1])
                        row['Estudante'] = c1.text_input("Estudante", value=row.get('Estudante', ''), key=f"inf_mat_est_{i}")
                        row['Data Matrícula'] = c2.text_input("Data da Matrícula", value=row.get('Data Matrícula', ''), key=f"inf_mat_data_{i}")
                        row['Total Frequência'] = c3.text_input("Frequência (Dias)", value=row.get('Total Frequência', ''), key=f"inf_mat_freq_{i}")
                        if c4.button("🗑️", key=f"inf_del_mat_{i}"):
                            data_inf['mat_tardia'].pop(i); st.rerun()
                    if st.button("➕ Adicionar Matrícula Tardia", key="inf_add_mat"):
                        data_inf['mat_tardia'].append({"Estudante": "", "Data Matrícula": "", "Total Frequência": ""})
                        st.rerun()
                        
                    st.divider()
                    st.markdown("**d) Outras Observações**")
                    data_inf['obs_outras'] = st.text_area("Campo livre para quaisquer outras observações da turma:", value=data_inf.get('obs_outras', ''), height=120, key="inf_obs_outras")

                with tabs[4]:
                    st.subheader("Finalização e Assinaturas")
                    
                    if st.button("🤖 Preencher Assinaturas Automaticamente", type="primary", key="inf_btn_auto"):
                        turma_atual = st.session_state.ata_turma_confirmada_inf
                        df_turma = df_matriz[(df_matriz['Turma'] == turma_atual)]
                        
                        if not df_turma.empty:
                            lista_final = []
                            professores_adicionados = set()
                            
                            for _, row in df_turma.iterrows():
                                materia = row['Disciplina']
                                nome_prof = row['Professor']
                                if nome_prof and nome_prof not in professores_adicionados:
                                    lista_final.append({"Nome": nome_prof, "Cargo/Atuação": f"{materia} (Atuante na Turma)"})
                                    professores_adicionados.add(nome_prof)
                                    
                            for _, row in df_gestao.iterrows():
                                if row['Nome']:
                                    lista_final.append({"Nome": row['Nome'], "Cargo/Atuação": row['Cargo']})
                            
                            data_inf['assinaturas'] = lista_final
                            st.success("✅ Grade preenchida com sucesso para a turma selecionada!")
                            st.rerun()
                        else:
                            st.error("Turma não encontrada na Matriz de Automação (Aba Configurações).")

                    st.divider()
                    st.markdown("**Participantes da Reunião**")
                    
                    if 'assinaturas' not in data_inf:
                        data_inf['assinaturas'] = [{"Nome": "", "Cargo/Atuação": ""}]
                        
                    for i, row in enumerate(data_inf['assinaturas']):
                        c1, c2, c3 = st.columns([5, 4, 2])
                        row['Nome'] = c1.text_input("Nome", value=row.get('Nome', ''), key=f"inf_sig_n_{i}", label_visibility="collapsed")
                        row['Cargo/Atuação'] = c2.text_input("Cargo", value=row.get('Cargo/Atuação', ''), key=f"inf_sig_c_{i}", label_visibility="collapsed")
                        
                        b1, b2, b3 = c3.columns(3)
                        if b1.button("⬆️", key=f"inf_up_{i}", disabled=(i == 0)):
                            data_inf['assinaturas'][i], data_inf['assinaturas'][i-1] = data_inf['assinaturas'][i-1], data_inf['assinaturas'][i]
                            st.rerun()
                        if b2.button("⬇️", key=f"inf_dw_{i}", disabled=(i == len(data_inf['assinaturas']) - 1)):
                            data_inf['assinaturas'][i], data_inf['assinaturas'][i+1] = data_inf['assinaturas'][i+1], data_inf['assinaturas'][i]
                            st.rerun()
                        if b3.button("🗑️", key=f"inf_del_{i}"):
                            data_inf['assinaturas'].pop(i)
                            st.rerun()
                            
                    if st.button("➕ Adicionar Participante Manualmente", key="inf_add_sig"):
                        data_inf['assinaturas'].append({"Nome": "", "Cargo/Atuação": ""})
                        st.rerun()
                        
                    st.divider()
                    
                    if st.button("💾 Salvar Ata do Infantil", use_container_width=True, type="secondary", key="inf_save"):
                        try:
                            dados_para_salvar = {}
                            for key, value in data_inf.items():
                                if isinstance(value, pd.DataFrame):
                                    dados_para_salvar[key] = value.to_dict(orient='records')
                                else:
                                    dados_para_salvar[key] = value
                            
                            novo_json = json.dumps(dados_para_salvar, ensure_ascii=False)
                            id_ata = f"{data_inf.get('turma', 'SemTurma')} - {data_inf.get('trimestre', 'SemTri')} (Infantil)"
                            df_atas = safe_read("Atas_Conselho", ["id_ata", "modalidade", "turma", "dados_json"])
                            
                            if not df_atas.empty and "id_ata" in df_atas.columns and id_ata in df_atas["id_ata"].values:
                                df_atas.loc[df_atas["id_ata"] == id_ata, "dados_json"] = novo_json
                            else:
                                novo_registro = {"id_ata": id_ata, "modalidade": "Educação Infantil", "turma": data_inf.get('turma', ''), "dados_json": novo_json}
                                df_atas = pd.concat([df_atas, pd.DataFrame([novo_registro])], ignore_index=True)
                            
                            safe_update("Atas_Conselho", df_atas)
                            st.success(f"✅ Ata do Infantil salva com sucesso!")
                        except Exception as e:
                            st.error(f"Erro ao salvar: {e}")

                    if st.button("👁️ GERAR ATA DO INFANTIL (PDF)", type="primary", use_container_width=True, key="inf_pdf"):
                        try:
                            pdf = OfficialPDF('P', 'mm', 'A4')
                            pdf.doc_type = "Ata" 
                            pdf.set_margins(15, 35, 15)
                            pdf.set_auto_page_break(auto=True, margin=20)
                            pdf.add_page()
                            
                            def calc_lines(txt, w):
                                if not txt: return 1
                                lines = 0
                                for par in txt.split('\n'):
                                    words = par.split(' ')
                                    curr_w = 0
                                    for word in words:
                                        word_w = pdf.get_string_width(word + ' ')
                                        if curr_w + word_w > w:
                                            lines += 1; curr_w = word_w
                                        else:
                                            curr_w += word_w
                                    lines += 1
                                return lines
                            
                            # --- CABEÇALHO ---
                            pdf.set_font("Arial", "B", 10)
                            escola_nome = data_inf.get('escola', 'CEIEF Rafael Affonso Leite').upper()
                            pdf.set_x(15)
                            pdf.cell(180, 5, clean_pdf_text(escola_nome), 0, 1, 'C')
                            
                            trimestre = data_inf.get('trimestre', '').upper()
                            ano = data_inf.get('ano_letivo', '')
                            pdf.set_x(15)
                            pdf.cell(180, 5, clean_pdf_text(f"REGISTRO E CONTROLE DO ACOMPANHAMENTO ESCOLAR"), 0, 1, 'C')
                            pdf.set_x(15)
                            pdf.cell(180, 5, clean_pdf_text(f"EDUCAÇÃO INFANTIL - CONSELHO DE CLASSE/TERMO - {trimestre} DE {ano}"), 0, 1, 'C')
                            
                            turma = data_inf.get('turma', '')
                            ciclo = data_inf.get('ciclo', '')
                            pdf.set_x(15)
                            pdf.cell(180, 5, clean_pdf_text(f"Turma: {turma} | Etapa: {ciclo}"), 0, 1, 'C')
                            pdf.ln(5)
                            
                            # --- SÍNTESE AVALIATIVA ---
                            pdf.set_font("Arial", "B", 10)
                            pdf.set_fill_color(220, 220, 220)
                            pdf.set_x(15)
                            pdf.cell(180, 6, "SÍNTESE AVALIATIVA", "LTR", 1, 'C', True)
                            
                            pdf.set_x(15)
                            pdf.cell(180, 2, "", "LR", 1) 
                            
                            pdf.set_font("Arial", "", 10)
                            pdf.set_x(15)
                            pdf.multi_cell(180, 5, clean_pdf_text(texto_base_ata_inf), "LR", 'J')
                            
                            pdf.set_x(15)
                            pdf.cell(180, 4, "", "LR", 1)
                            
                            pdf.set_font("Arial", "B", 10)
                            pdf.set_x(15)
                            pdf.cell(180, 5, clean_pdf_text("1- Síntese avaliativa da classe:"), "LR", 1, 'L')
                            
                            texto_sint = "descrever o desenvolvimento da classe em cada componente curricular, considerando os aspectos avaliados no trimestre:"
                            pdf.set_font("Arial", "", 10)
                            pdf.set_x(15)
                            pdf.multi_cell(180, 5, clean_pdf_text(texto_sint), "LR", 'J')
                            
                            pdf.set_x(15)
                            pdf.cell(180, 4, "", "LR", 1)
                            
                            criterios_etapa = get_criterios_infantil(st.session_state.ata_ciclo_confirmado_inf)
                            
                            disciplinas_inf = [
                                ("Linguagem Verbal", criterios_etapa["LV"], data_inf.get('sin_lv', '')),
                                ("Linguagem Matemática", criterios_etapa["LM"], data_inf.get('sin_lm', '')),
                                ("Indivíduo e Sociedade", criterios_etapa["IS"], data_inf.get('sin_is', '')),
                                ("Arte", criterios_etapa["Arte"], data_inf.get('sin_arte', '')),
                                ("Cultura Corporal e Movimento", criterios_etapa["CCM"], data_inf.get('sin_ccm', '')),
                                ("Linguagens e Tecnologias", criterios_etapa["LT"], data_inf.get('sin_lt', '')),
                                ("Libras", criterios_etapa["LIBRAS"], data_inf.get('sin_libras', ''))
                            ]
                            
                            for i, (nome, crit, texto) in enumerate(disciplinas_inf):
                                pdf.set_font("Arial", "B", 10)
                                pdf.set_x(15)
                                
                                h_box = calc_lines(crit, 168) * 5 if crit else 5
                                h_total = 5 + 2 + h_box + 2
                                if pdf.get_y() + h_total > 275:
                                    pdf.set_x(15)
                                    pdf.cell(180, 1, "", "LRB", 1)
                                    pdf.add_page()
                                    pdf.set_x(15)
                                    pdf.cell(180, 2, "", "LTR", 1)
                                
                                pdf.set_font("Arial", "B", 10)
                                pdf.set_x(15)
                                pdf.cell(180, 5, clean_pdf_text(f"  {chr(149)}  {nome}: descrever o desenvolvimento da classe referente a:"), "LR", 1, 'L')
                                
                                pdf.set_x(15)
                                pdf.cell(180, 2, "", "LR", 1)
                                
                                y_start = pdf.get_y()
                                pdf.set_font("Arial", "", 9)
                                pdf.set_x(20)
                                pdf.multi_cell(170, 5, clean_pdf_text(crit if crit else "Critérios não configurados para esta etapa."), 1, 'J')
                                y_end = pdf.get_y()
                                
                                pdf.line(15, y_start, 15, y_end)
                                pdf.line(195, y_start, 195, y_end)
                                
                                pdf.set_x(15)
                                pdf.cell(180, 2, "", "LR", 1)
                                
                                if texto.strip():
                                    pdf.set_font("Arial", "", 10)
                                    h_texto = calc_lines(texto, 178) * 5
                                    if pdf.get_y() + h_texto > 275:
                                        pdf.set_x(15)
                                        pdf.cell(180, 1, "", "LRB", 1)
                                        pdf.add_page()
                                        pdf.set_x(15)
                                        pdf.cell(180, 2, "", "LTR", 1)
                                        
                                    pdf.set_x(15)
                                    pdf.multi_cell(180, 5, clean_pdf_text(f"  {texto}"), "LR", 'J')
                                
                                if i < len(disciplinas_inf) - 1:
                                    pdf.set_x(15)
                                    pdf.cell(180, 4, "", "LR", 1) 
                            
                            # --- 2. PLANO DE AÇÃO ---
                            pdf.set_x(15)
                            pdf.cell(180, 5, "", "LR", 1) 
                            
                            if pdf.get_y() > 230: pdf.add_page()
                            
                            pdf.set_font("Arial", "B", 10)
                            pdf.set_x(15)
                            pdf.multi_cell(180, 5, clean_pdf_text("2- Plano de Ação para os estudantes com desenvolvimento abaixo do esperado em relação aos conteúdos do currículo:"), "LR", 'L')
                            
                            pdf.set_x(15)
                            pdf.cell(180, 3, "", "LR", 1)
                            
                            pdf.set_font("Arial", "B", 9)
                            col_w = [54, 18, 18, 18, 18, 18, 18, 18]
                            headers = ["Estudante", "LV", "LM", "IS", "A", "CCM", "LT", "LIB"]
                            pdf.set_x(15)
                            for i, h in enumerate(headers):
                                pdf.cell(col_w[i], 6, h, 1, 0, 'C')
                            pdf.ln()
                            
                            pdf.set_font("Arial", "", 10)
                            lista_abaixo = data_inf.get('abaixo_basico', [])
                                
                            def truncate_str(texto, max_w):
                                while pdf.get_string_width(texto) > max_w - 2: texto = texto[:-1]
                                return texto

                            for row in lista_abaixo:
                                estudante = str(row.get('Estudante', '')).strip()
                                if estudante: 
                                    pdf.set_x(15)
                                    estudante_seguro = truncate_str(estudante, col_w[0])
                                    pdf.cell(col_w[0], 6, clean_pdf_text(estudante_seguro), 1, 0, 'L')
                                    pdf.cell(col_w[1], 6, clean_pdf_text(str(row.get('LV', ''))), 1, 0, 'C')
                                    pdf.cell(col_w[2], 6, clean_pdf_text(str(row.get('LM', ''))), 1, 0, 'C')
                                    pdf.cell(col_w[3], 6, clean_pdf_text(str(row.get('IS', ''))), 1, 0, 'C')
                                    pdf.cell(col_w[4], 6, clean_pdf_text(str(row.get('A', ''))), 1, 0, 'C')
                                    pdf.cell(col_w[5], 6, clean_pdf_text(str(row.get('CCM', ''))), 1, 0, 'C')
                                    pdf.cell(col_w[6], 6, clean_pdf_text(str(row.get('LT', ''))), 1, 0, 'C')
                                    pdf.cell(col_w[7], 6, clean_pdf_text(str(row.get('LIBRAS', ''))), 1, 1, 'C')
                            
                            pdf.set_x(15)
                            pdf.cell(180, 3, "", "LR", 1) 
                            
                            pdf.set_font("Arial", "B", 10)
                            pdf.set_x(15)
                            pdf.cell(180, 5, clean_pdf_text("*Propostas para Intervenção/Recuperação de aprendizagem:"), "LR", 1, 'L')
                            pdf.set_font("Arial", "", 10)
                            
                            for prop in propostas_ata_inf.split('\n'):
                                if prop.strip():
                                    pdf.set_x(15)
                                    pdf.multi_cell(180, 5, clean_pdf_text(prop.strip()), "LR", 'J')

                            pdf.set_x(15)
                            pdf.cell(180, 5, "", "LR", 1) 

                            # --- 3. OBSERVAÇÕES GERAIS ---
                            if pdf.get_y() > 230: pdf.add_page()
                            
                            pdf.set_font("Arial", "B", 10)
                            pdf.set_x(15)
                            pdf.cell(180, 6, clean_pdf_text("3- Observações Gerais:"), "LR", 1, 'L')
                            pdf.set_font("Arial", "", 10)
                            
                            prefix_code = 97 
                            
                            lista_esp = data_inf.get('obs_especiais', [])
                            esp_valid = [r for r in lista_esp if str(r.get('Estudante', '')).strip()]
                            if esp_valid:
                                pdf.set_font("Arial", "B", 10)
                                pdf.set_x(15)
                                pdf.cell(180, 5, clean_pdf_text(f"{chr(prefix_code)}) Desempenho de alunos especiais (laudados):"), "LR", 1, 'L')
                                prefix_code += 1
                                for row in esp_valid:
                                    est = str(row.get('Estudante', '')).strip()
                                    obs = str(row.get('Desempenho/Observação', '')).strip()
                                    pdf.set_font("Arial", "B", 10)
                                    pdf.set_x(15)
                                    pdf.cell(180, 5, clean_pdf_text(f"  {chr(149)}  {est}:"), "LR", 1, 'L')
                                    pdf.set_font("Arial", "", 10)
                                    pdf.set_x(15)
                                    pdf.multi_cell(180, 5, clean_pdf_text(f"  {obs}"), "LR", 'J')
                                pdf.set_x(15)
                                pdf.cell(180, 2, "", "LR", 1)
                            
                            lista_enc = data_inf.get('encaminhamentos', [])
                            enc_valid = [r for r in lista_enc if str(r.get('Estudante', '')).strip()]
                            if enc_valid:
                                pdf.set_font("Arial", "B", 10)
                                pdf.set_x(15)
                                pdf.cell(180, 5, clean_pdf_text(f"{chr(prefix_code)}) Alunos encaminhados (Conselho Tutelar/Serv. Social):"), "LR", 1, 'L')
                                prefix_code += 1
                                for row in enc_valid:
                                    est = str(row.get('Estudante', '')).strip()
                                    mot = str(row.get('Motivo', '')).strip()
                                    pdf.set_font("Arial", "B", 10)
                                    pdf.set_x(15)
                                    pdf.cell(180, 5, clean_pdf_text(f"  {chr(149)}  {est}:"), "LR", 1, 'L')
                                    pdf.set_font("Arial", "", 10)
                                    pdf.set_x(15)
                                    pdf.multi_cell(180, 5, clean_pdf_text(f"  {mot}"), "LR", 'J')
                                pdf.set_x(15)
                                pdf.cell(180, 2, "", "LR", 1)

                            lista_tardia = data_inf.get('mat_tardia', [])
                            tardia_valid = [r for r in lista_tardia if str(r.get('Estudante', '')).strip()]
                            if len(tardia_valid) > 0:
                                pdf.set_font("Arial", "B", 10)
                                pdf.set_x(15)
                                pdf.cell(180, 5, clean_pdf_text(f"{chr(prefix_code)}) Estudantes Matriculados Tardiamente:"), "LR", 1, 'L')
                                prefix_code += 1
                                for row in tardia_valid:
                                    est = str(row.get('Estudante', '')).strip()
                                    mat = str(row.get('Data Matrícula', '')).strip()
                                    freq = str(row.get('Total Frequência', '')).strip()
                                    pdf.set_font("Arial", "B", 10)
                                    pdf.set_x(15)
                                    pdf.cell(180, 5, clean_pdf_text(f"  {chr(149)}  {est}:"), "LR", 1, 'L')
                                    texto_tardio = f"Matriculado(a) nesta sala em {mat}. Portanto, obteve um total de frequência de {freq} dias letivos."
                                    pdf.set_font("Arial", "", 10)
                                    pdf.set_x(15)
                                    pdf.multi_cell(180, 5, clean_pdf_text(f"  {texto_tardio}"), "LR", 'J')
                            else:
                                pdf.set_font("Arial", "", 10)
                                pdf.set_x(15)
                                pdf.cell(180, 5, f" {chr(prefix_code)}) Sem matrículas tardias registradas no período.", "LR", 1)
                                prefix_code += 1

                            pdf.set_x(15)
                            pdf.cell(180, 2, "", "LR", 1)
                            
                            obs_outras = data_inf.get('obs_outras', '').strip()
                            if obs_outras:
                                pdf.set_font("Arial", "B", 10)
                                pdf.set_x(15)
                                pdf.cell(180, 5, clean_pdf_text(f"{chr(prefix_code)}) Outras Observações:"), "LR", 1, 'L')
                                pdf.set_font("Arial", "", 10)
                                pdf.set_x(15)
                                pdf.multi_cell(180, 5, clean_pdf_text(f"  {obs_outras}"), "LR", 'J')
                                
                            pdf.set_x(15)
                            pdf.cell(180, 3, "", "LRB", 1) 

                            # --- ASSINATURAS ---
                            pdf.ln(5)
                            if pdf.get_y() > 220: pdf.add_page()
                            
                            pdf.set_font("Arial", "B", 10)
                            pdf.set_fill_color(220, 220, 220)
                            pdf.set_x(15)
                            pdf.cell(180, 6, "ASSINATURA DOS PARTICIPANTES NA REUNIÃO DO CONSELHO DE EDUCAÇÃO INFANTIL", 1, 1, 'C', True)
                            
                            lista_assinaturas = data_inf.get('assinaturas', [])
                            sigs_validas = [s for s in lista_assinaturas if str(s.get('Nome', '')).strip()]
                            
                            if not sigs_validas:
                                pdf.set_x(15)
                                pdf.cell(180, 20, "Nenhuma assinatura cadastrada para esta ata.", 1, 1, 'C')
                            else:
                                cols = 4
                                cell_w = 180 / cols
                                cell_h = 24 
                                
                                x_start = 15
                                y = pdf.get_y()
                                
                                for i, sig in enumerate(sigs_validas):
                                    col = i % cols
                                    if col == 0 and i > 0:
                                        y += cell_h
                                    
                                    if y + cell_h > 275:
                                        pdf.add_page()
                                        y = pdf.get_y()
                                    
                                    x = x_start + (col * cell_w)
                                    pdf.rect(x, y, cell_w, cell_h)
                                    
                                    pdf.line(x + 4, y + 10, x + cell_w - 4, y + 10)
                                    
                                    nome = str(sig.get('Nome', '')).strip()
                                    cargo_full = str(sig.get('Cargo/Atuação', '')).strip()
                                    
                                    cargo = cargo_full
                                    atuacao = ""
                                    if "(" in cargo_full:
                                        parts = cargo_full.split("(")
                                        cargo = parts[0].strip()
                                        atuacao = parts[1].replace(")", "").strip()
                                    
                                    font_size = 7
                                    pdf.set_font("Arial", "B", font_size)
                                    while pdf.get_string_width(nome) > cell_w - 2 and font_size > 4.5:
                                        font_size -= 0.5
                                        pdf.set_font("Arial", "B", font_size)
                                    
                                    pdf.set_xy(x + 1, y + 11) 
                                    pdf.multi_cell(cell_w - 2, 3, clean_pdf_text(nome), 0, 'C')
                                    
                                    pdf.set_font("Arial", "", 6)
                                    curr_y = pdf.get_y()
                                    pdf.set_xy(x + 1, curr_y)
                                    pdf.multi_cell(cell_w - 2, 3, clean_pdf_text(cargo), 0, 'C')
                                    
                                    if atuacao:
                                        curr_y = pdf.get_y()
                                        pdf.set_xy(x + 1, curr_y)
                                        pdf.multi_cell(cell_w - 2, 3, clean_pdf_text(atuacao), 0, 'C')
                                
                                pdf.set_y(y + cell_h)

                            st.session_state.pdf_bytes_ata = get_pdf_bytes(pdf)
                            st.success("✅ PDF do Infantil gerado com sucesso!")
                        except Exception as e:
                            st.error(f"Erro ao desenhar o PDF: {e}")

                if 'pdf_bytes_ata' in st.session_state:
                    turma_limpa = str(data_inf.get('turma', 'Turma')).replace('/', '-').replace('\\', '-')
                    trimestre_limpo = str(data_inf.get('trimestre', 'Trimestre')).replace('/', '-')
                    st.download_button("📥 BAIXAR ATA DO INFANTIL", st.session_state.pdf_bytes_ata, f"Ata_Infantil_{turma_limpa}_{trimestre_limpo}.pdf", "application/pdf", type="primary")

    # ==============================================================================
    # 2. TELA: HISTÓRICO DE ATAS
    # ==============================================================================
    if app_mode_regular == "📂 Histórico de Atas":
        st.markdown('<div class="header-box"><div class="header-title">Histórico de Atas</div></div>', unsafe_allow_html=True)
        df_atas = safe_read("Atas_Conselho", ["id_ata", "modalidade", "turma", "dados_json"])
        
        if df_atas.empty:
            st.info("Nenhuma ata salva ainda.")
        else:
            df_display = df_atas[["id_ata", "modalidade", "turma"]].copy()
            df_display.columns = ["ID / Título da Ata", "Modalidade", "Turma"]
            st.dataframe(df_display, use_container_width=True, hide_index=True)
            
            st.divider()
            st.subheader("🔄 Carregar Ata")
            c_sel, c_btn = st.columns([3, 1])
            ata_selecionada = c_sel.selectbox("Selecione a Ata:", df_atas["id_ata"].tolist(), label_visibility="collapsed")
            
            if c_btn.button("Carregar Dados", type="primary", use_container_width=True):
                dados_row = df_atas[df_atas["id_ata"] == ata_selecionada].iloc[0]
                try:
                    dados_json = json.loads(dados_row["dados_json"])
                    for key in ['abaixo_basico', 'basico', 'mat_tardia', 'obs_especiais', 'encaminhamentos', 'assinaturas']:
                        if key in dados_json and isinstance(dados_json[key], list):
                            dados_json[key] = pd.DataFrame(dados_json[key])
                            
                    if "Fundamental" in dados_row["modalidade"]:
                        st.session_state.data_ata_ef = dados_json
                        st.session_state.ata_turma_confirmada = dados_json.get('turma', '')
                        st.session_state.ata_ciclo_confirmado = dados_json.get('ciclo', '')
                        
                    elif "Infantil" in dados_row["modalidade"]:
                        st.session_state.data_ata_inf = dados_json
                        st.session_state.ata_turma_confirmada_inf = dados_json.get('turma', '')
                        st.session_state.ata_ciclo_confirmado_inf = dados_json.get('ciclo', '')
                        
                    st.success(f"Ata '{ata_selecionada}' carregada! Vá para 'Nova Ata' e certifique-se de escolher a modalidade correta no menu lateral.")
                except Exception as e:
                    st.error(f"Erro ao carregar: {e}")
            
            st.divider()
            if not is_monitor:
                with st.expander("⚠️ Excluir Ata"):
                    c_del_sel, c_del_btn = st.columns([3, 1])
                    ata_excluir = c_del_sel.selectbox("Ata:", df_atas["id_ata"].tolist(), key="del_ata_sel", label_visibility="collapsed")
                    if c_del_btn.button("🗑️ Excluir", type="secondary", use_container_width=True):
                        safe_update("Atas_Conselho", df_atas[df_atas["id_ata"] != ata_excluir])
                        st.success("Excluída!")
                        time.sleep(1); st.rerun()

    # ==============================================================================
    # 3. TELA: CONFIGURAÇÕES (O CÉREBRO DA AUTOMAÇÃO)
    # ==============================================================================
    if app_mode_regular == "⚙️ Configurações":
        st.markdown('<div class="header-box"><div class="header-title">Configurações do Sistema</div><div class="header-subtitle">Textos Base e Matriz de Professores</div></div>', unsafe_allow_html=True)
        
        t_conf = st.tabs(["📝 Textos Fund.", "🧸 Textos Inf.", "👨‍🏫 Matriz de Professores", "👔 Matriz da Gestão"])
        
        with t_conf[0]:
            st.info("💡 As edições salvas aqui serão utilizadas automaticamente nas novas Atas do Ensino Fundamental.")
            novo_texto_base = st.text_area("Texto Base da Síntese Avaliativa (Legislações)", value=texto_base_ata_ef, height=180)
            novas_propostas = st.text_area("Propostas de Recuperação", value=propostas_ata_ef, height=250)
            
            if st.button("💾 Salvar Textos Fundamental", type="primary", use_container_width=True):
                if not df_config.empty and "texto_base_ata" in df_config["chave"].values: df_config.loc[df_config["chave"] == "texto_base_ata", "valor"] = novo_texto_base
                else: df_config = pd.concat([df_config, pd.DataFrame([{"chave": "texto_base_ata", "valor": novo_texto_base}])], ignore_index=True)
                
                if not df_config.empty and "propostas_ata" in df_config["chave"].values: df_config.loc[df_config["chave"] == "propostas_ata", "valor"] = novas_propostas
                else: df_config = pd.concat([df_config, pd.DataFrame([{"chave": "propostas_ata", "valor": novas_propostas}])], ignore_index=True)
                
                safe_update("Config_Ata", df_config)
                st.success("✅ Textos atualizados!")

        with t_conf[1]:
            st.info("💡 Escolha a Etapa/Maternal e defina os conteúdos cobrados para aquela idade.")
            
            etapa_edit = st.selectbox("Selecione a Etapa para editar os conteúdos:", ["1ª Etapa", "2ª Etapa", "Maternal II"])
            criterios_tela = get_criterios_infantil(etapa_edit)
            
            novo_texto_base_inf = st.text_area("Texto Base da Síntese Avaliativa (Infantil)", value=texto_base_ata_inf, height=120)
            novas_propostas_inf = st.text_area("Propostas de Intervenção (Infantil)", value=propostas_ata_inf, height=120)
            
            st.markdown(f"**Conteúdos Avaliados - {etapa_edit}**")
            novo_crit_lv = st.text_area("Linguagem Verbal", value=criterios_tela["LV"])
            novo_crit_lm = st.text_area("Linguagem Matemática", value=criterios_tela["LM"])
            novo_crit_is = st.text_area("Indivíduo e Sociedade", value=criterios_tela["IS"])
            novo_crit_arte = st.text_area("Arte", value=criterios_tela["Arte"])
            novo_crit_ccm = st.text_area("Cultura Corporal e Movimento", value=criterios_tela["CCM"])
            novo_crit_lt = st.text_area("Linguagens e Tecnologias", value=criterios_tela["LT"])
            novo_crit_libras = st.text_area("Libras", value=criterios_tela["LIBRAS"])
            
            if st.button("💾 Salvar Textos e Conteúdos do Infantil", type="primary", use_container_width=True):
                if not df_config.empty and "texto_base_ata_inf" in df_config["chave"].values: df_config.loc[df_config["chave"] == "texto_base_ata_inf", "valor"] = novo_texto_base_inf
                else: df_config = pd.concat([df_config, pd.DataFrame([{"chave": "texto_base_ata_inf", "valor": novo_texto_base_inf}])], ignore_index=True)
                
                if not df_config.empty and "propostas_ata_inf" in df_config["chave"].values: df_config.loc[df_config["chave"] == "propostas_ata_inf", "valor"] = novas_propostas_inf
                else: df_config = pd.concat([df_config, pd.DataFrame([{"chave": "propostas_ata_inf", "valor": novas_propostas_inf}])], ignore_index=True)
                
                chaves_crit = [
                    (f"crit_lv_{etapa_edit}", novo_crit_lv), 
                    (f"crit_lm_{etapa_edit}", novo_crit_lm), 
                    (f"crit_is_{etapa_edit}", novo_crit_is), 
                    (f"crit_arte_{etapa_edit}", novo_crit_arte), 
                    (f"crit_ccm_{etapa_edit}", novo_crit_ccm),
                    (f"crit_lt_{etapa_edit}", novo_crit_lt),
                    (f"crit_libras_{etapa_edit}", novo_crit_libras)
                ]
                
                for k, v in chaves_crit:
                    if not df_config.empty and k in df_config["chave"].values: df_config.loc[df_config["chave"] == k, "valor"] = v
                    else: df_config = pd.concat([df_config, pd.DataFrame([{"chave": k, "valor": v}])], ignore_index=True)

                safe_update("Config_Ata", df_config)
                st.success(f"✅ Textos e Conteúdos salvos com sucesso para a {etapa_edit}!")

        with t_conf[2]:
            st.info("💡 Edite a tabela para alterar a atribuição de aulas.")
            
            config_col_matriz = {
                "Ciclo": st.column_config.SelectboxColumn("Ciclo", options=["Ciclo I (1º ao 3º ano)", "Ciclo II (4º e 5º ano)", "1ª Etapa", "2ª Etapa", "Maternal II", "Educação Infantil"]),
                "Turma": st.column_config.TextColumn("Turma (Ex: 5º Ano 1)"),
                "Disciplina": st.column_config.TextColumn("Disciplina (Ex: Polivalente, Matemática)"),
                "Professor": st.column_config.TextColumn("Nome do Professor")
            }
            
            df_matriz_editada = st.data_editor(df_matriz, column_config=config_col_matriz, num_rows="dynamic", use_container_width=True, hide_index=True)
            
            if st.button("💾 Salvar Matriz de Professores", type="primary", use_container_width=True):
                novo_matriz_json = df_matriz_editada.to_json(orient='records')
                if not df_config.empty and "matriz_professores" in df_config["chave"].values:
                    df_config.loc[df_config["chave"] == "matriz_professores", "valor"] = novo_matriz_json
                else:
                    df_config = pd.concat([df_config, pd.DataFrame([{"chave": "matriz_professores", "valor": novo_matriz_json}])], ignore_index=True)
                safe_update("Config_Ata", df_config)
                st.success("✅ Matriz de professores salva com sucesso!")

        with t_conf[3]:
            st.info("💡 Edite a tabela para atualizar quem assina como Equipe Gestora nas Atas.")
            
            config_col_gestao = {
                "Nome": st.column_config.TextColumn("Nome do Profissional"),
                "Cargo": st.column_config.SelectboxColumn("Cargo", options=["Prof. Coordenador", "Vice-Diretor", "Diretor de Escola"])
            }
            
            df_gestao_editada = st.data_editor(df_gestao, column_config=config_col_gestao, num_rows="dynamic", use_container_width=True, hide_index=True)
            
            if st.button("💾 Salvar Matriz de Gestão", type="primary", use_container_width=True):
                novo_gestao_json = df_gestao_editada.to_json(orient='records')
                if not df_config.empty and "matriz_gestao" in df_config["chave"].values:
                    df_config.loc[df_config["chave"] == "matriz_gestao", "valor"] = novo_gestao_json
                else:
                    df_config = pd.concat([df_config, pd.DataFrame([{"chave": "matriz_gestao", "valor": novo_gestao_json}])], ignore_index=True)
                safe_update("Config_Ata", df_config)
                st.success("✅ Matriz da gestão salva com sucesso!")

    

# ==============================================================================
# MÓDULO 4: AGENDAMENTO SALA DE INFORMÁTICA (NOVO)
# ==============================================================================

    elif app_mode_regular == "💻 Agendamento Informática":
        st.markdown('<div class="header-box"><div class="header-title">💻 Agendamento - Sala de Informática</div></div>', unsafe_allow_html=True)
        st.markdown("Reserve a sala de computadores para a sua turma do Ensino Regular.")
        st.divider()

        # --- GRADE FIXA ANUAL ---
        # Baseado na tabela oficial da escola. 
        # Estes horários serão bloqueados automaticamente todas as semanas.
        grade_fixa = {
            0: [ # Segunda-feira (0)
                {"Horario": "07:00 - 07:50", "Professor": "Prof. Fernando", "Turma": "1º ANO 1: Linguagens"},
                {"Horario": "12:30 - 13:20", "Professor": "Prof. Elaine", "Turma": "Etapa 2-2: Linguagens"},
                {"Horario": "13:20 - 14:10", "Professor": "Prof. Elaine", "Turma": "Etapa 2-3: Linguagens"},
                {"Horario": "14:10 - 15:00", "Professor": "Prof. Fernando", "Turma": "1º ANO 2: Linguagens"},
                {"Horario": "15:00 - 15:50", "Professor": "Prof. Fernando", "Turma": "2º ANO 1: Linguagens"}
            ],
            1: [ # Terça-feira (1)
                {"Horario": "07:00 - 07:50", "Professor": "Prof. Josiane", "Turma": "4º ANO 2: Linguagens"},
                {"Horario": "07:50 - 08:40", "Professor": "Prof. Fernando", "Turma": "Etapa 1-2: Linguagens"},
                {"Horario": "09:30 - 10:20", "Professor": "Prof. Josiane", "Turma": "4º ANO 1: Linguagens"},
                {"Horario": "12:30 - 13:20", "Professor": "Prof. Elaine", "Turma": "Etapa 2-1: Linguagens"},
                {"Horario": "13:20 - 14:10", "Professor": "Prof. Elaine", "Turma": "Mat. 2-2: Linguagens"}
            ],
            2: [ # Quarta-feira (2)
                {"Horario": "07:50 - 08:40", "Professor": "Prof. Karina", "Turma": "Mat. 2-1: Linguagens"},
                {"Horario": "08:40 - 09:30", "Professor": "Prof. Karina", "Turma": "Etapa 1-1: Linguagens"},
                {"Horario": "11:10 - 12:00", "Professor": "Prof. Fernando", "Turma": "Etapa 1-3: Linguagens"},
                {"Horario": "15:50 - 16:40", "Professor": "Prof. Fernando", "Turma": "2º ANO 2: Linguagens"}
            ],
            3: [ # Quinta-feira (3)
                {"Horario": "09:30 - 10:20", "Professor": "Prof. Bruna", "Turma": "5º ANO 2: Linguagens"},
                {"Horario": "10:20 - 11:10", "Professor": "Prof. Josiane", "Turma": "4º ANO 3: Linguagens"},
                {"Horario": "12:30 - 13:20", "Professor": "Prof. Elaine", "Turma": "3º ANO 2: Linguagens"},
                {"Horario": "13:20 - 14:10", "Professor": "Prof. Elaine", "Turma": "3º ANO 1: Linguagens"},
                {"Horario": "14:10 - 15:00", "Professor": "Prof. Elaine", "Turma": "3º ANO 3: Linguagens"},
                {"Horario": "15:00 - 15:50", "Professor": "Prof. Fernando", "Turma": "1º ANO 3: Linguagens"}
            ],
            4: [ # Sexta-feira (4)
                {"Horario": "07:00 - 07:50", "Professor": "Prof. Bruna", "Turma": "5º ANO 1: Linguagens"},
                {"Horario": "07:50 - 08:40", "Professor": "Prof. Bruna", "Turma": "5º ANO 3: Linguagens"},
                {"Horario": "15:00 - 15:50", "Professor": "Prof. Fernando", "Turma": "2º ANO 3: Linguagens"}
            ],
            5: [], 6: []
        }

        try:
            df_agendamentos = conn.read(worksheet="Agendamentos", usecols=[0, 1, 2, 3])
            if df_agendamentos.empty:
                df_agendamentos = pd.DataFrame(columns=["Data", "Horario", "Professor", "Turma"])
        except Exception as e:
            st.warning("⚠️ Aba 'Agendamentos' não encontrada. Crie uma aba na planilha com as colunas: Data, Horario, Professor, Turma.")
            df_agendamentos = pd.DataFrame(columns=["Data", "Horario", "Professor", "Turma"])

        col_form, col_view = st.columns([1, 1.2], gap="large")

        with col_view:
            st.subheader("📅 Grade do Dia")
            data_selecionada = st.date_input("Escolha a data para visualizar/agendar:", format="DD/MM/YYYY")
            data_str = data_selecionada.strftime("%d/%m/%Y")
            dia_semana_idx = data_selecionada.weekday() # 0 = Segunda, 4 = Sexta
            
            # 1. Carregar Agendamentos Fixos do dia da semana escolhido
            lista_fixos = grade_fixa.get(dia_semana_idx, [])
            df_fixos = pd.DataFrame(lista_fixos)
            if not df_fixos.empty:
                df_fixos["Tipo"] = "Fixo (Anual)"
            
            # 2. Carregar Agendamentos Avulsos (Google Sheets) para a data específica
            df_dinamico = df_agendamentos[df_agendamentos["Data"] == data_str].copy()
            if not df_dinamico.empty:
                df_dinamico["Tipo"] = "Reserva Avulsa"
                df_dinamico = df_dinamico[["Horario", "Professor", "Turma", "Tipo"]]
            
            # 3. Juntar tudo para mostrar na tabela
            frames = []
            if not df_fixos.empty: frames.append(df_fixos)
            if not df_dinamico.empty: frames.append(df_dinamico)
            
            if len(frames) > 0:
                df_dia_completo = pd.concat(frames, ignore_index=True)
                # Ordenar visualmente pelos horários
                df_dia_completo = df_dia_completo.sort_values(by="Horario")
                
                # Exibir tabela 
                st.dataframe(df_dia_completo[["Horario", "Professor", "Turma", "Tipo"]], use_container_width=True, hide_index=True)
                horarios_ocupados = df_dia_completo["Horario"].tolist()
            else:
                st.info(f"A sala de informática está totalmente livre no dia {data_str}.")
                horarios_ocupados = []

        with col_form:
            st.subheader("Novo Agendamento Avulso")
            horarios_escola = [
                "07:00 - 07:50", "07:50 - 08:40", "08:40 - 09:30", "09:30 - 10:20", "10:20 - 11:10", "11:10 - 12:00",
                "12:30 - 13:20", "13:20 - 14:10", "14:10 - 15:00", "15:00 - 15:50", "15:50 - 16:40", "16:40 - 17:30"
            ]
            
            # Subtrai os ocupados (fixos + avulsos) da lista de disponíveis
            horarios_disponiveis = [h for h in horarios_escola if h not in horarios_ocupados]

            with st.form("form_agendamento", clear_on_submit=True):
                professor = st.text_input("Nome do Professor(a)", placeholder="Ex: Prof. Silva")
                turma = st.text_input("Turma", placeholder="Ex: 6º Ano A")
                
                if not horarios_disponiveis:
                    st.error("Todos os horários estão lotados para este dia!")
                    horario_escolhido = None
                else:
                    horario_escolhido = st.selectbox("Horários Disponíveis", horarios_disponiveis)
                
                submit_agendamento = st.form_submit_button("💾 Confirmar Reserva", use_container_width=True)

                if submit_agendamento:
                    if not professor or not turma:
                        st.error("Por favor, preencha o Nome e a Turma.")
                    elif not horario_escolhido:
                        st.error("Selecione um horário válido.")
                    else:
                        novo_registro = pd.DataFrame([{"Data": data_str, "Horario": horario_escolhido, "Professor": professor, "Turma": turma}])
                        df_atualizado = pd.concat([df_agendamentos, novo_registro], ignore_index=True)
                        try:
                            conn.update(worksheet="Agendamentos", data=df_atualizado)
                            st.success(f"✅ Sala reservada com sucesso para {turma} às {horario_escolhido}!")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Erro ao salvar: {e}")



