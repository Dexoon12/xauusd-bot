import anthropic
import requests
import json
import os
from datetime import datetime, timezone
from dotenv import load_dotenv

from fase3_noticias import alerta_noticia_proxima

load_dotenv()

# ─── CONFIG ──────────────────────────────────────────────
ANTHROPIC_KEY    = os.getenv("ANTHROPIC_KEY",    "")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",   "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

SCORE_MINIMO     = 78
COOLDOWN_MINUTOS = 30

# ─── ESTADO ──────────────────────────────────────────────
ultima_alerta = {"tiempo": None, "direccion": None}


# ─── SESIÓN ACTIVA ────────────────────────────────────────
# Solo alertamos en sesión Londres (07-11 UTC) y Nueva York (13-17 UTC).
# Operar fuera de estas ventanas aumenta falsas señales por baja liquidez.
def en_sesion_activa():
    ahora = datetime.now(timezone.utc)
    h = ahora.hour + ahora.minute / 60.0
    return (7.0 <= h < 11.0) or (13.0 <= h < 17.0)


# ─── 1. SENTIMIENTO CON CLAUDE ───────────────────────────
def analizar_sentimiento_ia(noticias):
    if not noticias:
        return {
            "direccion": "neutral", "score": 50,
            "resumen":   "Sin noticias disponibles",
            "alcistas":  0, "bajistas": 0, "neutrales": 0
        }

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    titulares = "\n".join([
        f"{i+1}. [{n['fuente']}] {n['titulo']}"
        for i, n in enumerate(noticias[:12])
    ])

    prompt = f"""Eres un analista experto en el mercado del oro (XAUUSD).
Analiza estos titulares de noticias financieras y determina el sentimiento para el precio del oro.

TITULARES:
{titulares}

Para cada titular indica:
- ALCISTA si implica subida del oro
- BAJISTA si implica bajada del oro
- NEUTRAL si no afecta claramente

Luego da un resumen en UNA sola frase del sentimiento general.

Responde SOLO en este formato JSON exacto:
{{
  "analisis": [
    {{"id": 1, "sentimiento": "ALCISTA"}},
    {{"id": 2, "sentimiento": "BAJISTA"}}
  ],
  "direccion_global": "ALCISTA",
  "score": 75,
  "resumen": "El oro enfrenta presión bajista por fortaleza del dólar"
}}

El score va de 0 a 100 donde:
- 100 = extremadamente alcista
- 50  = neutral
- 0   = extremadamente bajista
"""

    try:
        respuesta = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )

        texto = respuesta.content[0].text.strip()
        if "```" in texto:
            texto = texto.split("```")[1]
            if texto.startswith("json"):
                texto = texto[4:]

        data      = json.loads(texto)
        alcistas  = sum(1 for a in data["analisis"] if a["sentimiento"] == "ALCISTA")
        bajistas  = sum(1 for a in data["analisis"] if a["sentimiento"] == "BAJISTA")
        neutrales = sum(1 for a in data["analisis"] if a["sentimiento"] == "NEUTRAL")

        return {
            "direccion": data["direccion_global"].lower(),
            "score":     data["score"],
            "resumen":   data["resumen"],
            "alcistas":  alcistas,
            "bajistas":  bajistas,
            "neutrales": neutrales,
        }

    except Exception as e:
        print(f"Error Claude: {e}")
        from fase3_noticias import calcular_sentimiento_global
        return calcular_sentimiento_global(noticias)


