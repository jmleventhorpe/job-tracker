"""Score how well each new posting fits the profile in config.toml, using Claude.

Runs only on postings that passed the hard filters and are not in the seen
store, so each posting is scored once.
"""

from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import anthropic

from .config import ScoringConfig
from .models import Fatal, Posting

# Models that accept server-side refusal fallbacks. On these, a rare refusal
# is re-run on another model inside the same call instead of failing.
FALLBACK_MODELS = {
    "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5",
    "claude-fable-5-2", "claude-fable-5-1",
}
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# USD per million tokens (input, output), for the run log's cost estimate.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}

INSTRUCTIONS = """\
You screen job adverts for one person. Their profile is below. For each \
advert, judge how well it fits that person: discipline, seniority, software \
and location preferences all count. Be strict: a role from a different \
discipline that only shares a word in its title (for example an FX TD for a \
lighting TD) is a poor fit.

Score from 0 to 10:
- 9-10: squarely what they are looking for
- 6-8: a reasonable fit worth applying to
- 3-5: adjacent; only worth a look if little else is around
- 0-2: not a fit

When a job description is included, use it: required seniority and years of \
experience, the software and tools named, the actual day-to-day work, and \
any mention of visa sponsorship or relocation support (which offsets the \
location adjustment for that role by up to 2 points). Without one, judge from \
the other fields.

Give the single best discipline from the allowed list, and a reason of one \
short sentence (under 25 words) that names the deciding factor.

<profile>
{profile}
</profile>"""


def fingerprint(cfg: ScoringConfig) -> str:
    """Changes whenever anything that affects a score changes, so earlier
    rejections can be scored again under the new settings."""
    parts = [cfg.model, cfg.effort, str(cfg.min_score), "|".join(cfg.disciplines),
             cfg.profile.strip(), INSTRUCTIONS]
    return hashlib.sha256("\n\x1e".join(parts).encode()).hexdigest()[:16]


@dataclass
class Score:
    score: int
    discipline: str
    reason: str


def _schema(disciplines: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "score": {"type": "integer"},
            "discipline": {"type": "string", "enum": disciplines},
            "reason": {"type": "string"},
        },
        "required": ["score", "discipline", "reason"],
        "additionalProperties": False,
    }


def describe(p: Posting) -> str:
    lines = [
        f"Role: {p.role}",
        f"Studio: {p.studio}",
        f"Location: {p.location or 'not given'}",
        f"Work mode: {p.work_mode or 'not given'}",
        f"Experience level: {p.experience or 'not given'}",
        f"Software: {p.software or 'not given'}",
        f"Date posted: {p.date_issued or 'not given'}",
    ]
    if p.description:
        lines.append(f"\n<job_description>\n{p.description}\n</job_description>")
    return "\n".join(lines)


class Scorer:
    def __init__(self, cfg: ScoringConfig, client: anthropic.Anthropic | None = None):
        self.cfg = cfg
        self.client = client or anthropic.Anthropic(max_retries=4)
        self.system = INSTRUCTIONS.format(profile=cfg.profile.strip())
        self.input_tokens = 0
        self.output_tokens = 0
        self._lock = threading.Lock()

    @property
    def cost(self) -> float | None:
        """Estimated USD spent this run, or None for a model without a price."""
        price = PRICES.get(self.cfg.model)
        if not price:
            return None
        return (self.input_tokens * price[0] + self.output_tokens * price[1]) / 1e6

    def score(self, posting: Posting) -> Score | None:
        """Score one posting. None means it could not be scored this time
        (rate limit, outage, refusal); the caller falls back to keywords."""
        kwargs: dict = dict(
            model=self.cfg.model,
            max_tokens=4000,
            system=self.system,
            messages=[{"role": "user", "content": describe(posting)}],
            output_config={
                "effort": self.cfg.effort,
                "format": {"type": "json_schema",
                           "schema": _schema(self.cfg.disciplines)},
            },
        )
        if self.cfg.model in FALLBACK_MODELS:
            kwargs.update(betas=[FALLBACK_BETA], fallbacks="default")
        try:
            resp = self.client.beta.messages.create(**kwargs)
        except TypeError as exc:
            # The SDK raises TypeError when it finds no credentials at all.
            if "authentication" not in str(exc):
                raise
            raise Fatal(
                "No Anthropic API key found. Set ANTHROPIC_API_KEY in .env, "
                "run with --no-score, or set [scoring] enabled = false."
            ) from None
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise Fatal(
                "Anthropic API rejected the credentials. Set ANTHROPIC_API_KEY in "
                f".env, or set [scoring] enabled = false. ({exc.message})"
            ) from None
        except (anthropic.BadRequestError, anthropic.NotFoundError) as exc:
            raise Fatal(
                f"Anthropic API rejected the request; check [scoring] model and "
                f"effort in config.toml. ({exc.message})"
            ) from None
        except (anthropic.RateLimitError, anthropic.APIConnectionError,
                anthropic.APIStatusError) as exc:
            print(f"  warning: could not score '{posting.role}': {exc}")
            return None

        with self._lock:
            self.input_tokens += resp.usage.input_tokens
            self.output_tokens += resp.usage.output_tokens
        if resp.stop_reason != "end_turn":
            print(f"  warning: could not score '{posting.role}' "
                  f"(stop reason: {resp.stop_reason})")
            return None
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            print(f"  warning: unreadable score for '{posting.role}'")
            return None
        return Score(
            score=max(0, min(10, int(data["score"]))),
            discipline=data["discipline"],
            reason=data["reason"].strip(),
        )

    def score_all(self, postings: list[Posting]) -> dict[tuple[str, str], Score | None]:
        if not postings:
            return {}
        print(f"Scoring {len(postings)} new postings with {self.cfg.model}...")
        with ThreadPoolExecutor(max_workers=self.cfg.max_workers) as pool:
            results = list(pool.map(self.score, postings))
        print(f"  used {self.input_tokens:,} input / {self.output_tokens:,} output tokens")
        return {p.key: s for p, s in zip(postings, results)}
