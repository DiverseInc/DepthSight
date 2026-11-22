# api/plans.py
import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


class PlansConfig:
    def __init__(self, config_path: Path):
        self._config_path = Path(config_path)
        self._plans = {}
        self._billing = {}
        self._referral_program = {}
        self._affiliate_program = {}
        self._block_restrictions = {}
        self._registration_trial = {}
        self._plan_aliases = {}
        self._deferred_permissions = set()
        self._last_mtime_ns = -1
        self._load_config(force=True)

    def _get_config_mtime_ns(self) -> int:
        return self._config_path.stat().st_mtime_ns

    def _load_config(self, force: bool = False) -> None:
        try:
            current_mtime_ns = self._get_config_mtime_ns()
            if not force and current_mtime_ns == self._last_mtime_ns:
                return

            with open(self._config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}

            self._plans = config.get("plans", {})
            self._billing = config.get("billing", {})
            self._referral_program = config.get("referral_program", {})
            self._affiliate_program = config.get("affiliate_program", {})
            self._block_restrictions = config.get("block_restrictions", {})
            self._registration_trial = config.get("registration_trial", {})
            self._plan_aliases = config.get("plan_aliases", {}) or {}
            self._deferred_permissions = set(config.get("deferred_permissions", []) or [])
            self._last_mtime_ns = current_mtime_ns

            if self._plans:
                logger.info(
                    f"Successfully loaded {len(self._plans)} plans from {self._config_path}"
                )
            else:
                logger.warning(
                    f"Plans config file loaded from {self._config_path}, but no plans were found inside."
                )

            if self._referral_program:
                logger.info(
                    f"Successfully loaded referral program config from {self._config_path}"
                )
            else:
                logger.warning(
                    f"Referral program config not found in {self._config_path}."
                )
        except FileNotFoundError:
            logger.error(
                f"FATAL: Plans config file not found at path: {self._config_path}"
            )
        except Exception as e:
            logger.error(
                f"Error loading or parsing plans config file: {e}", exc_info=True
            )

    def _reload_if_changed(self) -> None:
        self._load_config(force=False)

    def resolve_plan_key(self, plan_name: str) -> str:
        """Return the canonical plan key for `plan_name`.

        A legacy key -- one that is still stored on a user row or on a seeded
        strategy template -- is mapped onto the plan that carries that tier
        today. The alias is NOT a plan of its own: it is never sold and it
        never appears in the catalog.

        An unknown key is returned unchanged so the caller can fail closed on
        it. A self-referencing or looping alias chain stops at the first
        repeat, so a broken config can never hang a request.
        """
        self._reload_if_changed()
        key = str(plan_name or "").strip().lower()
        seen: set[str] = set()
        while key in self._plan_aliases and key not in seen:
            seen.add(key)
            key = str(self._plan_aliases[key]).strip().lower()
        return key

    def get_plan_aliases(self) -> dict:
        self._reload_if_changed()
        return dict(self._plan_aliases)

    def get_deferred_permissions(self) -> set:
        """Permissions that a tier may hold but must never advertise."""
        self._reload_if_changed()
        return set(self._deferred_permissions)

    def get_plan(self, plan_name: str) -> dict:
        self._reload_if_changed()
        key = self.resolve_plan_key(plan_name)
        return self._plans.get(key, {"permissions": [], "quotas": {}})

    def get_all_plans(self) -> dict:
        """The canonical plans. Aliases are deliberately NOT in here."""
        self._reload_if_changed()
        return self._plans

    def get_billing_config(self) -> dict:
        self._reload_if_changed()
        return self._billing

    def get_billing_mode(self) -> str:
        self._reload_if_changed()
        mode = str(self._billing.get("mode", "monthly")).lower()
        return mode if mode in {"monthly", "lifetime"} else "monthly"

    def get_plan_billing(self, plan_name: str, mode: str | None = None) -> dict:
        self._reload_if_changed()
        selected_mode = mode or self.get_billing_mode()
        plan = self._plans.get(self.resolve_plan_key(plan_name), {})
        return plan.get("billing", {}).get(selected_mode, {})

    def is_billing_mode_available(self, plan_name: str, mode: str | None = None) -> bool:
        """True when this plan may be PURCHASED in the given billing mode.

        The catalog declares, per billing mode, whether a tier is sold at
        all (`billing.<mode>.enabled`). A mode that is switched off must
        never be purchasable: `get_effective_plan_price` falls back to the
        MONTHLY price when the mode carries no price of its own, so without
        this check a purchase in the switched-off mode would create a
        ONE-OFF invoice for a tier that has no one-off price, and the
        customer would be granted the monthly period for it.

        A mode block with no `enabled` key is ENABLED -- the monthly blocks
        are written that way -- and a plan with no block for the mode at all
        is left to the caller, which decides what to do with a plan it
        cannot price.
        """
        self._reload_if_changed()
        selected_mode = mode or self.get_billing_mode()
        billing = self.get_plan_billing(plan_name, selected_mode)
        return bool(billing.get("enabled", True))

    def get_effective_plan_price(self, plan_name: str) -> float:
        self._reload_if_changed()
        mode_billing = self.get_plan_billing(plan_name)
        plan = self._plans.get(self.resolve_plan_key(plan_name), {})
        return float(mode_billing.get("price_usd", plan.get("price_usd", 0)))

    def get_lifetime_reservation_ttl_seconds(self) -> int:
        self._reload_if_changed()
        lifetime_config = self._billing.get("lifetime", {})
        return int(lifetime_config.get("reservation_ttl_seconds", 900))

    def get_referral_bonus_config(self) -> dict:
        self._reload_if_changed()
        return self._referral_program

    def get_affiliate_config(self) -> dict:
        self._reload_if_changed()
        return self._affiliate_program

    def get_block_restrictions(self) -> dict:
        self._reload_if_changed()
        return self._block_restrictions

    def get_registration_trial_config(self) -> dict:
        self._reload_if_changed()
        config = self._registration_trial or {}

        try:
            days = int(config.get("days", 0))
        except (TypeError, ValueError):
            # Fail CLOSED. A malformed value used to fall back to 7 days,
            # which silently granted a paid trial nobody promised. A trial is
            # free product: if the number cannot be read, there is no trial.
            logger.warning(
                "Invalid registration_trial.days value in %s. Treating it as 0 "
                "(no trial).",
                self._config_path,
            )
            days = 0

        return {
            "enabled": bool(config.get("enabled", False)),
            "plan": str(config.get("plan", "standard") or "standard"),
            "days": max(days, 0),
        }


