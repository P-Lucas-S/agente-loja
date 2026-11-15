"""Agente de atendimento do Mercadinho Exemplo.

O modelo (Gemini) decide quais ferramentas chamar; o código executa as
ferramentas sobre dados locais e devolve o resultado ao modelo, num laço de
no máximo MAX_PASSOS rodadas. Cada chamada fica registrada em `passos`, que o
front-end mostra no painel "Bastidores".
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

DADOS = Path(__file__).parent / "data"
PRODUTOS: list[dict] = json.loads((DADOS / "produtos.json").read_text(encoding="utf-8"))
PEDIDOS: dict[str, dict] = {p["numero"]: p for p in json.loads((DADOS / "pedidos.json").read_text(encoding="utf-8"))}
POLITICAS = (DADOS / "politicas.md").read_text(encoding="utf-8")
MAX_PASSOS = 5

SISTEMA = f"""Você é a atendente virtual do Mercadinho Exemplo, uma loja de bairro (fictícia).
Responda em português do Brasil, com frases curtas e simpáticas, sem inventar nada.

Regras:
- Preço, estoque e produtos: sempre use a ferramenta buscar_produtos antes de responder.
- Situação de pedido: use consultar_pedido. Se o cliente não disser o número, peça.
- Reclamação, troca, produto com defeito ou sugestão: registre com abrir_chamado e informe o número do chamado.
- Se o cliente pedir uma pessoa, estiver irritado depois de duas tentativas, ou o assunto fugir do que a loja faz, use transferir_para_humano.
- Dúvidas sobre horário, entrega, pagamento e trocas: responda com base nas informações abaixo.
- Não faça pedidos nem cobranças; diga que o pedido é feito pelo WhatsApp da loja.

