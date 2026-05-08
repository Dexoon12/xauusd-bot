import requests
from datetime import datetime, timezone, timedelta
from gnews import GNews
from dotenv import load_dotenv
import os

load_dotenv()

# ─── CONFIG ──────────────────────────────────────────────
NEWSAPI_KEY = "TU_NEWSAPI_KEY"  # lo dejamos por si sirve después

# ─── 1. CALENDARIO ECONÓMICO ─────────────────────────────
def obtener_calendario():
    eventos = []
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        url     = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        resp    = requests.get(url, headers=headers, timeout=10)

        if resp.status_code == 200:
            data = resp.json()
            hoy  = datetime.now(timezone.utc).date()

            for evento in data:
                try:
                    fecha_str = evento.get("date", "")
                    if not fecha_str:
                        continue
                    fecha   = datetime.fromisoformat(fecha_str).date()
                    impacto = evento.get("impact",  "").lower()
                    pais    = evento.get("country", "")

                    if fecha not in [hoy, hoy + timedelta(days=1)]:
                        continue
                    if impacto != "high" or pais != "USD":
                        continue

                    hora = datetime.fromisoformat(
                        evento["date"]
                    ).strftime("%H:%M UTC")

                    eventos.append({
                        "titulo":   evento.get("title",    "—"),
                        "hora":     hora,
                        "fecha":    str(fecha),
                        "previsto": evento.get("forecast", "—"),
                        "anterior": evento.get("previous", "—"),
                        "actual":   evento.get("actual",   "—"),
                        "es_hoy":   fecha == hoy,
                    })
                except Exception:
                    continue
        else:
            print(f"Forex Factory HTTP {resp.status_code}")

    except Exception as e:
        print(f"Error calendario: {e}")

    return eventos


def alerta_noticia_proxima(eventos, minutos=60, minutos_post=120):
    """
    Detecta eventos de alto impacto:
    - Hasta `minutos` min ANTES  (diff positivo)
    - Hasta `minutos_post` min DESPUÉS (diff negativo) — zona de volatilidad post-news
    Retorna (hay_noticia, evento, diff_minutos).  diff < 0 = evento ya ocurrió.
    """
    ahora = datetime.now(timezone.utc)
    for e in eventos:
        if not e["es_hoy"]:
            continue
        try:
            hora_evento = datetime.strptime(
                e["hora"], "%H:%M UTC"
            ).replace(
                tzinfo=timezone.utc,
                year=ahora.year, month=ahora.month, day=ahora.day
            )
            diff = (hora_evento - ahora).total_seconds() / 60
            if -minutos_post <= diff <= minutos:
                return True, e, diff
        except Exception:
            continue
    return False, None, None


# ─── 2. NOTICIAS VIA GOOGLE NEWS (gnews) ─────────────────
PALABRAS_ALCISTAS = [
    "rally", "surge", "soars", "jumps", "rises", "gains",
    "bullish", "buy", "demand", "safe haven", "inflation",
    "uncertainty", "war", "conflict", "crisis", "fear",
    "record high", "breakout", "strong", "support", "up",
    "higher", "climb", "boost", "positive"
]

PALABRAS_BAJISTAS = [
    "falls", "drops", "plunges", "sinks", "declines", "loses",
    "bearish", "sell", "pressure", "dollar", "rate hike",
    "hawkish", "weak", "breakdown", "sell-off", "down",
    "lower", "slide", "tumble", "negative", "loss"
]


def analizar_sentimiento(texto):
    texto_lower = texto.lower()
    alc = sum(1 for p in PALABRAS_ALCISTAS if p in texto_lower)
    baj = sum(1 for p in PALABRAS_BAJISTAS if p in texto_lower)
    if alc > baj:
        return "alcista", alc
    elif baj > alc:
        return "bajista", baj
    return "neutral", 0


def obtener_noticias_oro():
    """
    Usa Google News (sin API key, gratis, tiempo real)
    """
    noticias = []

    try:
        gn = GNews(
            language="en",
            country="US",
            period="1d",      # últimas 24 horas
            max_results=20
        )

        # Búsquedas sobre el oro
        queries = [
            "gold price today",
            "XAUUSD",
            "gold market USD Federal Reserve",
        ]

        vistos = set()

        for query in queries:
            try:
                resultados = gn.get_news(query)
                for art in resultados:
                    titulo = art.get("title", "") or ""
                    if titulo in vistos:
                        continue
                    vistos.add(titulo)

                    fuente = art.get("publisher", {})
                    if isinstance(fuente, dict):
                        fuente = fuente.get("title", "")

                    fecha = art.get("published date", "") or ""
                    link  = art.get("url", "") or ""

                    texto_analizar      = titulo
                    sentimiento, fuerza = analizar_sentimiento(texto_analizar)

                    noticias.append({
                        "titulo":      titulo,
                        "fuente":      fuente,
                        "link":        link,
                        "fecha":       fecha,
                        "sentimiento": sentimiento,
                        "fuerza":      fuerza,
                    })
            except Exception as e:
                print(f"Error query '{query}': {e}")
                continue

    except Exception as e:
        print(f"Error GNews: {e}")

    # Deduplicar y ordenar por fuerza
    noticias = sorted(noticias, key=lambda x: x["fuerza"], reverse=True)
    return noticias[:15]


