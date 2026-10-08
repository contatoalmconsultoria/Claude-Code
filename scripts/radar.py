#!/usr/bin/env python3
"""Radar Diário de Gestão — ALM Consultoria.

Motor determinístico da rotina diária. A coleta nas fontes (Agenda, Gmail, Drive,
Supabase) é feita pelo agente através dos conectores; este script cuida do que
precisa ser previsível e auditável:

  iniciar      trava a execução do dia (proteção contra duplicidade) e permite retomada
  checkpoint   registra a conclusão de uma etapa (mecanismo de retomada)
  validar      valida o banco de pendências
  aplicar      aplica um lote de alterações (insere novas, atualiza, conclui) sem apagar nada
  relatorio    gera o relatório (Markdown, HTML) e a prévia do e-mail
  enviar       verifica as autorizações de envio e prepara a chamada ao serviço de e-mail
  registrar-envio  registra o resultado devolvido pelo serviço de e-mail
  finalizar    encerra a execução e grava o resultado no log de execuções

Somente biblioteca padrão do Python 3.9+.
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import html
import json
import os
import shutil
import sys
import unicodedata
from pathlib import Path
from zoneinfo import ZoneInfo

RAIZ = Path(__file__).resolve().parent.parent
DB = RAIZ / "data" / "pendencias.json"
CONFIG = RAIZ / "config" / "radar.config.json"
EXECUCOES = RAIZ / "execucoes"
RELATORIOS = RAIZ / "relatorios"
LOG_EXEC = EXECUCOES / "execucoes.jsonl"
TZ = ZoneInfo("America/Sao_Paulo")

STATUS = ["Aberta", "Em andamento", "Aguardando cliente", "Aguardando equipe",
          "Aguardando direção", "Concluída", "Cancelada"]
FECHADOS = {"Concluída", "Cancelada"}
PRIORIDADES = ["P1", "P2", "P3", "P4"]
CATEGORIAS = ["direcao", "equipe_tecnica", "administrativa", "comercial", "cliente"]
OBRIGATORIOS = ["id", "cliente", "descricao", "responsavel", "categoria", "origem",
                "data_identificacao", "prioridade", "status", "ultima_movimentacao", "historico"]
ETAPAS = ["historico", "agenda", "atas", "emails", "clientes", "equipe", "prioridades",
          "relatorio", "email", "persistencia"]
NOME_PRIORIDADE = {"P1": "Crítica", "P2": "Alta", "P3": "Normal", "P4": "Acompanhamento"}


# ----------------------------------------------------------------- utilidades
def agora() -> dt.datetime:
    return dt.datetime.now(TZ)


def hoje() -> dt.date:
    return agora().date()


def data(s: str | None) -> dt.date | None:
    return dt.date.fromisoformat(s) if s else None


def br(d: dt.date | str | None) -> str:
    if isinstance(d, str):
        d = data(d)
    return d.strftime("%d/%m/%Y") if d else "—"


def ler_json(p: Path):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def gravar_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(p)  # escrita atômica: nunca deixa o banco pela metade


def config() -> dict:
    return ler_json(CONFIG) if CONFIG.exists() else {}


def normalizar(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return " ".join("".join(c for c in s if not unicodedata.combining(c)).split())


def erro(msg: str, codigo: int = 1):
    print(f"ERRO: {msg}", file=sys.stderr)
    sys.exit(codigo)


# ----------------------------------------------------- calendário de dias úteis
def pascoa(ano: int) -> dt.date:
    a, b, c = ano % 19, ano // 100, ano % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mes = (h + l - 7 * m + 114) // 31
    dia = ((h + l - 7 * m + 114) % 31) + 1
    return dt.date(ano, mes, dia)


def feriados(ano: int) -> dict[dt.date, str]:
    p = pascoa(ano)
    fer = {
        dt.date(ano, 1, 1): "Confraternização Universal",
        p - dt.timedelta(days=48): "Carnaval (ponto facultativo)",
        p - dt.timedelta(days=47): "Carnaval (ponto facultativo)",
        p - dt.timedelta(days=2): "Sexta-feira Santa",
        dt.date(ano, 4, 21): "Tiradentes",
        dt.date(ano, 5, 1): "Dia do Trabalho",
        p + dt.timedelta(days=60): "Corpus Christi (ponto facultativo)",
        dt.date(ano, 9, 7): "Independência",
        dt.date(ano, 10, 12): "Nossa Senhora Aparecida",
        dt.date(ano, 11, 2): "Finados",
        dt.date(ano, 11, 15): "Proclamação da República",
        dt.date(ano, 11, 20): "Dia da Consciência Negra",
        dt.date(ano, 12, 25): "Natal",
    }
    for extra in config().get("agendamento", {}).get("feriados_adicionais", []):
        d = data(extra["data"])
        if d.year == ano:
            fer[d] = extra["nome"]
    return fer


def dia_util(d: dt.date) -> bool:
    return d.weekday() < 5 and d not in feriados(d.year)


def dias_uteis_entre(inicio: dt.date, fim: dt.date) -> int:
    """Dias úteis após `inicio` até `fim` (inclusive). Negativo se `fim` já passou."""
    if fim <= inicio:
        n, d = 0, fim
        while d < inicio:
            d += dt.timedelta(days=1)
            if dia_util(d):
                n -= 1
        return n
    n, d = 0, inicio
    while d < fim:
        d += dt.timedelta(days=1)
        if dia_util(d):
            n += 1
    return n


# ------------------------------------------------------------ banco de dados
def carregar() -> dict:
    if not DB.exists():
        return {"versao_schema": 1, "atualizado_em": None, "pendencias": []}
    return ler_json(DB)


def validar_banco(banco: dict) -> list[str]:
    problemas, ids = [], set()
    for it in banco["pendencias"]:
        ref = it.get("id", "?")
        for campo in OBRIGATORIOS:
            if it.get(campo) in (None, "", []):
                problemas.append(f"{ref}: campo obrigatório ausente '{campo}'")
        if ref in ids:
            problemas.append(f"{ref}: ID duplicado")
        ids.add(ref)
        if it.get("status") not in STATUS:
            problemas.append(f"{ref}: status inválido '{it.get('status')}'")
        if it.get("prioridade") not in PRIORIDADES:
            problemas.append(f"{ref}: prioridade inválida '{it.get('prioridade')}'")
        if it.get("categoria") not in CATEGORIAS:
            problemas.append(f"{ref}: categoria inválida '{it.get('categoria')}'")
        if it.get("status") == "Concluída" and not it.get("evidencia_conclusao"):
            problemas.append(f"{ref}: concluída sem evidência de conclusão")
        if not it.get("prazo") and not it.get("prazo_texto"):
            problemas.append(f"{ref}: sem prazo e sem 'prazo_texto' (use 'Sem prazo informado')")
        for campo in ("prazo", "data_identificacao", "ultima_movimentacao"):
            try:
                data(it.get(campo))
            except ValueError:
                problemas.append(f"{ref}: data inválida em '{campo}'")
    return problemas


def proximo_id(banco: dict) -> str:
    nums = [int(i["id"].split("-")[1]) for i in banco["pendencias"]]
    return f"ALM-{(max(nums) if nums else 0) + 1:04d}"


def duplicata(banco: dict, cliente: str, descricao: str, limiar: float = 0.82):
    alvo = normalizar(descricao)
    for it in banco["pendencias"]:
        if normalizar(it["cliente"]) != normalizar(cliente):
            continue
        if difflib.SequenceMatcher(None, alvo, normalizar(it["descricao"])).ratio() >= limiar:
            return it
    return None


def aplicar_lote(banco: dict, lote: dict, data_exec: str) -> list[str]:
    """Aplica operações sem nunca remover registros. Retorna o log de alterações."""
    por_id = {i["id"]: i for i in banco["pendencias"]}
    log = []
    for op in lote.get("operacoes", []):
        tipo = op.get("op")
        fonte = op.get("fonte") or erro(f"operação sem 'fonte': {op}")
        if tipo == "nova":
            campos = op["campos"]
            dup = duplicata(banco, campos["cliente"], campos["descricao"])
            if dup:
                log.append(f"IGNORADA (duplicata de {dup['id']}): {campos['descricao']}")
                continue
            novo = {
                "id": proximo_id(banco), "prazo": None, "prazo_texto": None, "fase": None,
                "precisa_confirmacao": False, "evidencia_conclusao": None,
                "data_identificacao": data_exec, "ultima_movimentacao": data_exec,
                **campos, "origem": campos.get("origem", fonte),
            }
            if not novo["prazo"] and not novo["prazo_texto"]:
                novo["prazo_texto"] = "Sem prazo informado"
            novo["historico"] = [{"data": data_exec, "evento": "identificada", "fonte": fonte,
                                  "detalhe": op.get("detalhe", "Nova pendência")}]
            banco["pendencias"].append(novo)
            por_id[novo["id"]] = novo
            log.append(f"NOVA {novo['id']}: {novo['descricao']}")
        elif tipo in ("atualizar", "concluir", "cancelar"):
            it = por_id.get(op.get("id")) or erro(f"ID inexistente: {op.get('id')}")
            campos = dict(op.get("campos", {}))
            if tipo == "concluir":
                if not op.get("evidencia"):
                    erro(f"{it['id']}: conclusão exige 'evidencia' explícita")
                campos.update(status="Concluída", evidencia_conclusao=op["evidencia"])
            if tipo == "cancelar":
                if not op.get("motivo"):
                    erro(f"{it['id']}: cancelamento exige 'motivo'")
                campos.update(status="Cancelada")
            for proibido in ("id", "historico"):
                campos.pop(proibido, None)
            mudancas = {k: (it.get(k), v) for k, v in campos.items() if it.get(k) != v}
            if not mudancas and tipo == "atualizar" and not op.get("detalhe"):
                continue
            it.update(campos)
            it["ultima_movimentacao"] = op.get("data_movimentacao", data_exec)
            it["historico"].append({
                "data": data_exec,
                "evento": {"atualizar": "atualizada", "concluir": "concluída", "cancelar": "cancelada"}[tipo],
                "fonte": fonte,
                "detalhe": op.get("detalhe") or op.get("motivo") or op.get("evidencia") or "",
                "alteracoes": {k: {"de": a, "para": b} for k, (a, b) in mudancas.items()},
            })
            log.append(f"{tipo.upper()} {it['id']}: " + ", ".join(f"{k}: {a} → {b}" for k, (a, b) in mudancas.items()))
        else:
            erro(f"operação desconhecida: {tipo}")
    banco["atualizado_em"] = data_exec
    return log


# -------------------------------------------------- execução, trava e retomada
def estado_path(d: str) -> Path:
    return EXECUCOES / d / "estado.json"


def cmd_iniciar(a):
    d = a.data or hoje().isoformat()
    dia = data(d)
    if not dia_util(dia) and not a.forcar:
        print(json.dumps({"acao": "pular", "motivo": f"{br(dia)} não é dia útil"}, ensure_ascii=False))
        return
    p = estado_path(d)
    if p.exists():
        est = ler_json(p)
        if est["status"] == "concluida" and not a.forcar:
            print(json.dumps({"acao": "pular", "motivo": "execução do dia já concluída",
                              "concluida_em": est.get("fim")}, ensure_ascii=False))
            return
        if est["status"] == "em_andamento":
            iniciado = dt.datetime.fromisoformat(est["inicio"])
            idade_min = (agora() - iniciado).total_seconds() / 60
            limite = config().get("execucao", {}).get("trava_expira_minutos", 120)
            if idade_min < limite and not a.forcar:
                print(json.dumps({"acao": "abortar", "motivo": "outra execução em andamento",
                                  "iniciada_em": est["inicio"]}, ensure_ascii=False))
                sys.exit(3)
            pendentes = [e for e in ETAPAS if e not in est["etapas_concluidas"]]
            est.setdefault("retomadas", []).append(agora().isoformat())
            est["inicio"] = agora().isoformat()
            gravar_json(p, est)
            print(json.dumps({"acao": "retomar", "etapas_concluidas": est["etapas_concluidas"],
                              "etapas_pendentes": pendentes}, ensure_ascii=False))
            return
    modo = a.modo or config().get("execucao", {}).get("modo", "simulacao")
    est = {"data": d, "modo": modo, "status": "em_andamento", "inicio": agora().isoformat(),
           "etapas_concluidas": [], "erros": [], "envio": None}
    gravar_json(p, est)
    if DB.exists():  # cópia do banco antes de qualquer alteração (auditoria)
        shutil.copy(DB, p.parent / "pendencias_antes.json")
    print(json.dumps({"acao": "iniciar", "modo": modo, "etapas": ETAPAS}, ensure_ascii=False))


def cmd_checkpoint(a):
    p = estado_path(a.data)
    est = ler_json(p)
    if a.etapa not in ETAPAS:
        erro(f"etapa desconhecida: {a.etapa}")
    if a.erro:
        est["erros"].append({"etapa": a.etapa, "quando": agora().isoformat(), "erro": a.erro})
    elif a.etapa not in est["etapas_concluidas"]:
        est["etapas_concluidas"].append(a.etapa)
    gravar_json(p, est)
    print(f"checkpoint {a.etapa}: {'erro registrado' if a.erro else 'ok'}")


def cmd_finalizar(a):
    p = estado_path(a.data)
    est = ler_json(p)
    est["status"] = a.status
    est["fim"] = agora().isoformat()
    if a.observacao:
        est["observacao"] = a.observacao
    gravar_json(p, est)
    LOG_EXEC.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_EXEC, "a", encoding="utf-8") as f:
        f.write(json.dumps({k: est.get(k) for k in ("data", "modo", "status", "inicio", "fim",
                            "etapas_concluidas", "erros", "envio", "observacao")},
                           ensure_ascii=False) + "\n")
    print(f"execução {a.data} finalizada: {a.status}")


def ultimo_relatorio_antes(d: str) -> str | None:
    if not LOG_EXEC.exists():
        return None
    datas = []
    for linha in LOG_EXEC.read_text(encoding="utf-8").splitlines():
        reg = json.loads(linha)
        if reg["status"] == "concluida" and reg["data"] < d:
            datas.append(reg["data"])
    return max(datas) if datas else None


# ------------------------------------------------------------- comandos de DB
def cmd_validar(a):
    probs = validar_banco(carregar())
    if probs:
        print("\n".join(probs))
        sys.exit(2)
    print(f"banco válido: {len(carregar()['pendencias'])} registros")


def cmd_aplicar(a):
    banco = carregar()
    lote = ler_json(Path(a.lote))
    log = aplicar_lote(banco, lote, a.data)
    probs = validar_banco(banco)
    if probs:
        erro("o lote deixaria o banco inválido; nada foi gravado:\n" + "\n".join(probs))
    if not a.simular:
        gravar_json(DB, banco)
        dest = EXECUCOES / a.data / "alteracoes.log"
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "a", encoding="utf-8") as f:
            f.write("\n".join(log) + "\n")
    print("\n".join(log) or "nenhuma alteração")


# ------------------------------------------------------------------ relatório
def abertas(banco):
    return [i for i in banco["pendencias"] if i["status"] not in FECHADOS]


def vencida(it, ref: dt.date) -> bool:
    return bool(it.get("prazo")) and data(it["prazo"]) < ref and it["status"] not in FECHADOS


def data_conclusao(it) -> str | None:
    for h in reversed(it["historico"]):
        if h["evento"] == "concluída":
            return h["data"]
    return None


def chave_ordem(it):
    return (it["prioridade"], it.get("prazo") or "9999-12-31", it["cliente"])


def prazo_fmt(it, ref: dt.date) -> str:
    if it.get("prazo"):
        txt = br(it["prazo"])
        if vencida(it, ref):
            txt += f" (vencida há {(ref - data(it['prazo'])).days} d)"
        elif data(it["prazo"]) == ref:
            txt += " (hoje)"
        return txt + (f" — {it['prazo_texto']}" if it.get("prazo_texto") else "")
    return it.get("prazo_texto") or "Sem prazo informado"


def e_da_direcao(it) -> bool:
    return (it["categoria"] in ("direcao", "comercial", "administrativa")
            or (it["categoria"] != "cliente" and it["status"] == "Aguardando direção")
            or (it["categoria"] != "cliente" and normalizar(it["responsavel"]).startswith("liliane")))


def sem_responsavel(it) -> bool:
    r = normalizar(it["responsavel"])
    return any(t in r for t in ("a confirmar", "nao identificado", "a definir")) and it["categoria"] != "cliente"


def responsavel_equipe(it) -> str:
    r = it["responsavel"]
    for sep in (" / ", " (", ","):
        r = r.split(sep)[0]
    return r.strip()


def montar(banco: dict, ctx: dict, d: str) -> dict:
    ref = data(d)
    ab = abertas(banco)
    desde = ctx.get("concluidas_desde") or ultimo_relatorio_antes(d) or d
    concl = [i for i in banco["pendencias"] if i["status"] == "Concluída"
             and (data_conclusao(i) or "") >= desde]
    sem_mov = config().get("relatorio", {}).get("dias_sem_movimentacao", 14)
    return {
        "ref": ref, "ab": ab, "concl": concl, "desde": desde,
        "venc": [i for i in ab if vencida(i, ref)],
        "p1": [i for i in ab if i["prioridade"] == "P1"],
        "direcao": sorted([i for i in ab if e_da_direcao(i)], key=chave_ordem),
        "equipe": sorted([i for i in ab if i["categoria"] == "equipe_tecnica"], key=chave_ordem),
        "clientes": sorted([i for i in ab if i["categoria"] == "cliente"], key=lambda i: (i["cliente"],) + chave_ordem(i)),
        "sem_resp": [i for i in ab if sem_responsavel(i)],
        "parados": [i for i in ab if (ref - data(i["ultima_movimentacao"])).days > sem_mov],
        "confirmar": [i for i in ab if i.get("precisa_confirmacao")],
        "ctx": ctx,
    }


def linha_item(it, ref, cliente=True) -> str:
    partes = [f"**[{it['prioridade']}] {it['id']}**"]
    if cliente:
        partes.append(f"{it['cliente']}:")
    partes.append(it["descricao"])
    meta = f"Resp.: {it['responsavel']} · Prazo: {prazo_fmt(it, ref)} · Status: {it['status']} · Fonte: {it['origem']}"
    if it.get("precisa_confirmacao"):
        meta += " · ⚠️ confirmar"
    return "- " + " ".join(partes) + f"  \n  {meta}"


def gerar_markdown(m: dict) -> str:
    ref, ctx = m["ref"], m["ctx"]
    L = []
    titulo = f"GESTÃO ALM | Radar Diário | {br(ref)}"
    L.append(f"# {titulo}")
    if ctx.get("modo") == "simulacao":
        L.append("> **SIMULAÇÃO.** Nenhum e-mail foi enviado e nenhuma fonte externa foi alterada.")
    if ctx.get("nota_execucao"):
        L.append(f"> {ctx['nota_execucao']}")
    # 1
    L.append("\n## 1. Resumo executivo\n")
    L.append("| Indicador | Qtde |\n|---|---:|")
    L.append(f"| Pendências abertas | {len(m['ab'])} |")
    L.append(f"| Pendências vencidas | {len(m['venc'])} |")
    L.append(f"| Pendências críticas (P1) | {len(m['p1'])} |")
    L.append(f"| Concluídas desde {br(m['desde'])} | {len(m['concl'])} |")
    L.append(f"| Precisam de confirmação (⚠️) | {len(m['confirmar'])} |")
    atencao = [c for c in ctx.get("clientes", []) if c["situacao"] in ("Crítico", "Atenção")]
    if atencao:
        L.append("\n**Clientes que exigem atenção:** " + "; ".join(
            f"{c['cliente']} ({c['situacao']})" for c in sorted(atencao, key=lambda c: c["situacao"] != "Crítico")))
    L.append("\n**Três prioridades do dia**\n")
    for n, p in enumerate(ctx.get("prioridades_dia", [])[:3], 1):
        L.append(f"{n}. {p}")
    # 2
    L.append("\n## 2. Minhas prioridades (Liliane)\n")
    for it in m["direcao"]:
        L.append(linha_item(it, ref))
    # 3
    L.append("\n## 3. Reuniões de hoje\n")
    if ctx.get("reunioes_hoje"):
        L.append("| Horário | Cliente | Participantes | Pauta | Preparação | Pendências anteriores | Situação |\n|---|---|---|---|---|---|---|")
        for r in ctx["reunioes_hoje"]:
            L.append(f"| {r['horario']} | {r['cliente']} | {r.get('participantes','—')} | {r.get('pauta','—')} | "
                     f"{r.get('preparacao','—')} | {', '.join(r.get('pendencias_anteriores', [])) or '—'} | {r.get('situacao','—')} |")
    else:
        L.append("Nenhuma reunião na agenda de hoje.")
    if ctx.get("agenda_7_dias"):
        L.append("\n**Próximos 7 dias**\n")
        for r in ctx["agenda_7_dias"]:
            L.append(f"- {r['quando']} · {r['titulo']}" + (f" — {r['observacao']}" if r.get("observacao") else ""))
    # 4
    L.append("\n## 4. Pendências da equipe\n")
    grupos: dict[str, list] = {}
    for it in m["equipe"]:
        grupos.setdefault(responsavel_equipe(it), []).append(it)
    for resp in sorted(grupos):
        L.append(f"\n### {resp}\n")
        for it in grupos[resp]:
            L.append(linha_item(it, ref))
    if m["sem_resp"]:
        L.append("\n**Demandas sem responsável definido:** " + ", ".join(i["id"] for i in m["sem_resp"]))
    # 5
    L.append("\n## 5. Pendências dos clientes\n")
    atual = None
    for it in m["clientes"]:
        if it["cliente"] != atual:
            atual = it["cliente"]
            L.append(f"\n### {atual}\n")
        L.append(linha_item(it, ref, cliente=False))
    # 6
    L.append("\n## 6. Alertas gerenciais\n")
    for al in ctx.get("alertas", []):
        marca = " **[decisão]**" if al.get("decisao_necessaria") else ""
        L.append(f"- **{al['tipo']}**{marca}: {al['descricao']}")
    if m["venc"]:
        L.append("- **Vencidas:** " + ", ".join(f"{i['id']} ({i['cliente']})" for i in sorted(m["venc"], key=chave_ordem)))
    if m["parados"]:
        L.append("- **Sem movimentação há mais de "
                 f"{config().get('relatorio', {}).get('dias_sem_movimentacao', 14)} dias:** "
                 + ", ".join(f"{i['id']} ({i['cliente']})" for i in m["parados"]))
    if ctx.get("clientes"):
        L.append("\n**Situação de acompanhamento por cliente**\n")
        L.append("| Cliente | Última reunião | Próxima reunião | Situação | Motivo |\n|---|---|---|---|---|")
        for c in ctx["clientes"]:
            L.append(f"| {c['cliente']} | {c.get('ultima_reuniao','—')} | {c.get('proxima_reuniao','—')} | {c['situacao']} | {c.get('motivo','')} |")
    # 7
    L.append("\n## 7. Concluído desde o último relatório\n")
    if m["concl"]:
        for it in m["concl"]:
            L.append(f"- **{it['id']}** {it['cliente']}: {it['descricao']} — concluída em {br(data_conclusao(it))}. "
                     f"Evidência: {it['evidencia_conclusao']}")
    else:
        L.append("Nenhuma conclusão comprovada no período.")
    for extra in ctx.get("concluidas_fora_do_banco", []):
        L.append(f"- {extra}")
    # 8
    L.append("\n## 8. Lacunas de informação\n")
    if ctx.get("integracoes"):
        L.append("| Fonte | Situação | Observação |\n|---|---|---|")
        for i in ctx["integracoes"]:
            L.append(f"| {i['nome']} | {i['status']} | {i.get('observacao','')} |")
        L.append("")
    for g in ctx.get("lacunas", []):
        L.append(f"- {g}")
    if m["confirmar"]:
        L.append("- Pendências que precisam de confirmação antes de serem encerradas ou cobradas: "
                 + ", ".join(i["id"] for i in m["confirmar"]))
    L.append(f"\n---\n*Gerado em {agora().strftime('%d/%m/%Y %H:%M')} (America/Sao_Paulo) "
             "pelo Assistente Executivo de Gestão.*")
    return "\n".join(L)


# ---- HTML (relatório detalhado e corpo do e-mail), sem dependências externas
CSS = """
body{margin:0;background:#f4f5f7;color:#1f2933;font:15px/1.5 -apple-system,Segoe UI,Roboto,Arial,sans-serif}
.wrap{max-width:960px;margin:0 auto;padding:24px 16px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 10px;border-bottom:2px solid #0f4c81;padding-bottom:4px;color:#0f4c81}
h3{font-size:15px;margin:16px 0 6px}
.aviso{background:#fff4e5;border-left:4px solid #e8a33d;padding:8px 12px;margin:12px 0}
.kpis{display:flex;flex-wrap:wrap;gap:10px}.kpi{background:#fff;border:1px solid #dde2e8;border-radius:8px;padding:10px 14px;min-width:120px}
.kpi b{display:block;font-size:22px}.kpi span{font-size:12px;color:#52606d}
table{border-collapse:collapse;width:100%;background:#fff;font-size:13px;margin:6px 0}
th,td{border:1px solid #dde2e8;padding:6px 8px;text-align:left;vertical-align:top}th{background:#eef2f6}
.item{background:#fff;border:1px solid #dde2e8;border-radius:6px;padding:8px 10px;margin:6px 0}
.meta{font-size:12px;color:#52606d}
.tag{display:inline-block;font-size:11px;font-weight:700;border-radius:4px;padding:1px 6px;margin-right:6px;color:#fff}
.P1{background:#c0392b}.P2{background:#d35400}.P3{background:#2c6e9b}.P4{background:#7b8794}
.venc{color:#c0392b;font-weight:600}.conf{color:#b7791f}
.Crítico{color:#c0392b;font-weight:700}.Atenção{color:#b7791f;font-weight:700}.Em\\ dia{color:#2f855a;font-weight:700}
@media (max-width:600px){table{display:block;overflow-x:auto}}
"""


def esc(s) -> str:
    return html.escape(str(s if s is not None else "—"))


def item_html(it, ref, cliente=True) -> str:
    pz = prazo_fmt(it, ref)
    pz = f'<span class="venc">{esc(pz)}</span>' if vencida(it, ref) else esc(pz)
    conf = ' · <span class="conf">⚠️ confirmar</span>' if it.get("precisa_confirmacao") else ""
    cli = f"<b>{esc(it['cliente'])}:</b> " if cliente else ""
    return (f'<div class="item"><span class="tag {it["prioridade"]}">{it["prioridade"]}</span>'
            f'<b>{esc(it["id"])}</b> {cli}{esc(it["descricao"])}<div class="meta">Resp.: {esc(it["responsavel"])} · '
            f'Prazo: {pz} · Status: {esc(it["status"])} · Fonte: {esc(it["origem"])}{conf}</div></div>')


def gerar_html(m: dict, completo: bool = True) -> str:
    ref, ctx = m["ref"], m["ctx"]
    H = []
    titulo = f"GESTÃO ALM | Radar Diário | {br(ref)}"
    H.append(f'<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">'
             f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(titulo)}</title>'
             f'<style>{CSS}</style></head><body><div class="wrap"><h1>{esc(titulo)}</h1>')
    if ctx.get("modo") == "simulacao":
        H.append('<div class="aviso"><b>SIMULAÇÃO.</b> Nenhum e-mail foi enviado e nenhuma fonte externa foi alterada.</div>')
    if ctx.get("nota_execucao"):
        H.append(f'<div class="aviso">{esc(ctx["nota_execucao"])}</div>')
    H.append("<h2>1. Resumo executivo</h2><div class='kpis'>")
    for rot, v in (("Abertas", len(m["ab"])), ("Vencidas", len(m["venc"])), ("Críticas (P1)", len(m["p1"])),
                   (f"Concluídas desde {br(m['desde'])}", len(m["concl"])), ("A confirmar", len(m["confirmar"]))):
        H.append(f"<div class='kpi'><b>{v}</b><span>{esc(rot)}</span></div>")
    H.append("</div>")
    at = [c for c in ctx.get("clientes", []) if c["situacao"] in ("Crítico", "Atenção")]
    if at:
        H.append("<p><b>Clientes que exigem atenção:</b> " + "; ".join(
            f"{esc(c['cliente'])} (<span class='{esc(c['situacao'])}'>{esc(c['situacao'])}</span>)"
            for c in sorted(at, key=lambda c: c["situacao"] != "Crítico")) + "</p>")
    H.append("<p><b>Três prioridades do dia</b></p><ol>" +
             "".join(f"<li>{esc(p)}</li>" for p in ctx.get("prioridades_dia", [])[:3]) + "</ol>")

    lim = None if completo else config().get("email", {}).get("max_prioridades_corpo", 5)
    H.append(f"<h2>2. Minhas prioridades{'' if completo else ' (cinco principais)'}</h2>")
    H.extend(item_html(i, ref) for i in m["direcao"][:lim])

    H.append("<h2>3. Reuniões de hoje</h2>")
    if ctx.get("reunioes_hoje"):
        H.append("<table><tr><th>Horário</th><th>Cliente</th><th>Participantes</th><th>Pauta</th><th>Preparação</th>"
                 "<th>Pend. anteriores</th><th>Situação</th></tr>")
        for r in ctx["reunioes_hoje"]:
            H.append("<tr>" + "".join(f"<td>{esc(x)}</td>" for x in (
                r["horario"], r["cliente"], r.get("participantes"), r.get("pauta"), r.get("preparacao"),
                ", ".join(r.get("pendencias_anteriores", [])) or "—", r.get("situacao"))) + "</tr>")
        H.append("</table>")
    else:
        H.append("<p>Nenhuma reunião na agenda de hoje.</p>")
    if completo and ctx.get("agenda_7_dias"):
        H.append("<h3>Próximos 7 dias</h3><ul>" + "".join(
            f"<li>{esc(r['quando'])} · {esc(r['titulo'])}{(' — ' + esc(r['observacao'])) if r.get('observacao') else ''}</li>"
            for r in ctx["agenda_7_dias"]) + "</ul>")

    equipe = m["equipe"] if completo else [i for i in m["equipe"] if i["prioridade"] in ("P1", "P2") or vencida(i, ref)]
    H.append("<h2>4. Pendências da equipe" + ("" if completo else " (críticas, altas e vencidas)") + "</h2>")
    grupos: dict[str, list] = {}
    for it in equipe:
        grupos.setdefault(responsavel_equipe(it), []).append(it)
    for resp in sorted(grupos):
        H.append(f"<h3>{esc(resp)}</h3>" + "".join(item_html(i, ref) for i in grupos[resp]))
    if not grupos:
        H.append("<p>Nenhuma pendência da equipe nessa faixa.</p>")
    if completo and m["sem_resp"]:
        H.append("<p><b>Sem responsável definido:</b> " + ", ".join(esc(i["id"]) for i in m["sem_resp"]) + "</p>")

    if completo:
        H.append("<h2>5. Pendências dos clientes</h2>")
        atual = None
        for it in m["clientes"]:
            if it["cliente"] != atual:
                atual = it["cliente"]
                H.append(f"<h3>{esc(atual)}</h3>")
            H.append(item_html(it, ref, cliente=False))

    H.append(f"<h2>{'6' if completo else '5'}. Alertas gerenciais" + ("" if completo else " que exigem decisão") + "</h2><ul>")
    for al in ctx.get("alertas", []):
        if completo or al.get("decisao_necessaria"):
            H.append(f"<li><b>{esc(al['tipo'])}</b>{' <b>[decisão]</b>' if al.get('decisao_necessaria') else ''}: {esc(al['descricao'])}</li>")
    H.append("</ul>")
    if completo and ctx.get("clientes"):
        H.append("<table><tr><th>Cliente</th><th>Última reunião</th><th>Próxima reunião</th><th>Situação</th><th>Motivo</th></tr>")
        for c in ctx["clientes"]:
            H.append(f"<tr><td>{esc(c['cliente'])}</td><td>{esc(c.get('ultima_reuniao'))}</td><td>{esc(c.get('proxima_reuniao'))}</td>"
                     f"<td class='{esc(c['situacao'])}'>{esc(c['situacao'])}</td><td>{esc(c.get('motivo'))}</td></tr>")
        H.append("</table>")

    if completo:
        H.append("<h2>7. Concluído desde o último relatório</h2><ul>")
        for it in m["concl"]:
            H.append(f"<li><b>{esc(it['id'])}</b> {esc(it['cliente'])}: {esc(it['descricao'])}. Concluída em "
                     f"{br(data_conclusao(it))}. Evidência: {esc(it['evidencia_conclusao'])}</li>")
        for extra in ctx.get("concluidas_fora_do_banco", []):
            H.append(f"<li>{esc(extra)}</li>")
        if not m["concl"] and not ctx.get("concluidas_fora_do_banco"):
            H.append("<li>Nenhuma conclusão comprovada no período.</li>")
        H.append("</ul><h2>8. Lacunas de informação</h2>")
        if ctx.get("integracoes"):
            H.append("<table><tr><th>Fonte</th><th>Situação</th><th>Observação</th></tr>" + "".join(
                f"<tr><td>{esc(i['nome'])}</td><td>{esc(i['status'])}</td><td>{esc(i.get('observacao'))}</td></tr>"
                for i in ctx["integracoes"]) + "</table>")
        H.append("<ul>" + "".join(f"<li>{esc(g)}</li>" for g in ctx.get("lacunas", [])) + "</ul>")
    else:
        H.append("<p>O relatório completo segue em anexo (HTML).</p>")
    H.append(f"<p class='meta'>Gerado em {agora().strftime('%d/%m/%Y %H:%M')} (America/Sao_Paulo) pelo Assistente Executivo de Gestão.</p>")
    H.append("</div></body></html>")
    return "".join(H)


def cmd_relatorio(a):
    banco = carregar()
    probs = validar_banco(banco)
    if probs:
        erro("banco inválido:\n" + "\n".join(probs))
    ctx = ler_json(Path(a.contexto))
    m = montar(banco, ctx, a.data)
    saida = RELATORIOS / a.data
    saida.mkdir(parents=True, exist_ok=True)
    md = gerar_markdown(m)
    (saida / "radar.md").write_text(md, encoding="utf-8")
    (saida / "radar.html").write_text(gerar_html(m, completo=True), encoding="utf-8")
    (saida / "email.html").write_text(gerar_html(m, completo=False), encoding="utf-8")
    assunto = f"ALM | Radar Diário de Gestão | {br(m['ref'])}"
    gravar_json(saida / "email.json", {"assunto": assunto, "corpo_html": "email.html", "anexo": "radar.html"})
    print(json.dumps({"saida": str(saida.relative_to(RAIZ)), "assunto": assunto, "abertas": len(m["ab"]),
                      "vencidas": len(m["venc"]), "criticas": len(m["p1"]), "concluidas": len(m["concl"])},
                     ensure_ascii=False))


# ------------------------------------------------------------------ envio
def cmd_enviar(a):
    """Não envia nada: verifica autorizações e devolve a instrução para o conector de e-mail."""
    cfg = config().get("email", {})
    est_p = estado_path(a.data)
    modo = ler_json(est_p)["modo"] if est_p.exists() else "simulacao"
    bloqueios = []
    if modo != "producao":
        bloqueios.append(f"modo de execução é '{modo}' (envio só em 'producao')")
    if not cfg.get("envio_autorizado"):
        bloqueios.append("config email.envio_autorizado = false (falta autorização explícita da direção)")
    if not cfg.get("destinatario"):
        bloqueios.append("config email.destinatario não configurado")
    if os.environ.get("RADAR_ENVIO_AUTORIZADO") != "1":
        bloqueios.append("variável de ambiente RADAR_ENVIO_AUTORIZADO != 1")
    meta = ler_json(RELATORIOS / a.data / "email.json")
    if bloqueios:
        print(json.dumps({"acao": "nao_enviar", "bloqueios": bloqueios, "assunto": meta["assunto"]}, ensure_ascii=False))
        return
    print(json.dumps({"acao": "enviar", "conector": cfg.get("servico", "gmail"),
                      "modo_envio": cfg.get("modo_envio", "rascunho"), "para": cfg["destinatario"],
                      "assunto": meta["assunto"], "corpo_html": f"relatorios/{a.data}/email.html",
                      "anexo": f"relatorios/{a.data}/radar.html"}, ensure_ascii=False))


def cmd_registrar_envio(a):
    p = estado_path(a.data)
    est = ler_json(p)
    est["envio"] = {"status": a.status, "quando": agora().isoformat(), "id_servico": a.id_servico, "erro": a.erro}
    gravar_json(p, est)
    print(f"envio registrado: {a.status}")


# ------------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("iniciar"); s.add_argument("--data"); s.add_argument("--modo", choices=["simulacao", "producao"]); s.add_argument("--forcar", action="store_true"); s.set_defaults(f=cmd_iniciar)
    s = sub.add_parser("checkpoint"); s.add_argument("--data", required=True); s.add_argument("--etapa", required=True); s.add_argument("--erro"); s.set_defaults(f=cmd_checkpoint)
    s = sub.add_parser("finalizar"); s.add_argument("--data", required=True); s.add_argument("--status", required=True, choices=["concluida", "falhou", "parcial"]); s.add_argument("--observacao"); s.set_defaults(f=cmd_finalizar)
    s = sub.add_parser("validar"); s.set_defaults(f=cmd_validar)
    s = sub.add_parser("aplicar"); s.add_argument("--data", required=True); s.add_argument("--lote", required=True); s.add_argument("--simular", action="store_true"); s.set_defaults(f=cmd_aplicar)
    s = sub.add_parser("relatorio"); s.add_argument("--data", required=True); s.add_argument("--contexto", required=True); s.set_defaults(f=cmd_relatorio)
    s = sub.add_parser("enviar"); s.add_argument("--data", required=True); s.set_defaults(f=cmd_enviar)
    s = sub.add_parser("registrar-envio"); s.add_argument("--data", required=True); s.add_argument("--status", required=True, choices=["enviado", "rascunho_criado", "falhou"]); s.add_argument("--id-servico"); s.add_argument("--erro"); s.set_defaults(f=cmd_registrar_envio)
    s = sub.add_parser("dia-util"); s.add_argument("--data"); s.set_defaults(f=lambda a: print(json.dumps({"data": a.data or hoje().isoformat(), "dia_util": dia_util(data(a.data) if a.data else hoje()), "feriado": feriados((data(a.data) if a.data else hoje()).year).get(data(a.data) if a.data else hoje())}, ensure_ascii=False)))
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
