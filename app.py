from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import time
import hashlib
import re
try:
    import httpx
    HTTPX_OK = True
except ImportError:
    HTTPX_OK = False

app = Flask(__name__)
CORS(app)

BASE = "http://siscnrm.mec.gov.br/certificados"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "http://siscnrm.mec.gov.br/certificados",
}
FMT = "%d/%m/%Y"

# Cache simples em memória para programas por UF
_cache_programas_uf = {}

def parse_options(html):
    soup = BeautifulSoup(html, "html.parser")
    return [
        {"id": o.get("value","").strip(), "nome": o.get_text(strip=True)}
        for o in soup.find_all("option")
        if o.get("value","").strip() not in ("","0")
    ]

def parse_table(html):
    soup = BeautifulSoup(html, "html.parser")
    registros = []
    for table in soup.find_all("table"):
        linhas = table.find_all("tr")
        if not linhas: continue
        headers = [th.get_text(strip=True) for th in linhas[0].find_all(["th","td"])]
        if not headers: continue
        for tr in linhas[1:]:
            tds = [td.get_text(strip=True) for td in tr.find_all("td")]
            if tds and any(tds):
                registros.append(
                    dict(zip(headers, tds)) if len(tds)==len(headers)
                    else {f"Col{i+1}":v for i,v in enumerate(tds)}
                )
    return registros

def filtrar_por_data(registros, d_ini, d_fim):
    resultado = []
    for reg in registros:
        data_val = None
        for k, v in reg.items():
            if any(p in k.lower() for p in ["emiss","certificado"]):
                for fmt in ["%d/%m/%Y","%Y-%m-%d"]:
                    try: data_val = datetime.strptime(str(v).strip(), fmt); break
                    except: pass
                if data_val: break
        if data_val is None or (d_ini <= data_val <= d_fim):
            resultado.append(reg)
    return resultado

def buscar_programas_instituicao(co_entidade):
    """Busca programas de uma instituição — usado em paralelo."""
    try:
        r = requests.get(f"{BASE}/options/co_entidade/{co_entidade}", headers=HEADERS, timeout=20)
        return parse_options(r.text)
    except:
        return []

# ── Cache de sessão DoctorID ──────────────────────────────────────────────────
_did_sessions = {}   # hash -> (client, timestamp)
_did_lock = threading.Lock()
DURL = "https://www.doctorid.com.br"
SESSION_TTL = 1800   # 30 minutos

def get_did_session(email, senha):
    key = hashlib.md5(f"{email}:{senha}".encode()).hexdigest()
    with _did_lock:
        if key in _did_sessions:
            c, ts = _did_sessions[key]
            if "JSESSIONID" in c.cookies and time.time() - ts < SESSION_TTL:
                _did_sessions[key] = (c, time.time())
                return c
            del _did_sessions[key]
    # Nova sessão
    try:
        c = httpx.Client(follow_redirects=True, timeout=20)
        c.get(DURL + "/website")
        xsrf = c.cookies.get("XSRF-TOKEN", "")
        c.post(DURL + "/manualLogin",
            data={"S_IDENTIFIER": email, "S_PASSWORD": senha},
            headers={"X-Requested-With": "XMLHttpRequest", "X-Xsrf-Token": xsrf})
        if "JSESSIONID" not in c.cookies:
            return None
        xsrf = c.cookies.get("XSRF-TOKEN", xsrf)
        c.headers.update({"X-Requested-With": "XMLHttpRequest",
                          "X-Xsrf-Token": xsrf, "Referer": DURL + "/"})
        with _did_lock:
            _did_sessions[key] = (c, time.time())
        return c
    except:
        return None

