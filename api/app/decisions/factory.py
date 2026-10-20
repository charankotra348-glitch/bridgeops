from app.config import Settings, get_settings
from app.decisions.abstain import AbstainProvider
from app.decisions.base import DecisionProvider
from app.decisions.gemini import GeminiProvider
from app.decisions.jev import JevProvider

PROVIDERS = ("rules", "gemini", "jev")


def get_provider(settings: Settings | None = None, name: str | None = None) -> DecisionProvider:
    settings = settings or get_settings()
    name = name or settings.decision_provider
    if name == "jev":
        return JevProvider(api_key=settings.typesafe_api_key, url=settings.typesafe_api_url,
                           model=settings.jev_model, timeout_s=settings.jev_timeout_s)
    if name == "gemini":
        return GeminiProvider(api_key=settings.gemini_api_key, model=settings.gemini_model,
                              timeout_s=settings.gemini_timeout_s)
    if name == "rules":
        return AbstainProvider()
    raise ValueError(f"Unknown decision provider: {name}")
