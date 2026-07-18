"""Read-only, bounded lexical sentiment analytics for the Macro Map.

This module deliberately produces analytical context only.  It has no imports
from the execution, risk-tier, or order-routing layers, so a headline can never
change a live strategy parameter.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from core.news_aggregator import WATCHLIST


class TbbFxSentimentEngine:
    """A small deterministic financial-news sentiment scorer.

    The scorer intentionally avoids a heavyweight model download during market
    hours.  Phrase weights cover the macro terms used by the TBBFX watchlist
    and the final value is always clamped to the inclusive [-1.0, 1.0] range.
    """

    _PHRASE_WEIGHTS = {
        "safe haven": 0.75,
        "rate cut": 0.55,
        "easing cycle": 0.65,
        "soft landing": 0.55,
        "strong demand": 0.50,
        "upside surprise": 0.55,
        "beat estimates": 0.50,
        "risk appetite": 0.45,
        "geopolitical escalation": -0.85,
        "supply disruption": -0.80,
        "military strike": -0.90,
        "trade restrictions": -0.65,
        "rate hike": -0.50,
        "inflation shock": -0.75,
        "liquidity stress": -0.80,
        "credit stress": -0.75,
        "risk off": -0.60,
    }

    _TOKEN_WEIGHTS = {
        "bullish": 0.45,
        "rally": 0.35,
        "growth": 0.28,
        "recovery": 0.35,
        "resilient": 0.25,
        "supportive": 0.25,
        "improve": 0.20,
        "bearish": -0.45,
        "selloff": -0.45,
        "panic": -0.70,
        "recession": -0.55,
        "crisis": -0.70,
        "escalation": -0.55,
        "restrictions": -0.42,
        "volatility": -0.20,
        "uncertainty": -0.30,
    }

    _SEVERITY_WEIGHTS = {
        "critical": 10.0,
        "high": 7.0,
        "medium": 4.0,
        "low": 2.0,
        "neutral": 1.0,
    }

    def analyze_text(self, text: str) -> float:
        """Return deterministic sentiment polarity in the [-1.0, 1.0] range."""
        normalized = re.sub(r"\s+", " ", str(text or "").lower()).strip()
        if not normalized:
            return 0.0

        score = 0.0
        matches = 0
        for phrase, weight in self._PHRASE_WEIGHTS.items():
            occurrences = normalized.count(phrase)
            if occurrences:
                score += weight * occurrences
                matches += occurrences

        tokens = re.findall(r"[a-z]+", normalized)
        for token in tokens:
            weight = self._TOKEN_WEIGHTS.get(token)
            if weight is not None:
                score += weight
                matches += 1

        if not matches:
            return 0.0
        # tanh prevents a long headline from escaping the contract range.
        return max(-1.0, min(1.0, math.tanh(score / max(1.0, matches * 0.62))))

    def weighted_sentiment(
        self,
        stories: Iterable[Dict[str, Any]],
        symbols: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        """Compile severity-weighted scores, with provenance-safe metadata."""
        requested = tuple(symbols or WATCHLIST)
        buckets: Dict[str, Dict[str, Any]] = {
            symbol: {"weighted": 0.0, "weight": 0.0, "stories": 0, "sources": set()}
            for symbol in requested
        }

        for story in stories:
            text = " ".join(
                str(story.get(field, ""))
                for field in ("title", "headline", "summary", "context")
            )
            polarity = self.analyze_text(text)
            severity = str(story.get("severity", "neutral")).lower()
            impact_weight = self._SEVERITY_WEIGHTS.get(severity, 1.0)
            impact_weight = max(1.0, min(10.0, float(story.get("impact_weight", impact_weight) or impact_weight)))
            impacted = [str(value).upper() for value in (story.get("symbols") or [])]

            for symbol in requested:
                if symbol not in impacted:
                    continue
                bucket = buckets[symbol]
                bucket["weighted"] += polarity * impact_weight
                bucket["weight"] += impact_weight
                bucket["stories"] += 1
                source = str(story.get("provider") or story.get("source") or "unknown")
                bucket["sources"].add(source)

        results: List[Dict[str, Any]] = []
        for symbol in requested:
            bucket = buckets[symbol]
            score = bucket["weighted"] / bucket["weight"] if bucket["weight"] else 0.0
            score = max(-1.0, min(1.0, score))
            stance = "bullish" if score > 0.30 else "bearish" if score < -0.30 else "neutral"
            results.append(
                {
                    "symbol": symbol,
                    "sentiment_polarity": round(score, 4),
                    "weighted_sentiment_score": round(score, 4),
                    "stance": stance,
                    "story_count": bucket["stories"],
                    "severity_weight_total": round(bucket["weight"], 2),
                    "sources": sorted(bucket["sources"]),
                }
            )

        generated_at = datetime.now(timezone.utc).isoformat()
        return {
            "status": "available",
            "as_of": generated_at,
            "last_updated": generated_at,
            "method": "deterministic_weighted_lexicon_v1",
            "provider": "tbbfx_news_aggregator",
            "source_frequency": "NEWS_INTRADAY",
            "refresh_cadence_seconds": 60,
            "symbols": results,
            "read_only": True,
            "advisory_only": True,
            "execution_mutation_allowed": False,
            "generated_at": generated_at,
        }