def lookup_crm(client, crm):
    import re as _re
    crm_digitos = _re.sub(r"[^0-9]", "", str(crm)).ljust(12, "_")
    vazio = {"crm": crm, "telefone": "", "email": "", "nome": "",
             "cpf": "", "especialidades": [], "macrorregioes": []}
    try:
        r = client.post(DURL + "/personGroupCompany/find",
            data={"conselhoRegional": crm_digitos, "cpf": "", "nome": ""},
            timeout=15)
        if r.status_code != 200:
            return vazio
        dados = r.json()
        pessoas = dados.get("data", {}).get("pessoas", [])
        if not pessoas:
            return vazio
        p = pessoas[0]
        id_p = p.get("idPessoa")
        nome = p.get("nomePessoa", "")
        r2 = client.get(f"{DURL}/personGroupCompany/new/{id_p}", timeout=15)
        tel = email_r = cpf = ""
        especialidades = []
        macrorregioes = []
        if r2.status_code == 200:
            pd = r2.json().get("data", {}).get("pessoa", {})
            tel        = pd.get("celularFormatado") or pd.get("celular") or ""
            email_r    = pd.get("email") or pd.get("emailProfissional") or ""
            cpf        = pd.get("cpf") or ""
            especialidades = [
                e.get("especialidade", {}).get("nome", "")
                for e in pd.get("especialidadesDaPessoa", [])
                if e.get("especialidade", {}).get("nome")
            ]
            macrorregioes = [
                m.get("nome", "")
                for m in pd.get("macrorregioes", [])
                if m.get("nome")
            ]
        return {
            "crm":           crm,
            "nome":          nome,
            "telefone":      tel,
            "email":         email_r,
            "cpf":           cpf,
            "especialidades": especialidades,
            "macrorregioes":  macrorregioes,
        }
    except Exception as ex:
        return {"crm": crm, "telefone": "", "email": "", "nome": "",
                "cpf": "", "especialidades": [], "macrorregioes": [], "erro": str(ex)}

@app.route("/")
def index():
    return jsonify({"status":"ok","msg":"SisCNRM API"})

@app.route("/instituicoes/<uf>")
def instituicoes(uf):
    try:
        r = requests.get(f"{BASE}/options/sg_estado/{uf}", headers=HEADERS, timeout=30)
        return jsonify(parse_options(r.text))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/programas/<co_entidade>")
def programas(co_entidade):
    try:
        r = requests.get(f"{BASE}/options/co_entidade/{co_entidade}", headers=HEADERS, timeout=30)
        return jsonify(parse_options(r.text))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/programas_uf/<uf>")
def programas_uf(uf):
    """
    Retorna todos os programas únicos (por nome) de todas as instituições de uma UF.
    Cada item tem: nome (str) e ids (lista de IDs das instituições que oferecem o programa).
    Usa cache em memória para evitar reprocessamento.
    """
    uf = uf.upper()
    if uf in _cache_programas_uf:
        return jsonify(_cache_programas_uf[uf])

    try:
        # 1. Busca todas as instituições da UF
        r = requests.get(f"{BASE}/options/sg_estado/{uf}", headers=HEADERS, timeout=30)
        insts = parse_options(r.text)
        if not insts:
            return jsonify([])

        # 2. Busca programas de cada instituição em paralelo (máx 10 threads)
        # nome -> set de ids de instituição
        prog_map = {}  # nome -> list of {inst_id, prog_id}

        def fetch(inst):
            progs = buscar_programas_instituicao(inst["id"])
            return inst["id"], progs

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(fetch, inst): inst for inst in insts}
            for future in as_completed(futures):
                inst_id, progs = future.result()
                for p in progs:
                    nome = p["nome"].strip().upper()
                    if nome not in prog_map:
                        prog_map[nome] = []
                    prog_map[nome].append({
                        "inst_id": inst_id,
                        "prog_id": p["id"]
                    })

        # 3. Monta lista ordenada por nome
        resultado = [
            {"nome": nome, "combos": combos}
            for nome, combos in sorted(prog_map.items())
        ]

        # Salva no cache
        _cache_programas_uf[uf] = resultado
        return jsonify(resultado)

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/contar")
def contar():
    """Retorna só a contagem de certificados para um CRM — rápido."""
    crm = request.args.get("crm", "").strip()
    if not crm:
        return jsonify({"total": 0})
    try:
        session = requests.Session()
        session.get(BASE, headers=HEADERS, timeout=10)
        params = {"par2": crm}
        r = session.get(f"{BASE}/consultarelatorios/", params=params, headers=HEADERS, timeout=30)
        regs = parse_table(r.text)
        return jsonify({"total": len(regs), "crm": crm})
    except Exception as e:
        return jsonify({"total": 0, "error": str(e)})

