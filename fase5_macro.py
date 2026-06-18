"""
Módulo macro: lee expectativas de mercado (consensus) vs datos reales
y calcula el sesgo macro actual para el oro.

Fuente: ForexFactory JSON (nfs.faireconomy.media) — gratis, sin API key.
Cubre: FOMC, NFP, CPI, PCE, JOLTS, ADP, GDP, ISM, Retail Sales, etc.

Lógica central:
  dato real > consenso  →  economía fuerte  →  Fed hawkish  →  oro BAJA
  dato real < consenso  →  economía débil   →  Fed dovish   →  oro SUBE

Excepciones:
  Unemployment Rate, Jobless Claims:
    dato ALTO = más desempleo = Fed dovish = oro SUBE
"""

import requests
from datetime import datetime, timezone, timedelta

# ─── MAPA DE IMPACTO EN ORO ──────────────────────────────────────────────────
# mejor_alto=True:  actual > forecast → economía fuerte → hawkish → oro BAJA
# mejor_alto=False: actual > forecast → dato malo para economía → dovish → oro SUBE
EVENTOS_IMPACTO = {
    # ── Empleo ────────────────────────────────────────────────
    "Non-Farm Employment Change":     {"peso": 5, "mejor_alto": True},
    "Non-Farm Payrolls":              {"peso": 5, "mejor_alto": True},
    "ADP Non-Farm Employment Change": {"peso": 3, "mejor_alto": True},
    "Unemployment Rate":              {"peso": 4, "mejor_alto": False},
    "JOLTS Job Openings":             {"peso": 3, "mejor_alto": True},
    "Initial Jobless Claims":         {"peso": 2, "mejor_alto": False},
    "Continuing Jobless Claims":      {"peso": 1, "mejor_alto": False},

    # ── Inflación ─────────────────────────────────────────────
    # Régimen actual (2026): inflación alta → Fed sube tasas → oro BAJA
    "CPI m/m":                        {"peso": 5, "mejor_alto": True},
    "Core CPI m/m":                   {"peso": 5, "mejor_alto": True},
    "CPI y/y":                        {"peso": 4, "mejor_alto": True},
    "Core CPI y/y":                   {"peso": 4, "mejor_alto": True},
    "PPI m/m":                        {"peso": 4, "mejor_alto": True},
    "Core PPI m/m":                   {"peso": 4, "mejor_alto": True},
    "PCE Price Index m/m":            {"peso": 5, "mejor_alto": True},
    "Core PCE Price Index m/m":       {"peso": 5, "mejor_alto": True},

    # ── Fed / Tasas ───────────────────────────────────────────
    "Federal Funds Rate":             {"peso": 5, "mejor_alto": True},
    "FOMC Statement":                 {"peso": 5, "mejor_alto": True},
    "FOMC Meeting Minutes":           {"peso": 4, "mejor_alto": True},

    # ── Crecimiento ───────────────────────────────────────────
    "GDP q/q":                        {"peso": 4, "mejor_alto": True},
    "Preliminary GDP q/q":            {"peso": 3, "mejor_alto": True},
    "Flash GDP q/q":                  {"peso": 3, "mejor_alto": True},
    "Retail Sales m/m":               {"peso": 3, "mejor_alto": True},
    "Core Retail Sales m/m":          {"peso": 3, "mejor_alto": True},

    # ── Actividad / PMI ───────────────────────────────────────
    "ISM Manufacturing PMI":          {"peso": 3, "mejor_alto": True},
    "ISM Services PMI":               {"peso": 3, "mejor_alto": True},
    "Manufacturing PMI":              {"peso": 2, "mejor_alto": True},
    "Services PMI":                   {"peso": 2, "mejor_alto": True},
    "Empire State Manufacturing Index": {"peso": 1, "mejor_alto": True},
    "Philly Fed Manufacturing Index": {"peso": 1, "mejor_alto": True},

    # ── Confianza consumidor ──────────────────────────────────
    "CB Consumer Confidence":         {"peso": 2, "mejor_alto": True},
    "UoM Consumer Sentiment":         {"peso": 2, "mejor_alto": True},
    "Prelim UoM Consumer Sentiment":  {"peso": 2, "mejor_alto": True},

    # ── Vivienda ──────────────────────────────────────────────
    "New Home Sales":                 {"peso": 2, "mejor_alto": True},
    "Existing Home Sales":            {"peso": 2, "mejor_alto": True},
    "Building Permits":               {"peso": 2, "mejor_alto": True},
    "Housing Starts":                 {"peso": 2, "mejor_alto": True},
}


