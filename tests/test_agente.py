"""Testes no modo simulado (sem chave de API) e do laço com um Gemini falso."""
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import agent
from app.main import app

c = TestClient(app)


def conversa(msg, sessao=None):
    r = c.post("/api/chat", json={"sessao": sessao, "mensagem": msg})
    assert r.status_code == 200, r.text
    return r.json()


def test_busca_produto_e_falta_de_estoque():
    j = conversa("Vocês têm arroz integral?")
    assert j["passos"][0]["ferramenta"] == "buscar_produtos"
    assert "em falta" in j["resposta"]


def test_pedido():
    j = conversa("Qual o status do pedido 1042?")
    assert j["passos"][0]["resultado"]["status"] == "Saiu para entrega"


def test_pedido_inexistente():
    j = conversa("pedido 9999")
    assert "não encontrado" in j["resposta"].lower()


def test_chamado_aparece_na_lista():
    j = conversa("Chegou leite vencido no meu pedido 1035")
    assert j["passos"][0]["ferramenta"] == "abrir_chamado"
    lista = c.get("/api/chamados").json()
    assert lista[0]["pedido"] == "1035"


def test_transferencia_humano():
    j = conversa("Quero falar com uma pessoa")
    assert j["transferida"] is True


def test_limite_por_sessao(monkeypatch):
    import app.main as m
    monkeypatch.setattr(m, "LIMITE_SESSAO", 2)
    s = conversa("oi")["sessao"]
    conversa("arroz", s)
    assert "limite" in conversa("feijão", s)["resposta"]


def test_laco_gemini_executa_ferramenta_e_responde():
    """Simula duas rodadas do Gemini: pede a ferramenta, depois responde."""
    g = agent.Gemini.__new__(agent.Gemini)
    from google.genai import types
    g.types, g.modelo, g.config = types, "x", None
    rodadas = [
        SimpleNamespace(candidates=[SimpleNamespace(content=types.Content(role="model", parts=[types.Part.from_function_call(name="consultar_pedido", args={"numero": "1042"})]))],
                        function_calls=[types.FunctionCall(name="consultar_pedido", args={"numero": "1042"})], text=None),
        SimpleNamespace(candidates=[SimpleNamespace(content=types.Content(role="model", parts=[types.Part.from_text(text="Saiu para entrega!")]))],
                        function_calls=None, text="Saiu para entrega!"),
    ]
    vistos = []

    def gerar(model, contents, config):
        vistos.append(list(contents))
        return rodadas.pop(0)

    g.client = SimpleNamespace(models=SimpleNamespace(generate_content=gerar))
    s = agent.Sessao(id="t")
    texto, passos = g.responder(s, "cadê meu pedido 1042?")
    assert texto == "Saiu para entrega!"
    assert passos[0]["resultado"]["status"] == "Saiu para entrega"
    # a 2ª chamada levou a resposta da ferramenta com papel 'user'
    assert vistos[1][-1].role == "user" and vistos[1][-1].parts[0].function_response.name == "consultar_pedido"
    assert len(s.historico) == 4


def test_cors_libera_so_o_portfolio():
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    ok = c.options("/api/chat", headers={"Origin": "https://p-lucas-s.github.io", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"})
    assert ok.headers.get("access-control-allow-origin") == "https://p-lucas-s.github.io"
    outro = c.get("/api/health", headers={"Origin": "https://exemplo.com"})
    assert "access-control-allow-origin" not in outro.headers
