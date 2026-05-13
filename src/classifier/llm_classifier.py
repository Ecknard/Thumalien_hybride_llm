"""
src/classifier/llm_classifier.py — v2
Classificateur Fake News basé sur Phi-3 Mini via Ollama. CPU uniquement.

FIXES v2 :
  - Auto-disable après MAX_CONSECUTIVE_TIMEOUTS timeouts consécutifs
  - Timeout adaptatif basé sur la latence réelle mesurée
  - Prompt allégé (2 exemples vs 5) → 60% moins de tokens → plus rapide
  - num_ctx=1024 et num_thread=4 pour réduire la charge CPU
"""
import json
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

import requests
from loguru import logger

sys.path.append(str(Path(__file__).parents[2]))
from config import (
    LLM_MODEL_NAME, LLM_OLLAMA_URL,
    LLM_TIMEOUT_SECONDS, LLM_TEMPERATURE, LLM_MAX_TOKENS,
    CREDIBILITY_THRESHOLD, LOG_FILE
)

logger.add(LOG_FILE, rotation="10 MB", level="INFO")

MAX_CONSECUTIVE_TIMEOUTS = 2

# ============================================================
# PROMPT ALLÉGÉ
# ============================================================

SYSTEM_PROMPT = """Tu es un expert en détection de désinformation sur les réseaux sociaux.
Analyse le post Bluesky fourni et réponds UNIQUEMENT en JSON valide avec ces champs :
- "credibility_score": float 0.0 (fake) à 1.0 (fiable)
- "is_fake": bool (true si score < 0.5)
- "confidence": float 0.0 à 1.0
- "reasoning": string, max 1 phrase en français
- "signals": liste de strings (signaux suspects, vide si aucun)

Signaux suspects : URGENT/ALERTE/BREAKING, appel à partager avant censure, complotisme,
majuscules excessives, ponctuation excessive, source absente, ironie camouflant désinformation.
Signaux fiables : source citée, langage nuancé, faits vérifiables."""

FEW_SHOT_EXAMPLES = [
    {
        "role": "user",
        "content": 'Post: "URGENT !!! Le gouvernement cache la vérité ! Partagez avant censure !!!"'
    },
    {
        "role": "assistant",
        "content": '{"credibility_score":0.05,"is_fake":true,"confidence":0.95,"reasoning":"Urgence artificielle, appel censure, source absente.","signals":["urgence_artificielle","appel_censure","source_absente"]}'
    },
    {
        "role": "user",
        "content": 'Post: "Selon une étude publiée dans Nature (2024), les thérapies géniques montrent 87% d\'efficacité."'
    },
    {
        "role": "assistant",
        "content": '{"credibility_score":0.91,"is_fake":false,"confidence":0.88,"reasoning":"Source académique précise, langage factuel, chiffres spécifiques.","signals":[]}'
    },
]


# ============================================================
# CLASSE PRINCIPALE
# ============================================================