@app.route("/certificados")
def certificados():
    uf       = request.args.get("uf", "SP")
    inst     = request.args.get("inst", "").strip()
    prog     = request.args.get("prog", "").strip()
    prog_nome= request.args.get("prog_nome", "").strip().upper()
    data_ini = request.args.get("ini", "")
    data_fim = request.args.get("fim", "")
    ocorr    = request.args.get("ocorr", "")
    medico   = request.args.get("medico", "").strip().upper()
    crm      = request.args.get("crm", "").strip()
    cert     = request.args.get("cert", "").strip()
    cert2    = request.args.get("cert2", "").strip()

    d_ini = d_fim = None
    if data_ini and data_fim:
        try:
            d_ini = datetime.strptime(data_ini, FMT)
            d_fim = datetime.strptime(data_fim, FMT)
        except:
            return jsonify({"error": "Formato de data inválido. Use dd/mm/aaaa"}), 400

    try:
        session = requests.Session()
        session.get(BASE, headers=HEADERS, timeout=15)

        # Se tem prog_nome mas não inst/prog: busca por UF sem filtro de prog
        # e filtra localmente pelo nome do programa
        if prog_nome and not inst and not prog:
            params = {}
            if medico: params["par0"] = medico
            if crm:    params["par2"] = crm
            if cert:   params["par3"] = cert
            if uf:     params["par6"] = uf

            r = session.get(f"{BASE}/consultarelatorios/", params=params, headers=HEADERS, timeout=60)
            regs = parse_table(r.text)

            # Filtra localmente pelo nome do programa
            regs = [reg for reg in regs if prog_nome in str(reg.get("Programa","")).upper()]

        else:
            params = {}
            if medico: params["par0"] = medico
            if crm:    params["par2"] = crm
            if cert:   params["par3"] = cert
            if cert2:  params["par4"] = cert2
            if ocorr:  params["par5"] = ocorr
            if uf:     params["par6"] = uf
            if inst:   params["par7"] = inst
            if prog:   params["par8"] = prog

            r = session.get(f"{BASE}/consultarelatorios/", params=params, headers=HEADERS, timeout=60)
            regs = parse_table(r.text)

            # Fallback ano a ano
            if not regs and inst and prog and d_ini and d_fim:
                dedup = set()
                for ano in range(d_ini.year, d_fim.year + 1):
                    p = {**params, "par5": f"01/01/{ano}"}
                    r2 = session.get(f"{BASE}/consultarelatorios/", params=p, headers=HEADERS, timeout=60)
                    for reg in parse_table(r2.text):
                        ch = str(sorted(reg.items()))
                        if ch not in dedup:
                            dedup.add(ch)
                            regs.append(reg)

        # Filtros locais
        if d_ini and d_fim and regs:
            regs = filtrar_por_data(regs, d_ini, d_fim)
        if crm and regs:
            regs = [r for r in regs if crm in str(r.get("CRM",""))]
        if medico and regs:
            regs = [r for r in regs if medico.upper() in str(r.get("Médico","")).upper()]
        if cert and regs:
            regs = [r for r in regs if cert in str(r.get("Nº Certificado","")).split("-")[0]]

        return jsonify({"total": len(regs), "dados": regs})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/telefone")