# ─── PARSEAR VALORES ─────────────────────────────────────────────────────────
def parsear_valor(texto):
    """
    Convierte '180K', '3.2%', '-0.1M', '3.75' a float.
    Retorna None si no se puede parsear o está vacío.
    """
    if not texto or str(texto).strip() in ("—", "", "N/A", "None", "nan"):
        return None

    texto = str(texto).strip().replace(",", "").replace(" ", "")

    multiplicador = 1.0
    if texto.endswith("%"):
        texto = texto[:-1]
    elif texto.upper().endswith("K"):
        multiplicador = 1e3
        texto = texto[:-1]
    elif texto.upper().endswith("M"):
        multiplicador = 1e6
        texto = texto[:-1]
    elif texto.upper().endswith("B"):
        multiplicador = 1e9
        texto = texto[:-1]
    elif texto.upper().endswith("T"):
        multiplicador = 1e12
        texto = texto[:-1]

    try:
        return float(texto) * multiplicador
    except (ValueError, TypeError):
        return None


# ─── CALCULAR SORPRESA ───────────────────────────────────────────────────────
def calcular_sorpresa(evento):
    """
    Sorpresa = (actual - forecast) / |forecast| * 100
    Positivo = mejor que esperado (para la economía)
    Negativo = peor que esperado

    Retorna: (sorpresa_pct, actual_val, forecast_val) o (None, None, None)
    """
    actual_val   = parsear_valor(evento.get("actual",   ""))
    forecast_val = parsear_valor(evento.get("previsto", ""))

    if actual_val is None or forecast_val is None:
        return None, None, None

    if forecast_val == 0:
        sorpresa_pct = (actual_val - forecast_val) * 100
    else:
        sorpresa_pct = ((actual_val - forecast_val) / abs(forecast_val)) * 100

    sorpresa_pct = max(-200, min(200, sorpresa_pct))
    return round(sorpresa_pct, 1), actual_val, forecast_val


# ─── IMPACTO EN ORO ──────────────────────────────────────────────────────────
def impacto_en_oro(titulo, sorpresa_pct, peso):
    """
    Traduce el evento + sorpresa a impacto en precio del oro.

    Retorna: {"direccion_oro": "bajista"/"alcista"/"neutral", "magnitud": 0-1, "razon": str}
    """
    if sorpresa_pct is None or abs(sorpresa_pct) < 3:
        return {"direccion_oro": "neutral", "magnitud": 0.0, "razon": "sorpresa mínima"}

    # Buscar en el mapa (exacto o parcial)
    info = EVENTOS_IMPACTO.get(titulo)
    if not info:
        titulo_lower = titulo.lower()
        for key, val in EVENTOS_IMPACTO.items():
            if key.lower() in titulo_lower or titulo_lower in key.lower():
                info = val
                break

    if not info:
        return {"direccion_oro": "neutral", "magnitud": 0.0, "razon": "evento no mapeado"}

    mejor_alto = info["mejor_alto"]

    if sorpresa_pct > 0 and mejor_alto:
        direccion = "bajista"
        razon = f"beat: +{sorpresa_pct:.0f}% vs consenso → hawkish → oro baja"
    elif sorpresa_pct < 0 and mejor_alto:
        direccion = "alcista"
        razon = f"miss: {sorpresa_pct:.0f}% vs consenso → dovish → oro sube"
    elif sorpresa_pct > 0 and not mejor_alto:
        direccion = "alcista"
        razon = f"dato alto (malo): +{sorpresa_pct:.0f}% → dovish → oro sube"
    else:
        direccion = "bajista"
        razon = f"dato bajo (bueno): {sorpresa_pct:.0f}% → hawkish → oro baja"

    # Magnitud normalizada: sorpresa grande en evento pesado = magnitud alta
    magnitud = min(1.0, abs(sorpresa_pct) / 20 * (peso / 5))

    return {
        "direccion_oro": direccion,
        "magnitud":      round(magnitud, 2),
        "razon":         razon,
    }


# ─── OBTENER CALENDARIO MACRO ────────────────────────────────────────────────
def obtener_calendario_macro():
    """
    Obtiene esta semana + la próxima de ForexFactory JSON.
    Incluye todos los eventos USD con forecast/actual/previous.
    """
    eventos = []
    urls = [
        "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
        "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
    ]
    ahora = datetime.now(timezone.utc)

    for url in urls:
        try:
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            if resp.status_code != 200:
                continue

            for ev in resp.json():
                if ev.get("country", "") != "USD":
                    continue
                fecha_str = ev.get("date", "")
                if not fecha_str:
                    continue

                try:
                    dt_evento = datetime.fromisoformat(fecha_str).astimezone(timezone.utc)
                except Exception:
                    continue

                diff_min = (dt_evento - ahora).total_seconds() / 60

                eventos.append({
                    "titulo":         ev.get("title",    "—"),
                    "hora_utc":       dt_evento.strftime("%H:%M UTC"),
                    "fecha":          dt_evento.strftime("%Y-%m-%d"),
                    "impacto":        ev.get("impact",   "low"),
                    "previsto":       ev.get("forecast", ""),
                    "anterior":       ev.get("previous", ""),
                    "actual":         ev.get("actual",   ""),
                    "dt":             dt_evento,
                    "diff_min":       round(diff_min),
                    "es_hoy":         dt_evento.date() == ahora.date(),
                    "ya_ocurrio":     diff_min < 0,
                    "tiene_actual":   bool(ev.get("actual", "").strip()),
                })
        except Exception as e:
            print(f"Error macro calendario: {e}")

    eventos.sort(key=lambda x: x["dt"])
    return eventos


