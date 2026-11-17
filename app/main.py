"""API e página do agente de atendimento."""
from __future__ import annotations

import os
import time
import uuid
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent import CHAMADOS, Sessao, Simulado, criar_modelo

app = FastAPI(title="Agente de atendimento — Mercadinho Exemplo")
# O portfólio (GitHub Pages) conversa com o agente direto do navegador.
app.add_middleware(CORSMiddleware, allow_origins=["https://p-lucas-s.github.io"],
                   allow_methods=["GET", "POST"], allow_headers=["Content-Type"])
STATIC = Path(__file__).parent / "static"
modelo = criar_modelo()

SESSOES: dict[str, tuple[Sessao, float]] = {}
LIMITE_SESSAO = int(os.environ.get("LIMITE_MENSAGENS_SESSAO", 25))
LIMITE_DIA = int(os.environ.get("LIMITE_MENSAGENS_DIA", 300))
_uso = {"dia": date.today(), "total": 0}


class Entrada(BaseModel):
    sessao: str | None = None
    mensagem: str = Field(min_length=1, max_length=600)


def _sessao(sid: str | None) -> Sessao:
    agora = time.time()
    for k, (_, t) in list(SESSOES.items()):  # descarta conversas paradas há 1 hora
        if agora - t > 3600:
            del SESSOES[k]
    if sid and sid in SESSOES:
        s = SESSOES[sid][0]
    else:
        s = Sessao(id=uuid.uuid4().hex[:12])
    SESSOES[s.id] = (s, agora)
    return s


@app.post("/api/chat")
def chat(e: Entrada):
    if _uso["dia"] != date.today():
        _uso.update(dia=date.today(), total=0)
    s = _sessao(e.sessao)
    if _uso["total"] >= LIMITE_DIA:
        return {"sessao": s.id, "resposta": "A demonstração atingiu o limite de mensagens de hoje. Volte amanhã!", "passos": [], "transferida": s.transferida}
    if s.mensagens >= LIMITE_SESSAO:
        return {"sessao": s.id, "resposta": "Esta conversa chegou ao limite da demonstração. Recarregue a página para começar outra.", "passos": [], "transferida": s.transferida}
    s.mensagens += 1
    _uso["total"] += 1
    try:
        resposta, passos = modelo.responder(s, e.mensagem.strip())
    except Exception as err:  # erro do provedor (cota, rede): a página segue funcionando
        print(f"erro do modelo: {err!r}"[:600], flush=True)  # aparece nos logs do Render
        raise HTTPException(503, f"O modelo não respondeu agora ({type(err).__name__}). Tente de novo em instantes.")
    return {"sessao": s.id, "resposta": resposta, "passos": passos, "transferida": s.transferida}


@app.get("/api/chamados")
def chamados():
    return list(reversed(CHAMADOS[-50:]))


@app.get("/api/health")
def health():
    return {"ok": True, "modo": "simulado" if isinstance(modelo, Simulado) else "gemini"}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