# ─── 2. SCORE FINAL ──────────────────────────────────────
# Cambios vs versión anterior:
# - Eliminada constante artificial 50*0.15 que inflaba scores débiles
# - Pesos limpios: ICT 70% + sentimiento IA 30%
# - Umbrales de confianza ligeramente subidos para más calidad
def calcular_score_final(score_ict, sentimiento_ia, calendario):
    ict_dir   = score_ict["direccion"]
    ict_score = score_ict["score"]
    not_score = sentimiento_ia["score"]

    ict_long  = ict_score       if ict_dir == "LONG" else 100 - ict_score
    ict_short = 100 - ict_score if ict_dir == "LONG" else ict_score

    not_long  = not_score
    not_short = 100 - not_score

    hay_noticia, evento_proximo, diff_noticia = alerta_noticia_proxima(calendario)
    if hay_noticia:
        cal_factor = 0.5 if diff_noticia is not None and diff_noticia < 0 else 0.8
    else:
        cal_factor = 1.0

    # Pesos limpios: ICT 70%, noticias IA 30%
    final_long  = (ict_long  * 0.70 + not_long  * 0.30) * cal_factor
    final_short = (ict_short * 0.70 + not_short * 0.30) * cal_factor

    total     = final_long + final_short
    pct_long  = round((final_long  / total) * 100)
    pct_short = round((final_short / total) * 100)

    if pct_long >= pct_short:
        direccion = "LONG"
        score_val = pct_long
    else:
        direccion = "SHORT"
        score_val = pct_short

    tfs_confluencia = sum(
        1 for data in score_ict["por_tf"].values()
        if (direccion == "LONG"  and data["tendencia"] == "alcista") or
           (direccion == "SHORT" and data["tendencia"] == "bajista")
    )

    confianza = "ALTA"  if score_val >= 78 and tfs_confluencia >= 3 else \
                "MEDIA" if score_val >= 70 and tfs_confluencia >= 2 else "BAJA"

    return {
        "direccion":       direccion,
        "score":           score_val,
        "confianza":       confianza,
        "pct_long":        pct_long,
        "pct_short":       pct_short,
        "tfs_confluencia": tfs_confluencia,
        "hay_noticia":     hay_noticia,
        "evento_proximo":  evento_proximo,
    }


# ─── 3. SETUP ENTRY/SL/TP ────────────────────────────────
# Cambios vs versión anterior:
# - SL basado en ATR (1.5x) en lugar de 8 pts fijos
# - Entrada en nivel del OB más cercano (LIMIT) cuando hay estructura
# - tipo_entrada: "LIMIT" (en OB/FVG) o "MARKET" (sin estructura cercana)
# - TP usa FVG contrario o nivel swing si no hay FVG
def calcular_setup(precio_actual, direccion, score_ict):
    tf_data = score_ict["por_tf"].get("M15", {})
    obs     = tf_data.get("obs",  [])
    fvgs    = tf_data.get("fvgs", [])
    atr     = tf_data.get("atr",  score_ict.get("atr_m15", 5.0))
    spread  = 0.20

    if direccion == "LONG":
        # Busca el OB alcista no mitigado más cercano por debajo del precio
        ob_validos = [o for o in obs
                      if o["tipo"] == "alcista" and not o["mitigado"]
                      and o["high"] <= precio_actual]

        if ob_validos:
            ob_entry     = max(ob_validos, key=lambda x: x["high"])
            entry        = round(ob_entry["high"] + spread, 2)
            sl           = round(ob_entry["low"] - atr * 0.2, 2)
            tipo_entrada = "LIMIT"
        else:
            entry        = round(precio_actual + spread, 2)
            sl           = round(entry - atr * 1.5, 2)
            tipo_entrada = "MARKET"

        # TP: FVG bajista más cercano por encima
        fvg_arriba = [f for f in fvgs
                      if f["tipo"] == "bajista" and not f["llenado"]
                      and f["low"] > precio_actual]
        if fvg_arriba:
            tp = round(min(fvg_arriba, key=lambda x: x["low"])["low"], 2)
        else:
            # Usa nivel BOS alcista como objetivo, o 2R mínimo
            nivel_bos = tf_data.get("nivel_bos_alcista")
            if nivel_bos and nivel_bos > precio_actual:
                tp = round(nivel_bos, 2)
            else:
                tp = round(entry + abs(entry - sl) * 2.0, 2)

    else:  # SHORT
        ob_validos = [o for o in obs
                      if o["tipo"] == "bajista" and not o["mitigado"]
                      and o["low"] >= precio_actual]

        if ob_validos:
            ob_entry     = min(ob_validos, key=lambda x: x["low"])
            entry        = round(ob_entry["low"], 2)
            sl           = round(ob_entry["high"] + atr * 0.2, 2)
            tipo_entrada = "LIMIT"
        else:
            entry        = round(precio_actual, 2)
            sl           = round(entry + atr * 1.5, 2)
            tipo_entrada = "MARKET"

        # TP: FVG alcista más cercano por debajo
        fvg_abajo = [f for f in fvgs
                     if f["tipo"] == "alcista" and not f["llenado"]
                     and f["high"] < precio_actual]
        if fvg_abajo:
            tp = round(max(fvg_abajo, key=lambda x: x["high"])["high"], 2)
        else:
            nivel_bos = tf_data.get("nivel_bos_bajista")
            if nivel_bos and nivel_bos < precio_actual:
                tp = round(nivel_bos, 2)
            else:
                tp = round(entry - abs(entry - sl) * 2.0, 2)

    riesgo = round(abs(entry - sl), 2)
    reward = round(abs(entry - tp), 2)
    rr     = round(reward / riesgo, 1) if riesgo > 0 else 0

    return {
        "entry":        entry,
        "sl":           sl,
        "tp":           tp,
        "riesgo":       riesgo,
        "reward":       reward,
        "rr":           rr,
        "tipo_entrada": tipo_entrada,
        "atr":          round(atr, 2),
    }