def telefone():
    """Busca telefone de um médico pelo CRM via DoctorID."""
    email = request.args.get("email","").strip()
    senha = request.args.get("senha","").strip()
    crm   = request.args.get("crm","").strip()
    if not email or not senha or not crm:
        return jsonify({"error": "email, senha e crm obrigatorios"}), 400
    if not HTTPX_OK:
        return jsonify({"error": "httpx nao instalado"}), 500
    import re as _re
    DURL = "https://www.doctorid.com.br"
    try:
        c = httpx.Client(follow_redirects=True, timeout=20)
        c.get(DURL + "/website")
        xsrf = c.cookies.get("XSRF-TOKEN","")
        c.post(DURL + "/manualLogin", data={
            "S_IDENTIFIER": email,
            "S_PASSWORD": senha,
        }, headers={"X-Requested-With":"XMLHttpRequest","X-Xsrf-Token":xsrf})
        if "JSESSIONID" not in c.cookies:
            return jsonify({"error":"login_falhou"}), 401
        xsrf = c.cookies.get("XSRF-TOKEN", xsrf)
        c.headers.update({"X-Requested-With":"XMLHttpRequest","X-Xsrf-Token":xsrf,"Referer":DURL+"/"})
        crm_fmt = _re.sub(r"\D","",crm).ljust(12,"_")
        r = c.post(DURL+"/personGroupCompany/find",
            data={"conselhoRegional":crm_fmt,"cpf":"","nome":""},timeout=15)
        if r.status_code != 200:
            return jsonify({"crm":crm,"telefone":"","nome":""})
        pessoas = r.json().get("data",{}).get("pessoas",[])
        if not pessoas:
            return jsonify({"crm":crm,"telefone":"","nome":""})
        p = pessoas[0]
        id_p = p.get("idPessoa")
        nome = p.get("nomePessoa","")
        r2 = c.get(f"{DURL}/personGroupCompany/new/{id_p}",timeout=15)
        tel = ""
        if r2.status_code == 200:
            pd = r2.json().get("data",{}).get("pessoa",{})
            tel = pd.get("celularFormatado") or pd.get("celular") or ""
        email_ret = pd.get("email") or pd.get("emailProfissional") or ""
        debug = request.args.get("debug","")
        if debug:
            c.close()
            return jsonify({"debug": pd, "crm":crm})
        c.close()
        return jsonify({"crm":crm,"telefone":tel,"email":email_ret,"nome":nome})
    except Exception as e:
        return jsonify({"error":str(e)}), 500

@app.route("/contar_bulk")
def contar_bulk():
    """Verifica múltiplos CRMs de uma vez em paralelo. crms=crm1,crm2,..."""
    crms_raw = request.args.get("crms", "").strip()
    if not crms_raw:
        return jsonify({})
    crms = [c.strip() for c in crms_raw.split(",") if c.strip()]
    crms = crms[:10]  # limite de segurança

    def verificar(crm):
        try:
            session = requests.Session()
            session.get(BASE, headers=HEADERS, timeout=8)
            r = session.get(f"{BASE}/consultarelatorios/",
                            params={"par2": crm}, headers=HEADERS, timeout=20)
            total = len(parse_table(r.text))
            return crm, total
        except:
            return crm, 0

    resultado = {}
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(verificar, crm): crm for crm in crms}
        for future in as_completed(futures):
            crm, total = future.result()
            resultado[crm] = total

    return jsonify(resultado)