CONFIG_FILE_PATH = Path(__file__).parent / "plans_config.yml"

plans_config = PlansConfig(CONFIG_FILE_PATH)


# --- Public plan catalog ---------------------------------------------------
#
# The marketing pricing page and the in-app billing page MUST never disagree
# about what a tier contains. Both are served from this one builder, which
# reads plans_config.yml, so there is exactly one source of truth and a tier
# cannot drift away from what the backend actually grants.
#
# Everything the page shows about a tier is DERIVED from the machine-readable
# `quotas`, `permissions` and `limits` in plans_config.yml -- never from the
# free-text `features` copy, which is written by hand and can (and did) drift.

# quota key -> (subject, period) used to build the entitlement sentence.
_QUOTA_ENTITLEMENTS = (
    ("run_vector_backtest_per_day", "backtest", "day"),
    ("run_kline_backtest_per_day", "kline backtest", "day"),
    ("use_ai_assistant_per_day", "AI Assistant query", "day"),
    ("run_portfolio_backtest_per_day", "portfolio backtest", "day"),
    ("run_optimization_per_month", "optimization run", "month"),
    ("run_genetic_search_per_month", "genetic search", "month"),
    ("generate_dataset_per_month", "dataset generation", "month"),
    ("train_model_per_month", "model training", "month"),
)

# permission -> what the customer calls that capability.
_PERMISSION_LABELS = {
    "run_backtest": "Strategy backtesting",
    "run_portfolio_backtest": "Portfolio backtesting",
    "run_optimization": "Strategy optimization",
    "run_genetic_search": "Genetic strategy search",
    "run_simulation": "Portfolio simulation",
    "generate_dataset": "Dataset generation",
    "train_model": "ML Model Laboratory",
    "save_strategy_config": "Saved strategies",
    "use_ai_assistant": "AI Assistant",
}

# Plans that are allowed to use the `pro_only` strategy blocks.
# Mirrors api/ai_assistant.py and api/dependencies.py.
_PRO_BLOCK_PLANS = ("pro", "managed", "institutional")


def plan_has_pro_block_access(plan_key: str) -> bool:
    """True when this plan key may use the `pro_only` strategy blocks.

    The key is resolved first, so a legacy alias (`premium`) is answered the
    same way as the canonical plan it points at.
    """
    return plans_config.resolve_plan_key(plan_key) in _PRO_BLOCK_PLANS


def _pluralise(subject: str, count: int) -> str:
    if count == 1:
        return subject
    if subject.endswith("y"):
        return f"{subject[:-1]}ies"
    if subject.endswith(("ch", "sh", "s", "x")):
        return f"{subject}es"
    return f"{subject}s"