Informações da loja:
{POLITICAS}"""


# ---------------------------------------------------------------- ferramentas

def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s.lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


@dataclass
class Sessao:
    id: str
    historico: list = field(default_factory=list)  # list[types.Content] no modo Gemini
    mensagens: int = 0
    transferida: bool = False


CHAMADOS: list[dict] = []


def buscar_produtos(termo: str) -> dict:
    palavras = [p for p in re.findall(r"\w+", _norm(termo)) if len(p) > 2]
    achados = [p for p in PRODUTOS if palavras and all(w in _norm(p["nome"] + " " + p["categoria"]) for w in palavras)]
    if not achados and palavras:  # aceita correspondência parcial
        achados = [p for p in PRODUTOS if any(w in _norm(p["nome"]) for w in palavras)]
    return {"encontrados": [
        {"nome": p["nome"], "preco": f"R$ {p['preco']:.2f}".replace(".", ","),
         "disponivel": p["estoque"] > 0, "estoque": p["estoque"]} for p in achados[:8]]}


def consultar_pedido(numero: str) -> dict:
    p = PEDIDOS.get(re.sub(r"\D", "", str(numero)))
    if not p:
        return {"erro": "Pedido não encontrado. Confira o número (4 dígitos)."}
    return {k: p[k] for k in ("numero", "status", "previsao", "itens", "pagamento")}


def abrir_chamado(tipo: str, descricao: str, sessao: Sessao, pedido: str | None = None) -> dict:
    numero = f"CH-{len(CHAMADOS) + 1:04d}"
    CHAMADOS.append({"numero": numero, "tipo": tipo, "descricao": descricao[:500], "pedido": pedido,
                     "sessao": sessao.id, "criado_em": datetime.now().strftime("%d/%m %H:%M")})
    return {"chamado": numero, "prazo_resposta": "até 1 dia útil"}


def transferir_para_humano(motivo: str, sessao: Sessao) -> dict:
    sessao.transferida = True
    return {"transferido": True, "atendimento_humano": "segunda a sábado, das 8h às 18h", "motivo": motivo}


FERRAMENTAS = [
    {"name": "buscar_produtos", "description": "Busca produtos do mercadinho por nome ou categoria. Retorna preço e disponibilidade.",
     "parameters_json_schema": {"type": "object", "properties": {"termo": {"type": "string", "description": "Ex.: 'arroz', 'leite desnatado'"}}, "required": ["termo"]}},
    {"name": "consultar_pedido", "description": "Consulta a situação de um pedido de entrega pelo número.",
     "parameters_json_schema": {"type": "object", "properties": {"numero": {"type": "string", "description": "Número do pedido, 4 dígitos"}}, "required": ["numero"]}},
    {"name": "abrir_chamado", "description": "Registra reclamação, pedido de troca, produto com defeito ou sugestão.",
     "parameters_json_schema": {"type": "object", "properties": {
         "tipo": {"type": "string", "enum": ["reclamacao", "troca", "defeito", "sugestao"]},
         "descricao": {"type": "string", "description": "Resumo do problema nas palavras do cliente"},
         "pedido": {"type": "string", "description": "Número do pedido, se houver"}}, "required": ["tipo", "descricao"]}},
    {"name": "transferir_para_humano", "description": "Encaminha a conversa para um atendente humano.",
     "parameters_json_schema": {"type": "object", "properties": {"motivo": {"type": "string"}}, "required": ["motivo"]}},
]


def executar(nome: str, args: dict, sessao: Sessao) -> dict:
    try:
        if nome == "buscar_produtos":
            return buscar_produtos(str(args.get("termo", "")))
        if nome == "consultar_pedido":
            return consultar_pedido(str(args.get("numero", "")))
        if nome == "abrir_chamado":
            return abrir_chamado(str(args.get("tipo", "reclamacao")), str(args.get("descricao", "")), sessao, args.get("pedido"))
        if nome == "transferir_para_humano":
            return transferir_para_humano(str(args.get("motivo", "")), sessao)
    except Exception as e:  # a ferramenta nunca derruba a conversa
        return {"erro": f"falha na ferramenta: {e}"}
    return {"erro": f"ferramenta desconhecida: {nome}"}


# ---------------------------------------------------------------- modelo

class Gemini:
    """Laço de chamada de ferramentas com o SDK google-genai."""

    def __init__(self, api_key: str, modelo: str):
        from google import genai
        from google.genai import types
        self.types = types
        # 25 s por chamada e 2 tentativas: com a cota gratuita estourada a página responde logo, em vez de travar
        self.client = genai.Client(api_key=api_key, http_options=types.HttpOptions(
            timeout=25_000, retry_options=types.HttpRetryOptions(attempts=2, initial_delay=1, max_delay=4)))
        self.modelo = modelo
        self.config = types.GenerateContentConfig(
            system_instruction=SISTEMA,
            temperature=0.3,
            tools=[types.Tool(function_declarations=[types.FunctionDeclaration(**f) for f in FERRAMENTAS])],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

    def responder(self, sessao: Sessao, mensagem: str) -> tuple[str, list[dict]]:
        t = self.types
        conteudo = list(sessao.historico) + [t.Content(role="user", parts=[t.Part.from_text(text=mensagem)])]
        passos: list[dict] = []
        texto = ""
        for _ in range(MAX_PASSOS):
            resp = self.client.models.generate_content(model=self.modelo, contents=conteudo, config=self.config)
            if not resp.candidates or not resp.candidates[0].content:
                texto = "Desculpe, não consegui responder agora. Pode repetir?"
                break
            conteudo.append(resp.candidates[0].content)
            chamadas = resp.function_calls or []
            if not chamadas:
                texto = resp.text or ""
                break
            respostas = []
            for fc in chamadas:
                args = dict(fc.args or {})
                resultado = executar(fc.name, args, sessao)
                passos.append({"ferramenta": fc.name, "argumentos": args, "resultado": resultado})
                respostas.append(t.Part.from_function_response(name=fc.name, response={"result": resultado}))
            conteudo.append(t.Content(role="user", parts=respostas))
        else:
            texto = "Desculpe, me enrolei aqui. Vou chamar uma pessoa para te ajudar."
        sessao.historico = conteudo[-30:]  # janela curta para não estourar o plano gratuito
        return texto.strip(), passos


class Simulado:
    """Sem chave de API: regras simples que imitam o modelo. Usado nos testes
    e para a página abrir mesmo sem GEMINI_API_KEY."""

    def responder(self, sessao: Sessao, mensagem: str) -> tuple[str, list[dict]]:
        m = _norm(mensagem)
        passos = []

        def usar(nome, args):
            r = executar(nome, args, sessao)
            passos.append({"ferramenta": nome, "argumentos": args, "resultado": r})
            return r

        if any(w in m for w in ("pessoa", "humano", "atendente")):
            usar("transferir_para_humano", {"motivo": "cliente pediu atendimento humano"})
            return "(modo simulado) Transferi você para a equipe, que atende de segunda a sábado, das 8h às 18h.", passos
        num = re.search(r"\b(\d{4})\b", m)
        if num and "pedido" in m and not any(w in m for w in ("troca", "defeito", "reclam", "vencid", "estragad")):
            r = usar("consultar_pedido", {"numero": num.group(1)})
            if "erro" in r:
                return "(modo simulado) " + r["erro"], passos
            return f"(modo simulado) Seu pedido {r['numero']} está: {r['status']}. Previsão: {r['previsao']}.", passos
        if any(w in m for w in ("troca", "trocar", "defeito", "vencid", "reclam", "estragad")):
            r = usar("abrir_chamado", {"tipo": "troca" if "troc" in m else "reclamacao", "descricao": mensagem, "pedido": num.group(1) if num else None})
            return f"(modo simulado) Registrei o chamado {r['chamado']}. Retornamos {r['prazo_resposta']}.", passos
        termo = re.sub(r"\b(voces|tem|vende|quanto|custa|o|a|os|as|de|do|da|ai|preco|qual)\b", " ", m)
        r = usar("buscar_produtos", {"termo": termo.strip(" ?!.")})
        if r["encontrados"]:
            linhas = [f"{p['nome']}: {p['preco']}" + ("" if p["disponivel"] else " (em falta)") for p in r["encontrados"]]
            return "(modo simulado) Encontrei: " + "; ".join(linhas), passos
        return "(modo simulado) Não encontrei esse produto. Quer tentar outro nome?", passos


def criar_modelo():
    chave = os.environ.get("GEMINI_API_KEY", "").strip()
    if chave:
        return Gemini(chave, os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite"))
    return Simulado()