def calcular_sentimiento_global(noticias):
    if not noticias:
        return {
            "score": 50, "direccion": "neutral",
            "alcistas": 0, "bajistas": 0, "neutrales": 0
        }

    alcistas  = [n for n in noticias if n["sentimiento"] == "alcista"]
    bajistas  = [n for n in noticias if n["sentimiento"] == "bajista"]
    neutrales = [n for n in noticias if n["sentimiento"] == "neutral"]
    total     = len(alcistas) + len(bajistas)

    if total == 0:
        return {
            "score": 50, "direccion": "neutral",
            "alcistas": 0, "bajistas": 0, "neutrales": len(neutrales)
        }

    pct_alc = round((len(alcistas) / total) * 100)
    pct_baj = round((len(bajistas) / total) * 100)

    if pct_alc >= pct_baj:
        return {"score": pct_alc, "direccion": "alcista",
                "alcistas": len(alcistas), "bajistas": len(bajistas),
                "neutrales": len(neutrales)}
    else:
        return {"score": pct_baj, "direccion": "bajista",
                "alcistas": len(alcistas), "bajistas": len(bajistas),
                "neutrales": len(neutrales)}


# ─── 3. SCORE FINAL ──────────────────────────────────────
def score_final(score_ict, sentimiento_noticias, calendario):
    ict_dir   = score_ict["direccion"]
    ict_score = score_ict["score"]
    not_dir   = sentimiento_noticias["direccion"]
    not_score = sentimiento_noticias["score"]

    ict_long  = ict_score       if ict_dir == "LONG"   else 100 - ict_score
    ict_short = 100 - ict_score if ict_dir == "LONG"   else ict_score

    if not_dir == "alcista":
        not_long, not_short = not_score, 100 - not_score
    elif not_dir == "bajista":
        not_long, not_short = 100 - not_score, not_score
    else:
        not_long = not_short = 50

    hay_noticia, evento_proximo, diff_noticia = alerta_noticia_proxima(calendario)
    if hay_noticia:
        cal_long = cal_short = 30 if diff_noticia is not None and diff_noticia < 0 else 40
    else:
        cal_long = cal_short = 55

    final_long  = (ict_long  * 0.60) + (not_long  * 0.25) + (cal_long  * 0.15)
    final_short = (ict_short * 0.60) + (not_short * 0.25) + (cal_short * 0.15)

    total     = final_long + final_short
    pct_long  = round((final_long  / total) * 100)
    pct_short = round((final_short / total) * 100)

    if pct_long >= pct_short:
        direccion_final = "LONG"
        score_val       = pct_long
    else:
        direccion_final = "SHORT"
        score_val       = pct_short

    confianza = "ALTA"  if score_val >= 75 else \
                "MEDIA" if score_val >= 60 else "BAJA"

    return {
        "direccion":      direccion_final,
        "score":          score_val,
        "confianza":      confianza,
        "pct_long":       pct_long,
        "pct_short":      pct_short,
        "hay_noticia":    hay_noticia,
        "evento_proximo": evento_proximo,
    }


# ─── TEST ─────────────────────────────────────────────────
if __name__ == "__main__":
    print("=== FASE 3 — NOTICIAS + SENTIMIENTO ===\n")

    print("Obteniendo calendario Forex Factory...")
    calendario = obtener_calendario()
    print(f"Eventos alto impacto USD hoy/mañana: {len(calendario)}")
    for e in calendario:
        dia = "HOY" if e["es_hoy"] else "MAÑANA"
        print(f"  [{dia}] {e['hora']} — {e['titulo']}")
        print(f"          Previsto: {e['previsto']} | Anterior: {e['anterior']}")

    print()
    print("Obteniendo noticias del oro (Google News)...")
    noticias = obtener_noticias_oro()
    print(f"Noticias encontradas: {len(noticias)}\n")

    for n in noticias[:10]:
        emoji = "🟢" if n["sentimiento"] == "alcista" else \
                "🔴" if n["sentimiento"] == "bajista" else "⚪"
        print(f"  {emoji} {n['titulo'][:70]}")
        print(f"     Fuente: {n['fuente']} | {n['sentimiento'].upper()} (fuerza: {n['fuerza']})")
        print()

    print()
    sentimiento = calcular_sentimiento_global(noticias)
    print(f"Sentimiento global:")
    print(f"  Dirección: {sentimiento['direccion'].upper()}")
    print(f"  Score:     {sentimiento['score']}%")
    print(f"  🟢 Alcistas:  {sentimiento['alcistas']}")
    print(f"  🔴 Bajistas:  {sentimiento['bajistas']}")
    print(f"  ⚪ Neutrales: {sentimiento['neutrales']}")