def _quota_sentence(subject: str, period: str, value: int) -> str:
    if value == -1:
        # The unlimited sentence must use the SAME plural rule as every other
        # sentence. A naive `subject + "s"` printed "AI Assistant querys" and
        # "genetic searchs" on the pricing card.
        return f"Unlimited {_pluralise(subject, 2)}"
    return f"{value} {_pluralise(subject, value)} per {period}"


def _duration_sentence(days: int) -> str:
    if days == -1:
        return "Unlimited backtest history"
    if days >= 365:
        years = days / 365
        if years == int(years):
            return f"{int(years)}-year backtest history"
        return f"{days}-day backtest history"
    return f"{days}-day backtest history"


def derive_entitlements(
    plan_key: str,
    plan_config: dict,
    all_plans: dict | None = None,
    deferred_permissions: set | None = None,
) -> list[dict]:
    """Build the list of things this tier does and does not include.

    Returns a list of `{"code", "label", "included"}` entries. `included` is
    False for a capability the tier genuinely lacks -- the pricing page shows
    those under "Not included" rather than quietly omitting them.
    """
    quotas = plan_config.get("quotas", {}) or {}
    permissions = list(plan_config.get("permissions", []) or [])
    limits = plan_config.get("limits", {}) or {}
    all_plans = all_plans or {}
    if deferred_permissions is None:
        deferred_permissions = plans_config.get_deferred_permissions()

    entries: list[dict] = []

    # Quotas: what is included, and what another tier offers but this one does not.
    for quota_key, subject, period in _QUOTA_ENTITLEMENTS:
        value = quotas.get(quota_key, 0)
        code = quota_key.replace("_per_", "_")
        if value and value != -1:
            entries.append(
                {
                    "code": code,
                    "label": _quota_sentence(subject, period, int(value)),
                    "included": True,
                }
            )
        elif value == -1:
            entries.append(
                {"code": code, "label": _quota_sentence(subject, period, -1), "included": True}
            )
        else:
            offered_elsewhere = any(
                (other.get("quotas", {}) or {}).get(quota_key, 0) not in (0, None)
                for other in all_plans.values()
                if other is not plan_config
            )
            if offered_elsewhere:
                entries.append(
                    {
                        "code": code,
                        "label": f"{_pluralise(subject, 2).capitalize()}",
                        "included": False,
                    }
                )

    # Capabilities expressed as permissions.
    for permission, label in _PERMISSION_LABELS.items():
        code = f"permission_{permission}"
        if permission in deferred_permissions:
            # The tier may HOLD this capability, but it is an unfinished
            # scaffold, so no tier advertises it -- not as included, and not
            # as "Not included" either.
            continue
        if permission in permissions:
            if permission == "use_ai_assistant":
                # The AI Assistant quota is already shown above; don't repeat it.
                continue
            entries.append({"code": code, "label": label, "included": True})
        else:
            offered_elsewhere = any(
                permission in (other.get("permissions", []) or [])
                for other in all_plans.values()
            )
            if offered_elsewhere:
                entries.append({"code": code, "label": label, "included": False})

    # Live trading + how many strategies it allows.
    #
    # A tier can be allowed to trade live in two different ways, and the
    # catalog has to tell them apart:
    #
    #   * `allow_real_trading` -- live trading on every venue the platform
    #     routes to;
    #   * `allow_free_bybit_trading` -- live trading, but on Bybit ONLY. The
    #     enforcement is real: api/routes/strategies.py:541-549 answers any
    #     other venue with a 403, and api/depthsight_api.py:638-641 caps the
    #     tier at `max_free_bybit_live_strategies` live strategies.
    #
    # Before this branch existed, a Bybit-only tier was published as "Live
    # trading: not included", which is false -- and the public pricing page
    # renders the not-included entries under the heading "Not included"
    # (frontend/src/components/marketing/Pricing.tsx:104-119).
    if limits.get("allow_real_trading"):
        entries.append(
            {"code": "live_trading", "label": "Live trading on OKX, Binance, Bybit", "included": True}
        )
    elif limits.get("allow_free_bybit_trading"):
        entries.append(
            {"code": "live_trading", "label": "Live trading on Bybit", "included": True}
        )
    else:
        entries.append({"code": "live_trading", "label": "Live trading", "included": False})

    max_live = int(limits.get("max_live_strategies", 0) or 0)
    if max_live > 0:
        entries.append(
            {
                "code": "live_strategies",
                "label": f"{max_live} live trading strategies",
                "included": True,
            }
        )
    elif limits.get("allow_free_bybit_trading"):
        # The Bybit-only tier's own cap. The default is 1 because that is the
        # cap the enforcement uses when the limit is absent
        # (api/depthsight_api.py:641); the number is never written here.
        bybit_max_live = int(limits.get("max_free_bybit_live_strategies", 1) or 0)
        if bybit_max_live > 0:
            entries.append(
                {
                    "code": "live_strategies",
                    "label": f"{bybit_max_live} live trading strategies on Bybit",
                    "included": True,
                }
            )
        else:
            entries.append(
                {"code": "live_strategies", "label": "Live trading strategies", "included": False}
            )
    else:
        entries.append(
            {"code": "live_strategies", "label": "Live trading strategies", "included": False}
        )

    # Backtest history window.
    max_days = int(limits.get("max_backtest_duration_days", 0) or 0)
    if max_days > 0 or max_days == -1:
        entries.append(
            {
                "code": "history",
                "label": _duration_sentence(max_days),
                "included": True,
            }
        )

    # PRO strategy blocks.
    has_pro_blocks = plan_has_pro_block_access(plan_key)
    entries.append(
        {
            "code": "pro_blocks",
            "label": "PRO blocks (Tape, Book, Open Interest)",
            "included": has_pro_blocks,
        }
    )

    # Standard strategy blocks are available on every tier.
    entries.append(
        {"code": "standard_blocks", "label": "Standard blocks (Logic, Indicators, Proximity)", "included": True}
    )

    # Paper trading: available wherever the plan can trade at all.
    entries.append(
        {
            "code": "paper_trading",
            "label": "Paper trading",
            "included": True,
        }
    )

    return entries