# ─── EVENTOS CON SORPRESA RECIENTE ──────────────────────────────────────────
def eventos_con_sorpresa_reciente(eventos, horas=8):
    """
    Eventos que ya ocurrieron y tienen actual, dentro de las últimas `horas` horas.
    """
    recientes = []

    for ev in eventos:
        if not ev["tiene_actual"] or not ev["ya_ocurrio"]:
            continue

        min_desde = abs(ev["diff_min"])
        if min_desde > horas * 60:
            continue

        es_relevante = ev["titulo"] in EVENTOS_IMPACTO or ev["impacto"] == "high"
        if not es_relevante:
            continue

        sorpresa_pct, actual_val, forecast_val = calcular_sorpresa(ev)
        if sorpresa_pct is None:
            continue

        peso    = EVENTOS_IMPACTO.get(ev["titulo"], {}).get("peso", 2)
        impacto = impacto_en_oro(ev["titulo"], sorpresa_pct, peso)

        recientes.append({
            **ev,
            "sorpresa_pct":  sorpresa_pct,
            "sorpresa_abs":  abs(sorpresa_pct),
            "actual_val":    actual_val,
            "forecast_val":  forecast_val,
            "impacto_oro":   impacto,
            "min_desde":     round(min_desde),
            "peso":          peso,
        })

    recientes.sort(key=lambda x: x["min_desde"])
    return recientes


# ─── PRÓXIMOS EVENTOS DE RIESGO ──────────────────────────────────────────────
def proximos_eventos_riesgo(eventos, horas=48):
    """
    Eventos próximos (aún no ocurridos) con su consensus.
    """
    proximos = []

    for ev in eventos:
        if ev["ya_ocurrio"]:
            continue
        if ev["diff_min"] > horas * 60 or ev["diff_min"] < -10:
            continue

        es_relevante = ev["titulo"] in EVENTOS_IMPACTO or ev["impacto"] == "high"
        if not es_relevante:
            continue

        peso = EVENTOS_IMPACTO.get(ev["titulo"], {}).get("peso", 2)
        proximos.append({
            **ev,
            "peso":        peso,
            "horas_hasta": round(ev["diff_min"] / 60, 1),
        })

    proximos.sort(key=lambda x: (x["diff_min"], -x["peso"]))
    return proximos


