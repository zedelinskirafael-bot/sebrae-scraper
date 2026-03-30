from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from playwright.async_api import async_playwright
from supabase import create_client
import os, asyncio, httpx, re

app = FastAPI()

SEBRAE_URL = "https://app2.pr.sebrae.com.br"
SEBRAE_API = "https://api.pr.sebrae.com.br/crm-api"
SEBRAE_USER = os.getenv("SEBRAE_USER")
SEBRAE_PASS = os.getenv("SEBRAE_PASS")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
APP_KEY = os.getenv("APP_KEY")


class ScrapeRequest(BaseModel):
    codigo_cliente: str
    cliente_id: str


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/debug-login")
async def debug_login():
    log = []
    try:
        token = await get_token()
        log.append(f"Token capturado: {token}")
        headers = {
            "App_key": APP_KEY,
            "Authorization": token,
            "Content-Type": "application/json"
        }
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{SEBRAE_API}/agente/268934", headers=headers)
            log.append(f"API status: {r.status_code}")
            log.append(f"Resposta: {r.text[:300]}")
        return {"sucesso": True, "log": log}
    except Exception as e:
        return {"sucesso": False, "log": log, "erro": str(e)}


@app.post("/buscar-cliente")
async def buscar_cliente(req: ScrapeRequest):
    try:
        # 1. Busca token do Sebrae
        token = await get_token()
        headers = {
            "App_key": APP_KEY,
            "Authorization": token,
            "Content-Type": "application/json"
        }

        # 2. Busca dados do Sebrae
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{SEBRAE_API}/agente/{req.codigo_cliente}", headers=headers)
            empresa = r.json() if r.status_code == 200 else {}

            r = await client.get(f"{SEBRAE_API}/agente/{req.codigo_cliente}/telefone", headers=headers)
            telefones_empresa = r.json() if r.status_code == 200 else []

            r = await client.get(f"{SEBRAE_API}/agente/{req.codigo_cliente}/email", headers=headers)
            emails_empresa = r.json() if r.status_code == 200 else []

            r = await client.get(f"{SEBRAE_API}/agente/{req.codigo_cliente}/vinculo", headers=headers)
            socios = r.json() if r.status_code == 200 else []

        # 3. Conecta Supabase e busca o cliente para pegar organizacao_id e usuario_id
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        cliente_resp = supabase.table("clientes").select("organizacao_id, usuario_id").eq("id", req.cliente_id).single().execute()
        cliente_data = cliente_resp.data or {}
        org_id = cliente_data.get("organizacao_id")
        user_id = cliente_data.get("usuario_id")

        # 4. Atualiza nome_fantasia do cliente
        supabase.table("clientes").update({
            "nome_fantasia": empresa.get("nomeFantasia") or empresa.get("nome"),
        }).eq("id", req.cliente_id).execute()

        # 5. Salva telefones da empresa (referencia_id = cliente_id)
        for tel in (telefones_empresa if isinstance(telefones_empresa, list) else []):
            numero = tel.get("numero") or tel.get("telefone") or str(tel)
            if numero:
                supabase.table("telefones").insert({
                    "organizacao_id": org_id,
                    "usuario_id": user_id,
                    "referencia_id": req.cliente_id,
                    "numero": numero,
                    "tipo": tel.get("tipo") or "outros",
                }).execute()

        # 6. Salva emails da empresa (referencia_id = cliente_id)
        for em in (emails_empresa if isinstance(emails_empresa, list) else []):
            endereco = em.get("email") or em.get("endereco") or str(em)
            if endereco:
                supabase.table("emails").insert({
                    "organizacao_id": org_id,
                    "usuario_id": user_id,
                    "referencia_id": req.cliente_id,
                    "endereco": endereco,
                    "tipo": em.get("tipo") or "outros",
                }).execute()

        # 7. Salva sócios
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

                # Insere pessoa
                pessoa_resp = supabase.table("pessoas").insert({
                    "organizacao_id": org_id,
                    "usuario_id": user_id,
                    "cliente_id": req.cliente_id,
                    "nome": pf.get("nome") or pf.get("descricao"),
                    "codigo_socio": str(cod_pf),
                }).execute()

                pessoa_id = pessoa_resp.data[0]["id"] if pessoa_resp.data else None

                # Telefones do sócio (referencia_id = pessoa_id)
                for tel in (tels_pf if isinstance(tels_pf, list) else []):
                    numero = tel.get("numero") or tel.get("telefone") or str(tel)
                    if numero and pessoa_id:
                        supabase.table("telefones").insert({
                            "organizacao_id": org_id,
                            "usuario_id": user_id,
                            "referencia_id": pessoa_id,
                            "numero": numero,
                            "tipo": tel.get("tipo") or "outros",
                        }).execute()

                # Emails do sócio (referencia_id = pessoa_id)
                for em in (emails_pf if isinstance(emails_pf, list) else []):
                    endereco = em.get("email") or em.get("endereco") or str(em)
                    if endereco and pessoa_id:
                        supabase.table("emails").insert({
                            "organizacao_id": org_id,
                            "usuario_id": user_id,
                            "referencia_id": pessoa_id,
                            "endereco": endereco,
                            "tipo": em.get("tipo") or "outros",
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


async def get_token() -> str:
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
            await page.goto(f"{SEBRAE_URL}/SebraePR/login.do", wait_until="domcontentloaded")
            await asyncio.sleep(2)
            await page.fill("input[name='usuario']", SEBRAE_USER)
            await page.fill("input[name='senha']", SEBRAE_PASS)
            await page.click("input[type='image'][alt='Ok']")
            await asyncio.sleep(3)

            try:
                await page.click("input[type='image'][alt='Entrar no Sistema']", timeout=5000)
            except Exception:
                pass
            await asyncio.sleep(3)

            try:
                await page.click("img[src*='btn_smart']", timeout=5000)
            except Exception:
                pass
            await asyncio.sleep(5)

            if popup_page:
                try:
                    await popup_page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    await asyncio.sleep(5)

                try:
                    await popup_page.hover("text=Pessoas", timeout=5000)
                    await asyncio.sleep(1)
                    await popup_page.click("text=Cadastro/Consulta", timeout=5000)
                    await asyncio.sleep(3)
                except Exception:
                    try:
                        await popup_page.goto(
                            f"{SEBRAE_URL}/crm/consultarcliente",
                            wait_until="domcontentloaded",
                            timeout=15000
                        )
                        await asyncio.sleep(3)
                    except Exception:
                        pass

                url_atual = popup_page.url
                token = _extrair_token_da_url(url_atual)
                await browser.close()
                if token:
                    return token
                raise Exception(f"Token não encontrado na URL: {url_atual}")

            await browser.close()
            raise Exception("Popup não detectado")

        except Exception as e:
            try:
                await browser.close()
            except Exception:
                pass
            raise e


def _extrair_token_da_url(url: str) -> str | None:
    match = re.search(r'[?&]token=([a-f0-9\-]{36})', url, re.IGNORECASE)
    return match.group(1) if match else None