# ─── 4. TELEGRAM ─────────────────────────────────────────
def enviar_telegram(mensaje):
    try:
        url    = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        params = {
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       mensaje,
            "parse_mode": "HTML",
        }
        resp = requests.post(url, params=params, timeout=10)
        if resp.status_code == 200:
            print("  ✅ Alerta Telegram enviada")
        else:
            print(f"  ❌ Error Telegram: {resp.text}")
    except Exception as e:
        print(f"  Error enviando Telegram: {e}")


def formatear_alerta(score_final, setup, sentimiento_ia, score_ict, precio_actual):
    emoji_dir   = "🟢" if score_final["direccion"] == "LONG" else "🔴"
    emoji_conf  = "🔥" if score_final["confianza"] == "ALTA"  else \
                  "⚡" if score_final["confianza"] == "MEDIA" else "⚠️"
    emoji_entry = "🎯" if setup.get("tipo_entrada") == "LIMIT" else "⚡"
    tipo_txt    = "LIMIT" if setup.get("tipo_entrada") == "LIMIT" else "MARKET"

    tf_lineas = ""
    for tf, data in score_ict["por_tf"].items():
        tend  = data["tendencia"]
        check = "✅" if (
            (score_final["direccion"] == "LONG"  and tend == "alcista") or
            (score_final["direccion"] == "SHORT" and tend == "bajista")
        ) else "❌"
        tf_lineas += f"  {tf}  → {tend.upper()} {check}\n"

    noticia_txt = ""
    if score_final["hay_noticia"] and score_final["evento_proximo"]:
        ev = score_final["evento_proximo"]
        noticia_txt = (f"\n⚠️ <b>PRECAUCIÓN:</b> "
                       f"{ev['titulo']} a las {ev['hora']}\n")

    # Instrucción de orden clara según tipo de entrada
    dir_  = score_final["direccion"]
    if tipo_txt == "LIMIT":
        orden_tipo = "BUY LIMIT"  if dir_ == "LONG"  else "SELL LIMIT"
        orden_txt  = (f"🎯 <b>PON {orden_tipo} EN {setup['entry']}</b>\n"
                      f"  (precio actual: {precio_actual})")
    else:
        orden_tipo = "BUY MARKET" if dir_ == "LONG"  else "SELL MARKET"
        orden_txt  = f"⚡ <b>ENTRA {orden_tipo} AHORA @ {setup['entry']}</b>"

    mensaje = f"""
{emoji_dir} <b>SEÑAL {dir_} — XAUUSD</b>
━━━━━━━━━━━━━━━━━━━━━━
{emoji_conf} Score: <b>{score_final['score']}%</b> | Confianza: <b>{score_final['confianza']}</b>
TFs en confluencia: {score_final['tfs_confluencia']}/4

{orden_txt}
  SL:  <b>{setup['sl']}</b>  ({setup['riesgo']} pts)
  TP:  <b>{setup['tp']}</b>  ({setup['reward']} pts)
  R:R → <b>1:{setup['rr']}</b>
  ATR: {setup.get('atr', '—')} pts

📊 <b>Análisis ICT</b>
{tf_lineas}
📰 <b>Noticias IA ({sentimiento_ia['score']}%)</b>
{sentimiento_ia['resumen']}
  🟢 {sentimiento_ia['alcistas']} | 🔴 {sentimiento_ia['bajistas']}
{noticia_txt}
🕐 {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}
━━━━━━━━━━━━━━━━━━━━━━
⚠️ <i>Solo análisis. No es consejo financiero.</i>
"""
    return mensaje.strip()


# ─── 5. COOLDOWN ─────────────────────────────────────────
def puede_alertar(direccion):
    ahora = datetime.now(timezone.utc)
    if ultima_alerta["tiempo"] is None:
        return True
    diff = (ahora - ultima_alerta["tiempo"]).total_seconds() / 60
    if ultima_alerta["direccion"] == direccion and diff < COOLDOWN_MINUTOS:
        return False
    if ultima_alerta["direccion"] != direccion and diff < 30:
        return False
    return True
