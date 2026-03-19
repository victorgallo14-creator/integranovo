import streamlit as st
import pandas as pd
import json
import io
import zipfile
import os
import base64
import tempfile
from datetime import datetime, date
from streamlit_gsheets import GSheetsConnection
from fpdf import FPDF

# --- CONFIGURAÇÃO INICIAL ---
st.set_page_config(page_title="Exportador em Lote", page_icon="📦")
conn = st.connection("gsheets", type=GSheetsConnection)

# --- FUNÇÕES AUXILIARES E CLASSE PDF (Copiadas do seu código original) ---
def clean_pdf_text(text):
    if text is None or text is False: return ""
    if text is True: return "Sim"
    return str(text).encode('latin-1', 'replace').decode('latin-1')

def get_pdf_bytes(pdf_instance):
    try: return bytes(pdf_instance.output(dest='S').encode('latin-1'))
    except: return bytes(pdf_instance.output(dest='S'))

class OfficialPDF(FPDF):
    def __init__(self, orientation='P', unit='mm', format='A4'):
        super().__init__(orientation, unit, format)
        self.signature_info = None
        self.doc_uuid = None
        self.doc_type = None

    def set_signature_footer(self, signatures_list, doc_uuid):
        self.doc_uuid = doc_uuid
        if signatures_list and len(signatures_list) > 0:
            names = [s.get('name', '').upper() for s in signatures_list]
            names_str = ", ".join(names[:-1]) + " e " + names[-1] if len(names) > 1 else names[0]
            self.signature_info = f"Assinado por {len(names)} pessoas: {names_str}"
        else:
            self.signature_info = "Documento gerado sem assinaturas digitais."
            
    def footer(self):
        if self.doc_type != "Ata":
            self.set_y(-25)
            self.set_font('Arial', '', 8)
            self.set_text_color(80, 80, 80)
            if self.doc_uuid:
                box_h = 9
                margin_bottom = 22
                y_box = self.h - margin_bottom 
                x_box = 10
                w_box = self.w - 20
                self.set_fill_color(245, 245, 245)
                self.rect(x_box, y_box, w_box, box_h, 'F')
                self.set_xy(x_box + 2, y_box + 1.5)
                self.set_font('Arial', 'B', 7)
                if self.signature_info:
                    self.cell(0, 3, clean_pdf_text(self.signature_info), 0, 1, 'L')
                else:
                    self.ln(3)
                self.set_x(x_box + 2)
                self.set_font('Arial', '', 7)
                link_txt = f"Para verificar a validade das assinaturas, acesse https://integra.streamlit.app e informe o código {self.doc_uuid}"
                self.cell(0, 3, clean_pdf_text(link_txt), 0, 1, 'L')
            self.set_y(-10)
            self.set_font('Arial', '', 8)
            addr = "Secretaria Municipal de Educação | Centro de Formação do Professor - Limeira-SP"
            self.cell(0, 5, clean_pdf_text(addr), 0, 0, 'C')
            self.set_font('Arial', 'I', 8)
            self.cell(0, 5, clean_pdf_text(f'Página {self.page_no()}'), 0, 0, 'R')

    def section_title(self, title, width=0):
        self.set_font('Arial', 'B', 12); self.set_fill_color(240, 240, 240)
        self.cell(width, 8, clean_pdf_text(title), 1, 1, 'L', 1)

@st.cache_data(ttl=60)
def carregar_banco():
    try:
        return conn.read(worksheet="Alunos", ttl=0).dropna(how="all")
    except:
        return pd.DataFrame(columns=["nome", "tipo_doc", "dados_json"])

# --- INTERFACE DO EXPORTADOR ---
st.title("📦 Exportador em Lote de Documentos")
st.write("Esta é uma ferramenta isolada. Selecione os documentos e baixe todos de uma vez em formato .zip")

df_banco = carregar_banco()

if df_banco.empty:
    st.warning("Banco de dados vazio ou erro de conexão com o Google Sheets.")
    st.stop()

# --- FILTROS ---
# Por enquanto, configurei completamente o "CASO" para você ver rodando.
tipo_doc = st.selectbox("Qual documento deseja exportar?", ["CASO", "PEI", "PDI"])

df_filtrado = df_banco[df_banco["tipo_doc"] == tipo_doc]
lista_alunos = df_filtrado["nome"].dropna().unique().tolist()

