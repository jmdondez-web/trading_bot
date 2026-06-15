import requests
import json
import logging
from config import Config

logger = logging.getLogger("TradingCommittee")

# FIX: modèle Claude mis à jour (claude-3-haiku-20240307 est en fin de vie)
CLAUDE_MODEL  = "claude-haiku-4-5-20251001"
GPT_MODEL     = "gpt-4o-mini"
GEMINI_MODEL  = "gemini-2.0-flash"

class TradingCommittee:
    def __init__(self, db):
        self.db = db
        self.timeout = 5

    def evaluate(self, signal):
        if not Config.COMMITTEE_ENABLED:
            return True

        # FIX: on détermine d'abord quels modèles sont réellement configurés.
        # Ancienne version : si une clé manquait, le vote retournait True par défaut
        # -> le comité approuvait tout sans filtrer, fausse sécurité.
        # Nouvelle version : un modèle sans clé = abstention (non compté).
        # Le quorum COMMITTEE_VOTE_MIN s'applique sur les votes réels uniquement.
        votes = []
        vote_details = []

        if Config.CLAUDE_API_KEY:
            v = self._ask_claude(signal)
            votes.append(v)
            vote_details.append(f"Claude={'OUI' if v else 'NON'}")
        else:
            vote_details.append("Claude=ABSENT")

        if Config.OPENAI_API_KEY:
            v = self._ask_gpt(signal)
            votes.append(v)
            vote_details.append(f"GPT={'OUI' if v else 'NON'}")
        else:
            vote_details.append("GPT=ABSENT")

        if Config.GEMINI_API_KEY:
            v = self._ask_gemini(signal)
            votes.append(v)
            vote_details.append(f"Gemini={'OUI' if v else 'NON'}")
        else:
            vote_details.append("Gemini=ABSENT")

        # FIX: si aucune clé configurée, le comité ne peut pas voter
        # -> on rejette plutôt que d'approuver aveuglément
        if not votes:
            logger.warning(
                "Comité activé mais aucune clé API configurée "
                "(CLAUDE_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY) — signal rejeté."
            )
            return False

        accepted = sum(1 for v in votes if v)
        # Quorum adapté : si moins de 3 modèles disponibles, on abaisse le seuil
        quorum = min(Config.COMMITTEE_VOTE_MIN, len(votes))
        result = "ACCEPTED" if accepted >= quorum else "REJECTED"

        logger.info(
            f"Comité {signal['symbol']}: {' | '.join(vote_details)} "
            f"({accepted}/{len(votes)} >= {quorum}) -> {result}"
        )

        # Padding votes à 3 pour la DB (colonnes vote1/vote2/vote3 fixes)
        padded = votes + [None] * (3 - len(votes))
        self.db.add_committee_vote(signal['symbol'], padded, result)
        return result == "ACCEPTED"

    def _build_prompt(self, signal):
        return (
            f"Signal trading crypto: {signal['symbol']} "
            f"RSI={signal['rsi']:.1f} "
            f"Contexte={signal['context_state']} "
            f"Prix={signal['price']:.2f} "
            f"BB={signal['bb_position']}. "
            f"Faut-il acheter? Réponds uniquement OUI ou NON."
        )

    def _ask_claude(self, signal):
        try:
            prompt = self._build_prompt(signal)
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": Config.CLAUDE_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": CLAUDE_MODEL,  # FIX: modèle à jour
                    "max_tokens": 10,
                    "messages": [{"role": "user", "content": prompt}]
                },
                timeout=self.timeout
            )
            text = resp.json().get('content', [{}])[0].get('text', '').upper()
            return "OUI" in text
        except Exception as e:
            # FIX: erreur = abstention (False) plutôt que vote OUI par défaut
            logger.warning(f"Claude erreur (abstention): {e}")
            return False

    def _ask_gpt(self, signal):
        try:
            prompt = self._build_prompt(signal)
            resp = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {Config.OPENAI_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": GPT_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 10
                },
                timeout=self.timeout
            )
            text = resp.json()['choices'][0]['message']['content'].upper()
            return "OUI" in text
        except Exception as e:
            # FIX: erreur = abstention (False) plutôt que vote OUI par défaut
            logger.warning(f"GPT erreur (abstention): {e}")
            return False

    def _ask_gemini(self, signal):
        try:
            prompt = self._build_prompt(signal)
            resp = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{GEMINI_MODEL}:generateContent?key={Config.GEMINI_API_KEY}",
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=self.timeout
            )
            text = resp.json()['candidates'][0]['content']['parts'][0]['text'].upper()
            return "OUI" in text
        except Exception as e:
            # FIX: erreur = abstention (False) plutôt que vote OUI par défaut
            logger.warning(f"Gemini erreur (abstention): {e}")
            return False

    def get_stats(self):
        return self.db.get_committee_stats()