@app.route("/telefone_bulk")
def telefone_bulk():
    """Busca telefones de multiplos CRMs com sessao em cache."""
    email  = request.args.get("email","").strip()
    senha  = request.args.get("senha","").strip()
    crms_r = request.args.get("crms","").strip()
    if not email or not senha or not crms_r:
        return jsonify({"error": "params missing"}), 400
    if not HTTPX_OK:
        return jsonify({"error": "httpx nao instalado"}), 500
    crms = [c.strip() for c in crms_r.split(",") if c.strip()][:8]
    client = get_did_session(email, senha)
    if not client:
        return jsonify({"error": "login_falhou"}), 401
    resultado = {}
    for crm in crms:
        data = lookup_crm(client, crm)
        resultado[data["crm"]] = {
            "nome":          data.get("nome", ""),
            "telefone":      data.get("telefone", ""),
            "email":         data.get("email", ""),
            "cpf":           data.get("cpf", ""),
            "especialidades": data.get("especialidades", []),
            "macrorregioes":  data.get("macrorregioes", []),
        }
    return jsonify(resultado)


# ═══════════════════════════════════════════════════════════════
# PROXY CFM — evita bloqueio CORS do browser
# ═══════════════════════════════════════════════════════════════
CFM_API = "https://portal.cfm.org.br/api_rest_php/api/v2/medicos"
CFM_HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://portal.cfm.org.br",
    "Referer": "https://portal.cfm.org.br/busca-medicos/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}

@app.route("/cfm/municipios/<uf>")
def cfm_municipios(uf):
    """Retorna lista de municípios de uma UF via proxy do CFM."""
    try:
        r = requests.get(
            f"{CFM_API}/listar_municipios/{uf.upper()}",
            headers=CFM_HEADERS,
            timeout=15,
        )
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/cfm/especialidades")
def cfm_especialidades():
    """Retorna lista de especialidades via proxy do CFM."""
    try:
        r = requests.get(
            f"{CFM_API}/listar_especialidades",
            headers=CFM_HEADERS,
            timeout=15,
        )
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/cfm/buscar", methods=["POST"])
def cfm_buscar():
    """
    Proxy para buscar médicos no CFM.
    Recebe o payload completo (com token captcha) e repassa ao CFM.
    Body: { token, uf, municipio_id, especialidade_id, situacao, tipo, pagina, pageSize }
    """
    try:
        body = request.get_json()
        token        = body.get("token", "")
        uf           = body.get("uf", "")
        municipio_id = body.get("municipio_id", "")
        esp_id       = body.get("especialidade_id", "")
        situacao     = body.get("situacao", "")
        tipo         = body.get("tipo", "")
        pagina       = int(body.get("pagina", 1))
        page_size    = int(body.get("pageSize", 50))

        if not token:
            return jsonify({"error": "token obrigatorio"}), 400

        payload = [{
            "useCaptchav2": True,
            "captcha": token,
            "medico": {
                "nome": "",
                "ufMedico": uf,
                "crmMedico": "",
                "municipioMedico": municipio_id,
                "tipoInscricaoMedico": tipo,
                "situacaoMedico": situacao,
                "detalheSituacaoMedico": "",
                "especialidadeMedico": esp_id,
                "areaAtuacaoMedico": "",
            },
            "page": pagina,
            "pageNumber": pagina,
            "pageSize": page_size,
        }]

        r = requests.post(
            f"{CFM_API}/buscar_medicos",
            json=payload,
            headers=CFM_HEADERS,
            timeout=30,
        )
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Reumatologia (SBR — reumatologia.org.br) ─────────────────────────────────
# Diretório de associados com Título de Especialista (plugin Search & Filter Pro).
# Fluxo: lista (JSON) -> perfil de cada associado (CRM/RQE/telefone/endereço).
REUMA_BASE = "https://www.reumatologia.org.br"
REUMA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "pt-BR,pt;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": REUMA_BASE + "/procure/",
}