alunos_selecionados = st.multiselect(
    "Selecione os alunos (ou deixe em branco para exportar a escola toda):", 
    lista_alunos
)

if not alunos_selecionados:
    alunos_selecionados = lista_alunos

st.divider()

# --- GERAÇÃO DO ZIP ---
if st.button(f"📦 Gerar Lote de PDFs ({len(alunos_selecionados)} arquivos)", type="primary"):
    with st.spinner("Desenhando PDFs e compactando arquivo... Isso leva alguns segundos. ☕"):
        
        zip_buffer = io.BytesIO()
        documentos_gerados = 0
        
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            
            for nome_aluno in alunos_selecionados:
                row = df_filtrado[df_filtrado["nome"] == nome_aluno].iloc[0]
                data = json.loads(row["dados_json"])
                data_case = data # Mapeamento para o código de desenho
                
                try:
                    if tipo_doc == "CASO":
                        # ==============================================================================
                        # SEU CÓDIGO EXATO DE DESENHO DO ESTUDO DE CASO
                        # ==============================================================================
                        pdf = OfficialPDF('P', 'mm', 'A4')
                        pdf.add_page(); pdf.set_margins(15, 15, 15)
                        pdf.set_signature_footer(data.get('signatures', []), data.get('doc_uuid', ''))
                        
                        if os.path.exists("logo_prefeitura.png"): pdf.image("logo_prefeitura.png", 15, 10, 25)
                        if os.path.exists("logo_escola.png"): pdf.image("logo_escola.png", 170, 6, 25)

                        pdf.set_xy(0, 15); pdf.set_font("Arial", "B", 12)
                        pdf.cell(210, 6, clean_pdf_text("PREFEITURA MUNICIPAL DE LIMEIRA"), 0, 1, 'C')
                        pdf.cell(180, 6, clean_pdf_text("CEIEF RAFAEL AFFONSO LEITE"), 0, 1, 'C')
                        pdf.ln(8)
                        pdf.set_font("Arial", "B", 16); pdf.cell(0, 10, "ESTUDO DE CASO", 0, 1, 'C')
                        pdf.ln(5)
                        
                        pdf.section_title("1.1 DADOS GERAIS DO ESTUDANTE", width=0)
                        pdf.ln(4)
                        
                        pdf.set_fill_color(240, 240, 240)
                        pdf.set_font("Arial", "B", 10); pdf.cell(0, 8, "1.1.1 - IDENTIFICAÇÃO", 1, 1, 'L', 1)
                        
                        pdf.set_font("Arial", "B", 10); pdf.cell(30, 8, "Nome:", 1, 0, 'L', 1)
                        pdf.set_font("Arial", "", 10); pdf.cell(110, 8, clean_pdf_text(data.get('nome', '')), 1, 0)
                        pdf.set_font("Arial", "B", 10); pdf.cell(15, 8, "D.N.:", 1, 0, 'C', 1)
                        pdf.set_font("Arial", "", 10); pdf.cell(0, 8, clean_pdf_text(str(data.get('d_nasc', ''))), 1, 1, 'C')
                        
                        pdf.set_font("Arial", "B", 10); pdf.cell(30, 8, "Escolaridade:", 1, 0, 'L', 1)
                        pdf.set_font("Arial", "", 10); pdf.cell(25, 8, clean_pdf_text(data.get('ano_esc', '')), 1, 0)
                        pdf.set_font("Arial", "B", 10); pdf.cell(20, 8, "Período:", 1, 0, 'C', 1)
                        pdf.set_font("Arial", "", 10); pdf.cell(20, 8, clean_pdf_text(data.get('periodo', '')), 1, 0, 'C')
                        pdf.set_font("Arial", "B", 10); pdf.cell(20, 8, "Unidade:", 1, 0, 'C', 1)
                        pdf.set_font("Arial", "", 10); pdf.cell(0, 8, clean_pdf_text(data.get('unidade', '')), 1, 1)
                        
                        pdf.set_font("Arial", "B", 10); pdf.cell(30, 8, clean_pdf_text("Endereço:"), 1, 0, 'L', 1)
                        pdf.set_font("Arial", "", 10); pdf.cell(0, 8, clean_pdf_text(data.get('endereco', '')), 1, 1)

                        pdf.set_font("Arial", "B", 10); pdf.cell(20, 8, "Bairro:", 1, 0, 'L', 1)
                        pdf.set_font("Arial", "", 10); pdf.cell(70, 8, clean_pdf_text(data.get('bairro', '')), 1, 0)
                        pdf.set_font("Arial", "B", 10); pdf.cell(20, 8, "Cidade:", 1, 0, 'C', 1)
                        pdf.set_font("Arial", "", 10); pdf.cell(0, 8, clean_pdf_text(data.get('cidade', '')), 1, 1)
                        
                        pdf.set_font("Arial", "B", 10); pdf.cell(20, 8, "Telefone:", 1, 0, 'L', 1)
                        pdf.set_font("Arial", "", 10); pdf.cell(0, 8, clean_pdf_text(data.get('telefones', '')), 1, 1)
                        
                        pdf.ln(4)
                        pdf.set_font("Arial", "B", 10); pdf.cell(0, 8, "1.1.2 - DADOS FAMILIARES", 1, 1, 'L', 1)
                        
                        pdf.set_font("Arial", "B", 10); pdf.cell(20, 8, "Pai:", 1, 0, 'L', 1)
                        pdf.set_font("Arial", "", 10); pdf.cell(80, 8, clean_pdf_text(data.get('pai_nome', '')), 1, 0)
                        pdf.set_font("Arial", "B", 10); pdf.cell(25, 8, clean_pdf_text("Profissão:"), 1, 0, 'C', 1)
                        pdf.set_font("Arial", "", 10); pdf.cell(0, 8, clean_pdf_text(data.get('pai_prof', '')), 1, 1)
                        
                        pdf.set_font("Arial", "B", 10); pdf.cell(20, 8, clean_pdf_text("Mãe:"), 1, 0, 'L', 1)
                        pdf.set_font("Arial", "", 10); pdf.cell(80, 8, clean_pdf_text(data.get('mae_nome', '')), 1, 0)
                        pdf.set_font("Arial", "B", 10); pdf.cell(25, 8, clean_pdf_text("Profissão:"), 1, 0, 'C', 1)
                        pdf.set_font("Arial", "", 10); pdf.cell(0, 8, clean_pdf_text(data.get('mae_prof', '')), 1, 1)
                        
                        pdf.ln(2)
                        pdf.set_font("Arial", "B", 10); pdf.cell(0, 8, clean_pdf_text("Irmãos (Nome | Idade | Escolaridade)"), 1, 1, 'L', 1)
                        pdf.set_font("Arial", "", 9)
                        for irmao in data.get('irmaos', []):
                            if irmao.get('nome'):
                                txt = f"{irmao['nome']}  |  {irmao['idade']}  |  {irmao['esc']}"
                                pdf.cell(0, 6, clean_pdf_text(txt), 1, 1)
                        
                        pdf.ln(2)
                        pdf.set_font("Arial", "B", 10); pdf.cell(40, 8, "Com quem mora:", 1, 0, 'L', 1)
                        pdf.set_font("Arial", "", 10); pdf.cell(0, 8, clean_pdf_text(data.get('quem_mora', '')), 1, 1)
                        
                        pdf.set_font("Arial", "B", 10); pdf.cell(40, 8, clean_pdf_text("Convênio Médico:"), 1, 0, 'L', 1)
                        pdf.set_font("Arial", "", 10); pdf.cell(50, 8, clean_pdf_text(data.get('convenio')), 1, 0)
                        pdf.set_font("Arial", "B", 10); pdf.cell(20, 8, clean_pdf_text("Qual:"), 1, 0, 'C', 1)
                        pdf.set_font("Arial", "", 10); pdf.cell(0, 8, clean_pdf_text(data.get('convenio_qual')), 1, 1)
                        
                        pdf.set_font("Arial", "B", 10); pdf.cell(40, 8, clean_pdf_text("Benefício Social:"), 1, 0, 'L', 1)
                        pdf.set_font("Arial", "", 10); pdf.cell(50, 8, clean_pdf_text(data.get('social')), 1, 0)
                        pdf.set_font("Arial", "B", 10); pdf.cell(20, 8, clean_pdf_text("Qual:"), 1, 0, 'C', 1)
                        pdf.set_font("Arial", "", 10); pdf.cell(0, 8, clean_pdf_text(data.get('social_qual')), 1, 1)

                        pdf.ln(4)
                        pdf.set_font("Arial", "B", 10); pdf.cell(0, 8, clean_pdf_text("1.1.3 - HISTÓRIA ESCOLAR"), 1, 1, 'L', 1)
                        
                        pdf.set_font("Arial", "B", 10); pdf.cell(50, 8, "Idade entrou na escola:", 1, 0, 'L', 1)
                        pdf.set_font("Arial", "", 10); pdf.cell(0, 8, clean_pdf_text(data.get('hist_idade_entrou')), 1, 1)
                        
                        pdf.set_font("Arial", "B", 10); pdf.cell(50, 8, "Outras escolas:", 1, 0, 'L', 1)
                        pdf.set_font("Arial", "", 10); pdf.cell(0, 8, clean_pdf_text(data.get('hist_outra_escola')), 1, 1)
                        
                        pdf.set_font("Arial", "B", 10); pdf.cell(50, 8, clean_pdf_text("Motivo transferência:"), 1, 0, 'L', 1)
                        pdf.set_font("Arial", "", 10); pdf.cell(0, 8, clean_pdf_text(data.get('hist_motivo_transf')), 1, 1)
                        
                        if data.get('hist_obs'):
                            pdf.ln(2)
                            pdf.set_font("Arial", "B", 10); pdf.cell(0, 6, "Observações Escolares:", 0, 1)
                            pdf.set_font("Arial", "", 9); pdf.multi_cell(0, 5, clean_pdf_text(data.get('hist_obs')), 1)

                        # Adiciona a quebra de página de segurança final
                        pdf.ln(15)
                        pdf.set_font("Arial", "I", 9)
                        pdf.cell(0, 8, "Documento gerado em lote via integração.", 0, 1, 'C')

                    elif tipo_doc == "PEI":
                        # ==============================================================================
                        # AQUI VOCÊ PODE COLAR DEPOIS O SEU CÓDIGO DO PEI SE QUISER BAIXAR O LOTE DE PEI
                        # ==============================================================================
                        pdf = OfficialPDF('L', 'mm', 'A4')
                        pdf.add_page(); pdf.set_margins(10, 10, 10)
                        pdf.set_font("Arial", "B", 16)
                        pdf.cell(0, 10, clean_pdf_text(f"DOCUMENTO: {tipo_doc} - Geração Básica"), 0, 1, 'C')
                        pdf.set_font("Arial", "", 12)
                        pdf.cell(0, 10, clean_pdf_text(f"Aluno: {nome_aluno}"), 0, 1, 'L')
                        pdf.cell(0, 10, clean_pdf_text("Cole aqui a lógica do PEI no código fonte para detalhamento."), 0, 1, 'L')

                    else:
                        pdf = OfficialPDF('P', 'mm', 'A4')
                        pdf.add_page(); pdf.set_margins(15, 15, 15)
                        pdf.set_font("Arial", "B", 16)
                        pdf.cell(0, 10, clean_pdf_text(f"DOCUMENTO: {tipo_doc}"), 0, 1, 'C')
                        pdf.set_font("Arial", "", 12)
                        pdf.cell(0, 10, clean_pdf_text(f"Aluno: {nome_aluno}"), 0, 1, 'L')

                    # ---------------------------------------------------------
                    
                    # Salva o PDF no ZIP
                    pdf_bytes = get_pdf_bytes(pdf)
                    nome_arquivo = f"{tipo_doc}_{nome_aluno.replace(' ', '_')}.pdf"
                    zip_file.writestr(nome_arquivo, pdf_bytes)
                    
                    documentos_gerados += 1
                except Exception as e:
                    st.error(f"Erro ao gerar PDF de {nome_aluno}: {e}")
                    
        if documentos_gerados > 0:
            st.success(f"Tudo pronto! {documentos_gerados} arquivos compactados com sucesso.")
            st.download_button(
                label="📥 CLIQUE AQUI PARA BAIXAR O LOTE (.ZIP)",
                data=zip_buffer.getvalue(),
                file_name=f"Lote_{tipo_doc}_{datetime.now().strftime('%d%m%Y')}.zip",
                mime="application/zip",
                type="primary",
                use_container_width=True
            )