# ─── CALCULAR FACTOR MACRO ───────────────────────────────────────────────────
def calcular_factor_macro():
    """
    Función principal. Calcula el sesgo macro actual para el oro.

    Retorna:
    {
        "sesgo":                 "alcista"/"bajista"/"neutral",
        "factor_long":           float 30-70 (usado en score final),
        "factor_short":          float 30-70,
        "intensidad":            float 0-1,
        "eventos_recientes":     list,
        "proximos_riesgo":       list,
        "resumen":               str,
        "hay_riesgo_inmediato":  bool,   # evento peso>=4 en < 2h
        "evento_inminente":      dict/None,
        "trigger_sorpresa":      bool,   # sorpresa grande en < 20 min
        "trigger_evento":        dict/None,
    }
    """
    eventos   = obtener_calendario_macro()
    recientes = eventos_con_sorpresa_reciente(eventos, horas=8)
    proximos  = proximos_eventos_riesgo(eventos, horas=48)

    # ── Calcular sesgo desde sorpresas recientes ──────────────
    score_alcista = 0.0
    score_bajista = 0.0
    peso_total    = 0.0

    for ev in recientes:
        impacto  = ev["impacto_oro"]
        magnitud = impacto["magnitud"]
        peso     = ev["peso"]
        # Decaimiento: sorpresa hace 8h pesa 20% de lo que pesa hace 0h
        decay = max(0.2, 1.0 - ev["min_desde"] / (8 * 60))

        if impacto["direccion_oro"] == "alcista":
            score_alcista += peso * magnitud * decay
        elif impacto["direccion_oro"] == "bajista":
            score_bajista += peso * magnitud * decay

        peso_total += peso * decay

    # ── Determinar sesgo ──────────────────────────────────────
    if peso_total < 0.3:
        sesgo      = "neutral"
        intensidad = 0.0
    else:
        diff = score_alcista - score_bajista
        if diff > peso_total * 0.15:
            sesgo      = "alcista"
            intensidad = min(1.0, diff / peso_total)
        elif diff < -peso_total * 0.15:
            sesgo      = "bajista"
            intensidad = min(1.0, abs(diff) / peso_total)
        else:
            sesgo      = "neutral"
            intensidad = 0.0

    # ── Convertir a factores de score (base 50, rango 30-70) ──
    if sesgo == "alcista":
        factor_long  = 50 + intensidad * 20
        factor_short = 50 - intensidad * 20
    elif sesgo == "bajista":
        factor_long  = 50 - intensidad * 20
        factor_short = 50 + intensidad * 20
    else:
        factor_long  = factor_short = 50.0

    # ── Evento inminente (evento peso>=4 en < 2h) ─────────────
    hay_riesgo_inmediato = False
    evento_inminente     = None
    for ev in proximos:
        if ev["peso"] >= 4 and 0 < ev["horas_hasta"] <= 2.0:
            hay_riesgo_inmediato = True
            evento_inminente     = ev
            break

    # ── Trigger: sorpresa grande en los últimos 20 min ────────
    trigger_sorpresa = False
    trigger_evento   = None
    for ev in recientes:
        if ev["min_desde"] <= 20 and ev["sorpresa_abs"] >= 15 and ev["peso"] >= 3:
            trigger_sorpresa = True
            trigger_evento   = ev
            break

    # ── Resumen legible ───────────────────────────────────────
    if recientes:
        ev0    = recientes[0]
        imp0   = ev0["impacto_oro"]["direccion_oro"].upper()
        resumen = (f"{ev0['titulo']}: {ev0['sorpresa_pct']:+.0f}% vs consenso "
                   f"({ev0['actual']} vs {ev0['previsto']}) → {imp0} para oro")
    elif proximos:
        prox    = proximos[0]
        resumen = (f"Próximo: {prox['titulo']} en {prox['horas_hasta']:.1f}h "
                   f"(consenso: {prox['previsto'] or '—'})")
    else:
        resumen = "Sin datos macro recientes"

    return {
        "sesgo":                sesgo,
        "factor_long":          round(factor_long,  1),
        "factor_short":         round(factor_short, 1),
        "intensidad":           round(intensidad, 2),
        "eventos_recientes":    recientes,
        "proximos_riesgo":      proximos,
        "resumen":              resumen,
        "hay_riesgo_inmediato": hay_riesgo_inmediato,
        "evento_inminente":     evento_inminente,
        "trigger_sorpresa":     trigger_sorpresa,
        "trigger_evento":       trigger_evento,
    }


# ─── TEST ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 55)
    print("  FASE 5 — ANÁLISIS MACRO")
    print("=" * 55)

    macro = calcular_factor_macro()

    print(f"\nSesgo macro:  {macro['sesgo'].upper()} (intensidad: {macro['intensidad']:.2f})")
    print(f"Factor LONG:  {macro['factor_long']}")
    print(f"Factor SHORT: {macro['factor_short']}")
    print(f"Resumen:      {macro['resumen']}")

    if macro["eventos_recientes"]:
        print(f"\nSorpresas recientes ({len(macro['eventos_recientes'])}):")
        for ev in macro["eventos_recientes"][:5]:
            imp   = ev["impacto_oro"]
            emoji = "🟢" if imp["direccion_oro"] == "alcista" else "🔴" if imp["direccion_oro"] == "bajista" else "⚪"
            print(f"  {emoji} [{ev['min_desde']}min] {ev['titulo']}")
            print(f"       Real: {ev['actual']} | Consenso: {ev['previsto']} | "
                  f"Sorpresa: {ev['sorpresa_pct']:+.1f}%")
            print(f"       → {imp['razon']}")

    if macro["proximos_riesgo"]:
        print(f"\nPróximos eventos de riesgo:")
        for ev in macro["proximos_riesgo"][:8]:
            peso_str = "⭐" * ev["peso"]
            print(f"  [{ev['horas_hasta']:.1f}h] {ev['titulo']:<40} "
                  f"consenso: {ev['previsto'] or '—':<10} {peso_str}")

    if macro["hay_riesgo_inmediato"]:
        ev = macro["evento_inminente"]
        print(f"\n⚠️  EVENTO INMINENTE: {ev['titulo']} en {ev['horas_hasta']:.1f}h")

    if macro["trigger_sorpresa"]:
        ev = macro["trigger_evento"]
        print(f"\n🚨 TRIGGER: {ev['titulo']} sorpresa {ev['sorpresa_pct']:+.0f}% hace {ev['min_desde']} min")