class PhiClassifier:
    """
    Classificateur Fake News basé sur Phi-3 Mini via Ollama.
    Résilient : se désactive automatiquement si trop lent sur le CPU.
    """

    def __init__(self, model_name: str = LLM_MODEL_NAME, ollama_url: str = LLM_OLLAMA_URL):
        self.model_name = model_name
        self.ollama_url = ollama_url.rstrip("/")
        self._available: Optional[bool] = None
        self._consecutive_timeouts = 0
        self._auto_disabled = False
        self._measured_latency: Optional[float] = None

    def is_available(self) -> bool:
        if self._auto_disabled:
            return False
        if self._available is not None:
            return self._available
        try:
            resp = requests.get(f"{self.ollama_url}/api/tags", timeout=5)
            if resp.status_code != 200:
                self._available = False
                return False
            models = [m["name"] for m in resp.json().get("models", [])]
            base = self.model_name.split(":")[0]
            self._available = any(base in m for m in models)
            if not self._available:
                logger.warning(
                    f"Ollama OK mais '{self.model_name}' absent. "
                    f"Lancez : ollama pull {self.model_name}"
                )
            else:
                logger.info(f"Phi-3 disponible — modèle : {self.model_name}")
            return self._available
        except requests.exceptions.ConnectionError:
            self._available = False
            logger.warning(f"Ollama non joignable sur {self.ollama_url}. Lancez : ollama serve")
            return False

    def reset(self):
        """Réactive Phi-3 après auto-disable."""
        self._consecutive_timeouts = 0
        self._auto_disabled = False
        self._available = None
        self._measured_latency = None
        logger.info("PhiClassifier réinitialisé.")

    def _effective_timeout(self) -> int:
        if self._measured_latency:
            return max(int(self._measured_latency * 2.5), 30)
        return LLM_TIMEOUT_SECONDS

    def _build_messages(self, text: str) -> List[Dict]:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(FEW_SHOT_EXAMPLES)
        messages.append({"role": "user", "content": f'Post: "{text[:300]}"'})
        return messages

    def _call_ollama(self, messages: List[Dict]) -> Optional[str]:
        timeout = self._effective_timeout()
        payload = {
            "model": self.model_name,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": LLM_TEMPERATURE,
                "num_predict": LLM_MAX_TOKENS,
                "num_ctx": 1024,
                "num_thread": 4,
            }
        }
        try:
            t0 = time.time()
            resp = requests.post(f"{self.ollama_url}/api/chat", json=payload, timeout=timeout)
            latency = time.time() - t0
            resp.raise_for_status()
            self._measured_latency = latency
            self._consecutive_timeouts = 0
            logger.debug(f"Phi-3 répondu en {latency:.1f}s")
            return resp.json()["message"]["content"]

        except requests.exceptions.Timeout:
            self._consecutive_timeouts += 1
            logger.error(
                f"Timeout Ollama après {timeout}s "
                f"(#{self._consecutive_timeouts}/{MAX_CONSECUTIVE_TIMEOUTS})"
            )
            if self._consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS:
                self._auto_disabled = True
                logger.warning(
                    "Phi-3 AUTO-DÉSACTIVÉ après timeouts répétés. "
                    "Pipeline en mode DistilBERT seul. "
                    "Appelez .reset() pour réactiver."
                )
            return None
        except Exception as e:
            logger.error(f"Erreur appel Ollama: {e}")
            return None

    def _parse_response(self, raw: str, text: str) -> Dict:
        if not raw:
            return self._fallback_result(text, "réponse vide Ollama")
        raw = raw.strip()
        if "```json" in raw:
            raw = raw.split("```json")[1].split("```")[0].strip()
        elif "```" in raw:
            raw = raw.split("```")[1].split("```")[0].strip()
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start == -1 or end == 0:
            return self._fallback_result(text, "JSON non trouvé")
        try:
            data = json.loads(raw[start:end])
            credibility = max(0.0, min(1.0, float(data.get("credibility_score", 0.5))))
            confidence = max(0.0, min(1.0, float(data.get("confidence", 0.8))))
            return {
                "credibility_score": round(credibility, 4),
                "is_fake": credibility < CREDIBILITY_THRESHOLD,
                "confidence": round(confidence, 4),
                "reasoning": str(data.get("reasoning", "Analyse Phi-3."))[:300],
                "signals": [str(s) for s in data.get("signals", [])],
                "model": f"phi3 ({self.model_name})",
            }
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"Parse JSON Phi-3 échoué: {e}")
            return self._fallback_result(text, f"JSON invalide: {e}")

    def _fallback_result(self, text: str, reason: str = "") -> Dict:
        return {
            "credibility_score": 0.5,
            "is_fake": False,
            "confidence": 0.0,
            "reasoning": f"Analyse LLM indisponible ({reason}).",
            "signals": [],
            "model": "phi3-fallback",
            "error": reason,
        }

    def predict_one(self, text: str) -> Dict:
        if not self.is_available():
            reason = "auto-désactivé" if self._auto_disabled else "Ollama non disponible"
            return self._fallback_result(text, reason)
        t0 = time.time()
        raw = self._call_ollama(self._build_messages(text))
        result = self._parse_response(raw, text)
        result["latency_sec"] = round(time.time() - t0, 2)
        logger.debug(f"Phi-3 | {result['credibility_score']:.2f} | fake={result['is_fake']} | {result['latency_sec']}s")
        return result

    def predict(self, texts: List[str]) -> List[Dict]:
        return [self.predict_one(t) for t in texts]

    def get_model_info(self) -> Dict:
        return {
            "available": self.is_available(),
            "auto_disabled": self._auto_disabled,
            "consecutive_timeouts": self._consecutive_timeouts,
            "measured_latency_sec": self._measured_latency,
            "model": self.model_name,
            "ollama_url": self.ollama_url,
        }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--predict", type=str, default=None)
    args = parser.parse_args()
    clf = PhiClassifier()
    if args.check:
        info = clf.get_model_info()
        print(f"{'✅' if info['available'] else '❌'} {info['model']} @ {info['ollama_url']}")
    if args.predict:
        r = clf.predict_one(args.predict)
        print(f"{'🔴 FAKE' if r['is_fake'] else '🟢 FIABLE'} {r['credibility_score']:.1%}")
        print(f"  {r['reasoning']}")
