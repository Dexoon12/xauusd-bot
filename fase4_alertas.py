import anthropic
import requests
import json
import os
from datetime import datetime, timezone
from dotenv import load_dotenv

from fase3_noticias import alerta_noticia_proxima

# Carga variables de entorno
load_dotenv()

# ─── CONFIG ──────────────────────────────────────────────
ANTHROPIC_KEY    = os.getenv("ANTHROPIC_KEY",    "")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",   "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

SCORE_MINIMO     = 72
COOLDOWN_MINUTOS = 30

# ─── ESTADO ──────────────────────────────────────────────
ultima_alerta = {"tiempo": None, "direccion": None}

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
        alcistas  = sum(1 for a in data["analisis"]
                       if a["sentimiento"] == "ALCISTA")
        bajistas  = sum(1 for a in data["analisis"]
                       if a["sentimiento"] == "BAJISTA")
        neutrales = sum(1 for a in data["analisis"]
                       if a["sentimiento"] == "NEUTRAL")

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
def calcular_score_final(score_ict, sentimiento_ia, calendario):
    ict_dir   = score_ict["direccion"]
    ict_score = score_ict["score"]
    not_score = sentimiento_ia["score"]

    ict_long  = ict_score       if ict_dir == "LONG" else 100 - ict_score
    ict_short = 100 - ict_score if ict_dir == "LONG" else ict_score

    not_long  = not_score
    not_short = 100 - not_score

    hay_noticia, evento_proximo = alerta_noticia_proxima(calendario)
    cal_factor = 0.8 if hay_noticia else 1.0

    final_long  = ((ict_long  * 0.60) + (not_long  * 0.25) +
                   50 * 0.15) * cal_factor
    final_short = ((ict_short * 0.60) + (not_short * 0.25) +
                   50 * 0.15) * cal_factor

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

    confianza = "ALTA"  if score_val >= 75 and tfs_confluencia >= 3 else \
                "MEDIA" if score_val >= 65 and tfs_confluencia >= 2 else "BAJA"

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
def calcular_setup(precio_actual, direccion, score_ict):
    tf_data = score_ict["por_tf"].get("M15", {})
    obs     = tf_data.get("obs",  [])
    fvgs    = tf_data.get("fvgs", [])
    spread  = 0.20

    if direccion == "LONG":
        entry       = round(precio_actual + spread, 2)
        ob_alcistas = [o for o in obs
                       if o["tipo"] == "alcista" and
                       not o["mitigado"] and
                       o["low"] < precio_actual]
        sl = round(max(ob_alcistas, key=lambda x: x["low"])["low"] - 0.5, 2) \
             if ob_alcistas else round(entry - 8, 2)

        fvg_arriba = [f for f in fvgs
                      if f["tipo"] == "bajista" and
                      not f["llenado"] and
                      f["low"] > precio_actual]
        tp = round(min(fvg_arriba, key=lambda x: x["low"])["low"], 2) \
             if fvg_arriba else round(entry + abs(entry - sl) * 2, 2)

    else:
        entry       = round(precio_actual, 2)
        ob_bajistas = [o for o in obs
                       if o["tipo"] == "bajista" and
                       not o["mitigado"] and
                       o["high"] > precio_actual]
        sl = round(min(ob_bajistas, key=lambda x: x["high"])["high"] + 0.5, 2) \
             if ob_bajistas else round(entry + 8, 2)

        fvg_abajo = [f for f in fvgs
                     if f["tipo"] == "alcista" and
                     not f["llenado"] and
                     f["high"] < precio_actual]
        tp = round(max(fvg_abajo, key=lambda x: x["high"])["high"], 2) \
             if fvg_abajo else round(entry - abs(entry - sl) * 2, 2)

    riesgo = round(abs(entry - sl), 2)
    reward = round(abs(entry - tp), 2)
    rr     = round(reward / riesgo, 1) if riesgo > 0 else 0

    return {
        "entry": entry, "sl": sl, "tp": tp,
        "riesgo": riesgo, "reward": reward, "rr": rr
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


def formatear_alerta(score_final, setup, sentimiento_ia,
                     score_ict, precio_actual):
    emoji_dir  = "🟢" if score_final["direccion"] == "LONG" else "🔴"
    emoji_conf = "🔥" if score_final["confianza"] == "ALTA"  else \
                 "⚡" if score_final["confianza"] == "MEDIA" else "⚠️"

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

    mensaje = f"""
{emoji_dir} <b>SEÑAL {score_final['direccion']} — XAUUSD</b>
━━━━━━━━━━━━━━━━━━━━━━
{emoji_conf} Score: <b>{score_final['score']}%</b> | Confianza: <b>{score_final['confianza']}</b>
TFs en confluencia: {score_final['tfs_confluencia']}/4

📊 <b>Análisis ICT</b>
{tf_lineas}
📰 <b>Noticias IA ({sentimiento_ia['score']}%)</b>
{sentimiento_ia['resumen']}
  🟢 {sentimiento_ia['alcistas']} | 🔴 {sentimiento_ia['bajistas']}
{noticia_txt}
💰 <b>Setup sugerido</b>
  Precio: {precio_actual}
  Entry:  <b>{setup['entry']}</b>
  SL:     {setup['sl']}  ({setup['riesgo']} pts)
  TP:     {setup['tp']}  ({setup['reward']} pts)
  R:R  →  <b>1:{setup['rr']}</b>

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
    if ultima_alerta["direccion"] != direccion and diff < 10:
        return False
    return True