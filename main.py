from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from playwright.async_api import async_playwright
from supabase import create_client
import os, asyncio, httpx, urllib.parse, json

app = FastAPI()

SEBRAE_URL = "https://app2.pr.sebrae.com.br"
SEBRAE_API = "https://api.pr.sebrae.com.br/crm-api"
SEBRAE_USER = os.getenv("SEBRAE_USER")
SEBRAE_PASS = os.getenv("SEBRAE_PASS")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")


class ScrapeRequest(BaseModel):
    codigo_cliente: str
    cliente_id: str


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/debug-login")
async def debug_login():
    """
    Endpoint de debug: executa o login e retorna TODOS os cookies capturados,
    sem tentar parsear nada. Use este endpoint primeiro para descobrir o cookie correto.
    """
    resultado = await _debug_completo_login()
    return resultado


@app.post("/buscar-cliente")
async def buscar_cliente(req: ScrapeRequest):
    try:
        app_key, token = await get_token()
        if not app_key or not token:
            raise Exception("Falha no login — token não encontrado")

        headers = {
            "App_key": app_key,
            "Authorization": token,
            "Content-Type": "application/json"
        }

        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{SEBRAE_API}/agente/{req.codigo_cliente}", headers=headers)
            empresa = r.json() if r.status_code == 200 else {}

            r = await client.get(f"{SEBRAE_API}/agente/{req.codigo_cliente}/telefone", headers=headers)
            telefones_empresa = r.json() if r.status_code == 200 else []

            r = await client.get(f"{SEBRAE_API}/agente/{req.codigo_cliente}/email", headers=headers)
            emails_empresa = r.json() if r.status_code == 200 else []

            r = await client.get(f"{SEBRAE_API}/agente/{req.codigo_cliente}/vinculo", headers=headers)
            socios = r.json() if r.status_code == 200 else []

        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

        supabase.table("clientes").update({
            "nome_fantasia": empresa.get("nomeFantasia") or empresa.get("nome"),
            "cnpj": empresa.get("docId"),
        }).eq("id", req.cliente_id).execute()

        for tel in (telefones_empresa if isinstance(telefones_empresa, list) else []):
            numero = tel.get("numero") or tel.get("telefone") or str(tel)
            if numero:
                supabase.table("telefones").insert({
                    "cliente_id": req.cliente_id,
                    "numero": numero,
                }).execute()

        for em in (emails_empresa if isinstance(emails_empresa, list) else []):
            endereco = em.get("email") or em.get("endereco") or str(em)
            if endereco:
                supabase.table("emails").insert({
                    "cliente_id": req.cliente_id,
                    "email": endereco,
                }).execute()

        pessoas_salvas = []
        async with httpx.AsyncClient(timeout=30) as client:
            for socio in (socios if isinstance(socios, list) else []):
                cod_pf = socio.get("codigo")
                if not cod_pf:
                    continue

                r = await client.get(f"{SEBRAE_API}/agente/{cod_pf}", headers=headers)
                pf = r.json() if r.status_code == 200 else {}

                r = await client.get(f"{SEBRAE_API}/agente/{cod_pf}/telefone", headers=headers)
                tels_pf = r.json() if r.status_code == 200 else []

                r = await client.get(f"{SEBRAE_API}/agente/{cod_pf}/email", headers=headers)
                emails_pf = r.json() if r.status_code == 200 else []

                pessoa_resp = supabase.table("pessoas").insert({
                    "cliente_id": req.cliente_id,
                    "nome": pf.get("nome") or pf.get("descricao"),
                    "apelido": pf.get("nomeFantasia"),
                    "vinculo": socio.get("vinculo", {}).get("descricao") if socio.get("vinculo") else None,
                    "codigo_sebrae": str(cod_pf),
                }).execute()

                pessoa_id = pessoa_resp.data[0]["id"] if pessoa_resp.data else None

                for tel in (tels_pf if isinstance(tels_pf, list) else []):
                    numero = tel.get("numero") or tel.get("telefone") or str(tel)
                    if numero and pessoa_id:
                        supabase.table("telefones").insert({
                            "pessoa_id": pessoa_id,
                            "numero": numero,
                        }).execute()

                for em in (emails_pf if isinstance(emails_pf, list) else []):
                    endereco = em.get("email") or em.get("endereco") or str(em)
                    if endereco and pessoa_id:
                        supabase.table("emails").insert({
                            "pessoa_id": pessoa_id,
                            "email": endereco,
                        }).execute()

                pessoas_salvas.append(pf.get("nome") or str(cod_pf))

        return {
            "sucesso": True,
            "empresa": empresa.get("nomeFantasia") or empresa.get("nome"),
            "socios": pessoas_salvas,
            "debug": {
                "empresa_raw": empresa,
                "socios_raw": socios[:2] if socios else []
            }
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def _debug_completo_login():
    """
    Executa o login completo com captura de popup e retorna todos os cookies
    de todos os domínios, com valor parcial para diagnóstico.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context()
        page = await context.new_page()

        log = []

        try:
            # ETAPA 1: Login
            log.append("Etapa 1: abrindo login.do...")
            await page.goto(f"{SEBRAE_URL}/SebraePR/login.do", wait_until="domcontentloaded")
            await asyncio.sleep(2)

            await page.fill("input[name='usuario']", SEBRAE_USER)
            await page.fill("input[name='senha']", SEBRAE_PASS)
            await page.click("input[type='image'][alt='Ok']")
            await asyncio.sleep(3)
            log.append(f"Etapa 1 OK — URL atual: {page.url}")

            # Cookies após etapa 1
            cookies_e1 = await context.cookies()
            log.append(f"Cookies após etapa 1: {[c['name']+'@'+c['domain'] for c in cookies_e1]}")

            # ETAPA 2: Confirmar unidade
            log.append("Etapa 2: confirmando unidade...")
            try:
                await page.click("input[type='image'][alt='Entrar no Sistema']", timeout=5000)
            except Exception as e2:
                log.append(f"Etapa 2 erro no click: {e2} — tentando continuar")
            await asyncio.sleep(3)
            log.append(f"Etapa 2 OK — URL atual: {page.url}")

            # Cookies após etapa 2
            cookies_e2 = await context.cookies()
            log.append(f"Cookies após etapa 2: {[c['name']+'@'+c['domain'] for c in cookies_e2]}")

            # ETAPA 3: Clicar em SMART — pode abrir popup
            log.append("Etapa 3: clicando em SMART...")
            popup_page = None

            async def handle_popup(popup):
                nonlocal popup_page
                popup_page = popup
                log.append(f"Popup detectado: {popup.url}")

            context.on("page", handle_popup)

            try:
                await page.click("img[src*='btn_smart']", timeout=5000)
            except Exception as e3:
                log.append(f"Etapa 3 erro no click img: {e3} — tentando link SMART")
                try:
                    await page.click("a[href*='smart'], a[href*='SMART']", timeout=3000)
                except Exception as e3b:
                    log.append(f"Etapa 3 fallback também falhou: {e3b}")

            # Aguarda popup ou navegação
            await asyncio.sleep(5)

            # Se abriu popup, aguarda carregar
            if popup_page:
                log.append(f"Aguardando popup carregar: {popup_page.url}")
                try:
                    await popup_page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    await asyncio.sleep(5)
                log.append(f"Popup URL final: {popup_page.url}")

            log.append(f"URL principal após etapa 3: {page.url}")

            # Coleta TODOS os cookies de TODOS os contextos
            cookies_final = await context.cookies()

            # Monta resposta de debug detalhada
            cookies_info = []
            for c in cookies_final:
                val_preview = c["value"][:80] + "..." if len(c["value"]) > 80 else c["value"]
                cookies_info.append({
                    "name": c["name"],
                    "domain": c["domain"],
                    "path": c["path"],
                    "value_preview": val_preview,
                    "value_length": len(c["value"]),
                    "httpOnly": c.get("httpOnly"),
                    "secure": c.get("secure"),
                })

            # Tenta parsear candidatos a token
            candidatos = {}
            keywords = ["crm", "app", "token", "auth", "session", "smart", "key"]
            for c in cookies_final:
                nome_lower = c["name"].lower()
                if any(k in nome_lower for k in keywords):
                    try:
                        val_decoded = urllib.parse.unquote(c["value"])
                        parsed = json.loads(val_decoded)
                        candidatos[c["name"]] = {
                            "domain": c["domain"],
                            "parsed": parsed,
                            "tem_appKey": "appKey" in parsed,
                            "tem_token": "token" in parsed,
                        }
                    except Exception:
                        candidatos[c["name"]] = {
                            "domain": c["domain"],
                            "value_raw": c["value"][:120],
                        }

            await browser.close()

            return {
                "sucesso": True,
                "log": log,
                "total_cookies": len(cookies_final),
                "cookies_detalhados": cookies_info,
                "candidatos_a_token": candidatos,
            }

        except Exception as e:
            await browser.close()
            return {
                "sucesso": False,
                "log": log,
                "erro": str(e),
            }


async def get_token():
    """
    Executa o login completo e retorna (app_key, token).
    Tenta múltiplos nomes de cookie para encontrar as credenciais.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context()
        page = await context.new_page()

        popup_page = None

        async def handle_popup(popup):
            nonlocal popup_page
            popup_page = popup

        context.on("page", handle_popup)

        try:
            # Etapa 1
            await page.goto(f"{SEBRAE_URL}/SebraePR/login.do", wait_until="domcontentloaded")
            await asyncio.sleep(2)
            await page.fill("input[name='usuario']", SEBRAE_USER)
            await page.fill("input[name='senha']", SEBRAE_PASS)
            await page.click("input[type='image'][alt='Ok']")
            await asyncio.sleep(3)

            # Etapa 2
            try:
                await page.click("input[type='image'][alt='Entrar no Sistema']", timeout=5000)
            except Exception:
                pass
            await asyncio.sleep(3)

            # Etapa 3 — SMART (pode abrir popup)
            try:
                await page.click("img[src*='btn_smart']", timeout=5000)
            except Exception:
                try:
                    await page.click("a[href*='smart'], a[href*='SMART']", timeout=3000)
                except Exception:
                    pass

            await asyncio.sleep(5)

            # Aguarda popup se abriu
            if popup_page:
                try:
                    await popup_page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    await asyncio.sleep(5)

            cookies = await context.cookies()

            # Tenta encontrar token em qualquer cookie com keywords relevantes
            keywords = ["crm", "app", "token", "auth", "smart", "key"]
            for c in cookies:
                if any(k in c["name"].lower() for k in keywords):
                    try:
                        val = urllib.parse.unquote(c["value"])
                        data = json.loads(val)
                        app_key = data.get("appKey") or data.get("app_key") or data.get("AppKey")
                        token = data.get("token") or data.get("Token") or data.get("authorization")
                        if app_key and token:
                            await browser.close()
                            return app_key, token
                    except Exception:
                        pass

            # Se não achou via JSON, tenta cookies individuais
            app_key_val = None
            token_val = None
            for c in cookies:
                name_lower = c["name"].lower()
                if "appkey" in name_lower or "app_key" in name_lower:
                    app_key_val = c["value"]
                if "token" in name_lower or "authorization" in name_lower:
                    token_val = c["value"]

            await browser.close()

            if app_key_val and token_val:
                return app_key_val, token_val

            # Falhou — retorna lista de cookies para diagnóstico
            nomes = [c["name"] + "@" + c["domain"] for c in cookies]
            raise Exception(f"Token não encontrado. Cookies disponíveis: {nomes}")

        except Exception as e:
            await browser.close()
            raise e