def build_plan_catalog(include_inactive: bool = False) -> dict:
    """Build the full plan catalog used by BOTH the pricing page and billing.

    Returns:
        {
          "plans": [ ... one entry per plan ... ],
          "trial": {"enabled", "days", "plan", "active"},
          "billing_mode": "monthly" | "lifetime",
        }

    `trial.active` is the important field: a registration trial only exists
    when it is enabled AND lasts more than zero days. The pricing page uses
    it to decide whether it may promise a trial at all.
    """
    all_plans = plans_config.get_all_plans()
    billing_mode = plans_config.get_billing_mode()
    trial_config = plans_config.get_registration_trial_config()

    plan_entries = []
    for key, config in all_plans.items():
        if not include_inactive and not config.get("active", False):
            continue

        # A tier the server REFUSES to sell in the ACTIVE billing mode must
        # not be published. The public pricing API returns this catalog
        # verbatim (api/routes/public.py:25-36), so a tier listed here is
        # advertised on the pricing page -- while the purchase endpoint
        # answers a purchase of it with HTTP 400
        # (api/routes/payments.py:115-122).
        #
        # The $499 Managed tier is the case in point: it is a monthly
        # service and its one-off block is switched off
        # (`billing.lifetime.enabled: false`), so in a one-off deployment it
        # is not sold at all. A tier that cannot be bought must not be
        # advertised.
        if not plans_config.is_billing_mode_available(key, billing_mode):
            continue

        plan_billing = config.get("billing", {}) or {}
        effective_price = plans_config.get_effective_plan_price(key)

        plan_entries.append(
            {
                "key": key,
                "name": config.get("name", "Unnamed Plan"),
                "description": config.get("description", ""),
                "price_usd": effective_price,
                "active": bool(config.get("active", False)),
                "billing_mode": billing_mode,
                "billing": {
                    "monthly": plan_billing.get("monthly", {}) or {},
                    "lifetime": plan_billing.get("lifetime", {}) or {},
                },
                # Kept for the in-app billing modal. The marketing page does NOT
                # use this copy -- it uses the derived entitlements below.
                "features": config.get("features", []) or [],
                # A curated HEADLINE for the pricing card. Some tiers (the
                # managed service) hold so many entitlements that the derived
                # label list is unreadable, so the card shows a short
                # hand-written summary instead. It still lives in the CATALOG,
                # so the page is still catalog-sourced, and the pricing test
                # asserts every quantified claim in it against the raw plan.
                "headline_bullets": config.get("headline_bullets", []) or [],

                "quotas": config.get("quotas", {}) or {},
                "permissions": config.get("permissions", []) or [],
                "limits": config.get("limits", {}) or {},
                "entitlements": derive_entitlements(key, config, all_plans),
            }
        )

    return {
        "plans": plan_entries,
        "trial": {
            "enabled": bool(trial_config.get("enabled", False)),
            "days": int(trial_config.get("days", 0) or 0),
            "plan": trial_config.get("plan"),
            # A trial with zero days is not a trial: the backend gives a new
            # account the `free` plan (see api/crud.py create_user).
            "active": bool(trial_config.get("enabled", False))
            and int(trial_config.get("days", 0) or 0) > 0,
        },
        "billing_mode": billing_mode,
    }
