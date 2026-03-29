from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from playwright.async_api import async_playwright
from supabase import create_client
import os, asyncio

app = FastAPI()

SEBRAE_URL = "https://app2.pr.sebrae.com.br"
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

@app.post("/buscar-cliente")
async def buscar_cliente(req: ScrapeRequest):
    try:
        dados = await scrape_cliente(req.codigo_cliente)
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        supabase.table("clientes").update({
            "razao_social": dados.get("razao_social"),
            "nome_fantasia": dados.get("nome_fantasia"),
            "cnpj": dados.get("cnpj"),
            "porte": dados.get("porte"),
        }).eq("id", req.cliente_id).execute()
        return {"sucesso": True, "dados": dados}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def scrape_cliente(codigo: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        await page.goto(f"{SEBRAE_URL}/login")
        await page.wait_for_load_state("networkidle")
        await page.fill("input[name='username'], input[type='email'], #username", SEBRAE_USER)
        await page.fill("input[name='password'], input[type='password'], #password", SEBRAE_PASS)
        await page.click("button[type='submit']")
        await page.wait_for_load_state("networkidle")

        await page.goto(f"{SEBRAE_URL}/crm/cadastrarPessoaJuridica/{codigo}")
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(3)

        js = '''() => {
            const getText = (sel) => {
                const el = document.querySelector(sel);
                return el ? el.innerText.trim() : null;
            };
            return {
                razao_social: getText('[placeholder*="Razao"]'),
                nome_fantasia: getText('[placeholder*="Fantasia"]'),
                cnpj: getText('[placeholder*="CNPJ"]'),
                porte: getText('[placeholder*="Porte"]'),
            };
        }'''

        dados = await page.evaluate(js)
        await browser.close()
        return dados
