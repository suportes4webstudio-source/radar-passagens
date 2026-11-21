#!/usr/bin/env python3
"""Atualiza ofertas.json com os preços publicados pela Azul.

Lê o bloco __NEXT_DATA__ das páginas de rota da Azul (calendário diário dos
próximos ~30 dias e ofertas com data), junta com o ofertas.json existente
(preservando ofertas ainda válidas de outras fontes) e grava o resultado.
"""
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

ARQ = Path(__file__).resolve().parent.parent / "ofertas.json"
ROTAS = {
    "SP": "https://passagens.voeazul.com.br/pt/voos-de-s%C3%A3o-paulo-para-belo-horizonte",
    "POA": "https://passagens.voeazul.com.br/pt/voos-de-porto-alegre-para-belo-horizonte",
}
URL_EXIBIR = {
    "SP": "https://passagens.voeazul.com.br/pt/voos-de-são-paulo-para-belo-horizonte",
    "POA": "https://passagens.voeazul.com.br/pt/voos-de-porto-alegre-para-belo-horizonte",
}
ORIGENS_OK = {"SP": {"GRU", "CGH", "VCP", "SAO"}, "POA": {"POA"}}
DESTINOS_OK = {"CNF", "BHZ", "PLU"}
MAX_OFERTAS_ROTA = 14
MAX_HISTORICO = 168
PRECO_MINIMO = 120  # descarta valores suspeitos
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

BRT = timezone(timedelta(hours=-3))


def hoje():
    return datetime.now(BRT).date().isoformat()