def reuma_parse_lista(results_html):
    soup = BeautifulSoup(results_html, "html.parser")
    out = []
    for box in soup.select(".box-associado"):
        h3 = box.find("h3")
        a = h3.find("a") if h3 else None
        if not a:
            continue
        nome = a.get_text(strip=True)
        m = re.search(r"/associado/([^/]+)/?", a.get("href", ""))
        slug = m.group(1) if m else ""
        cidade = ""; uf = ""
        titulo = pediatrico = expres = False
        for span in box.select("span.selos"):
            img = span.find("img")
            src = img.get("src", "") if img else ""
            txt = span.get_text(strip=True)
            if "icone-cidade" in src:
                cidade = txt
                mm = re.search(r"-\s*([A-Z]{2})\s*$", txt)
                if mm: uf = mm.group(1)
            elif "icone-pediatra" in src:
                pediatrico = True
            elif "icone-presidente" in src:
                expres = True
            elif "icone-titulo" in src:
                titulo = True
        out.append({"nome": nome, "slug": slug, "cidade": cidade, "uf": uf,
                    "titulo": titulo, "pediatrico": pediatrico, "ex_presidente": expres})
    return out

def reuma_total(results_html):
    m = re.search(r"encontrou\s*<strong>\s*(\d+)", results_html)
    return int(m.group(1)) if m else None

def reuma_parse_perfil(html):
    soup = BeautifulSoup(html, "html.parser")
    text = re.split(r"Refine sua busca", soup.get_text("\n"))[0]
    crm = ""; crm_uf = ""
    m = re.search(r"CRM\s*(\d+)\s*-?\s*([A-Za-z]{2})", text)
    if m:
        crm = m.group(1); crm_uf = m.group(2).upper()
    rqe = re.findall(r"RQE\s*(\d+)", text)
    enderecos = [e.strip() for e in re.findall(r"Endere[çc]o:\s*(.+)", text)]
    telefones = [t.strip() for t in re.findall(r"Telefone:\s*(.+)", text)]
    return {"crm": crm, "crm_uf": crm_uf, "rqe": ", ".join(rqe),
            "titulo": "Título de Especialista" in text,
            "enderecos": enderecos, "telefones": telefones}

@app.route("/reuma/buscar")
def reuma_buscar():
    """Lista associados da SBR. Params: estado(slug), cidade(value), especialidade(slug), nome, pagina, ppp."""
    estado = request.args.get("estado", "")
    cidade = request.args.get("cidade", "")
    esp    = request.args.get("especialidade", "")
    nome   = request.args.get("nome", "")
    pagina = int(request.args.get("pagina", 1))
    ppp    = int(request.args.get("ppp", 100))
    params = {"sfid": "81", "sf_action": "get_data", "sf_data": "all",
              "_sf_ppp[]": ppp, "_sf_paged": pagina}
    if estado: params["_sft_estado[]"] = estado
    if cidade: params["_sfm_cidade[]"] = cidade
    if esp:    params["_sft_especialidade[]"] = esp
    if nome:   params["_sf_search[]"] = nome
    try:
        r = requests.get(REUMA_BASE + "/", params=params, headers=REUMA_HEADERS, timeout=30)
        data = r.json()
        results_html = data.get("results", "")
        return jsonify({"total": reuma_total(results_html),
                        "associados": reuma_parse_lista(results_html)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/reuma/detalhe_bulk", methods=["POST"])
def reuma_detalhe_bulk():
    """Recebe {slugs:[...]}, busca cada perfil em paralelo e extrai CRM/RQE/telefone/endereço."""
    body = request.get_json() or {}
    slugs = body.get("slugs", [])
    if not slugs:
        return jsonify({"error": "slugs obrigatorio"}), 400
    def fetch(slug):
        try:
            r = requests.get(f"{REUMA_BASE}/associado/{slug}/", headers=REUMA_HEADERS, timeout=20)
            d = reuma_parse_perfil(r.text)
        except Exception as ex:
            d = {"crm": "", "crm_uf": "", "rqe": "", "titulo": False,
                 "enderecos": [], "telefones": [], "erro": str(ex)}
        d["slug"] = slug
        return d
    out = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(fetch, s) for s in slugs]
        for f in as_completed(futs):
            out.append(f.result())
    return jsonify({"detalhes": out})

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