def baixar(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "pt-BR,pt;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def extrair_next_data(html):
    m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise ValueError("__NEXT_DATA__ não encontrado")
    return json.loads(m.group(1))


def parse_rota(rota, nd):
    data = nd["props"]["pageProps"]["apolloState"]["data"]
    dia = hoje()
    calendario, ofertas = {}, []

    for chave, valor in data.items():
        if not isinstance(valor, dict):
            continue
        # Calendário diário
        if chave.startswith("CmpConfig") and "fare-daily-histogram" in chave:
            estado = (valor.get("cmpConfig") or {}).get("settings", {}).get("initialState", {})
            for d in estado.get("dailyHistogram") or []:
                try:
                    ps = d.get("priceSpecification") or {}
                    preco = ps.get("totalPrice")
                    if ps.get("currencyCode") != "BRL" or not preco:
                        continue
                    if d.get("journeyType") not in (None, "ONE_WAY"):
                        continue
                    data_ida = d.get("departureDate")
                    of = d.get("outboundFlight") or {}
                    if not data_ida or data_ida < dia or d.get("isPastDay"):
                        continue
                    item = {
                        "data": data_ida,
                        "origem": of.get("departureAirportIataCode") or "",
                        "destino": of.get("arrivalAirportIataCode") or "CNF",
                        "preco": round(preco),
                        "cia": "Azul", "fonte": "Azul", "url": URL_EXIBIR[rota],
                    }
                    if item["preco"] < PRECO_MINIMO:
                        continue
                    if data_ida not in calendario or item["preco"] < calendario[data_ida]["preco"]:
                        calendario[data_ida] = item
                except Exception:
                    continue
        # Ofertas com data
        if chave.startswith("StandardFareModule"):
            for f in valor.get("fares") or []:
                try:
                    if f.get("redemption"):  # pontos
                        continue
                    if f.get("currencyCode") != "BRL":
                        continue
                    preco = f.get("totalPrice")
                    ida = f.get("departureDate")
                    origem = f.get("originAirportCode") or ""
                    destino = f.get("destinationAirportCode") or ""
                    if not preco or not ida or ida < dia:
                        continue
                    if origem not in ORIGENS_OK[rota] or destino not in DESTINOS_OK:
                        continue
                    if round(preco) < PRECO_MINIMO:
                        continue
                    volta = f.get("returnDate") or None
                    ofertas.append({
                        "rota": rota, "cia": "Azul", "origem": origem, "destino": "CNF",
                        "ida": ida, "volta": volta,
                        "tipo": "idaevolta" if f.get("flightType") == "ROUND_TRIP" or volta else "ida",
                        "direto": False, "preco": round(preco),
                        "fonte": "Azul", "url": URL_EXIBIR[rota],
                    })
                except Exception:
                    continue

    return sorted(calendario.values(), key=lambda x: x["data"]), ofertas


def chave_oferta(o):
    return (o.get("rota"), o.get("cia"), o.get("origem"), o.get("ida"), o.get("volta"), o.get("tipo"))


def selecionar(ofertas, rota, dia):
    lim = (datetime.fromisoformat(dia) + timedelta(days=30)).date().isoformat()
    da_rota = [o for o in ofertas if o.get("rota") == rota]
    proximas = sorted([o for o in da_rota if o["ida"] <= lim], key=lambda o: (o["ida"], o["preco"]))
    depois = sorted([o for o in da_rota if o["ida"] > lim], key=lambda o: (o["preco"], o["ida"]))
    return (proximas + depois)[:MAX_OFERTAS_ROTA]


def minimo(valores):
    v = [x for x in valores if isinstance(x, (int, float))]
    return min(v) if v else None


def main():
    dia = hoje()
    try:
        atual = json.loads(ARQ.read_text(encoding="utf-8"))
    except Exception:
        atual = {}
    cal_antigo = atual.get("calendario") or {}
    ofertas_antigas = [o for o in (atual.get("ofertas") or [])
                       if o.get("rota") in ROTAS and (o.get("ida") or "") >= dia]

    calendario, novas, erros = {}, [], []
    for rota, url in ROTAS.items():
        try:
            cal, ofs = parse_rota(rota, extrair_next_data(baixar(url)))
            if not cal:
                raise ValueError("calendário vazio")
            calendario[rota] = cal
            novas.extend(ofs)
            print(f"{rota}: {len(cal)} dias no calendário, {len(ofs)} ofertas")
        except Exception as e:  # mantém o que já havia
            erros.append(f"{rota}: {e}")
            calendario[rota] = [c for c in (cal_antigo.get(rota) or []) if c.get("data", "") >= dia]
            print(f"AVISO {rota}: {e} (mantendo calendário anterior)", file=sys.stderr)

    if len(erros) == len(ROTAS):
        print("Nenhuma rota atualizada; ofertas.json não foi alterado.", file=sys.stderr)
        sys.exit(1)

    # Novas ofertas substituem as antigas com a mesma chave
    mapa = {chave_oferta(o): o for o in ofertas_antigas}
    for o in novas:
        mapa[chave_oferta(o)] = o
    todas = list(mapa.values())
    ofertas = []
    for rota in ROTAS:
        ofertas.extend(selecionar(todas, rota, dia))

    lim = (datetime.fromisoformat(dia) + timedelta(days=30)).date().isoformat()

    def min30(rota):
        cal = [c["preco"] for c in calendario.get(rota, []) if c["data"] <= lim]
        ida = [o["preco"] for o in ofertas if o["rota"] == rota and o["tipo"] == "ida" and o["ida"] <= lim]
        return minimo(cal + ida)

    def min_ida(rota):
        return minimo([o["preco"] for o in ofertas if o["rota"] == rota and o["tipo"] == "ida"]
                      + [c["preco"] for c in calendario.get(rota, [])])

    agora = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    historico = (atual.get("historico") or []) + [{
        "t": agora,
        "spMin30": min30("SP"), "poaMin30": min30("POA"),
        "spMinIda": min_ida("SP"), "poaMinIda": min_ida("POA"),
    }]

    novo = {
        "atualizadoEm": agora,
        "calendario": calendario,
        "ofertas": ofertas,
        "historico": historico[-MAX_HISTORICO:],
    }
    ARQ.write_text(json.dumps(novo, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"OK {agora} | SP 30d: {min30('SP')} | POA 30d: {min30('POA')}")


if __name__ == "__main__":
    main()
