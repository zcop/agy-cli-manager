"""Pure cached quota evaluation; no network or account activation."""
import math
from datetime import datetime, timezone


def normalize_policy(raw=None):
    policy = {"configured": False, "enabled": True, "pools": [], "match": "any",
              "threshold_percent": 10.0, "max_age_seconds": 1800}
    if isinstance(raw, dict):
        policy.update({key: raw[key] for key in policy if key in raw})
    policy["pools"] = sorted(set(x for x in policy["pools"] if isinstance(x, str) and x.strip())) if isinstance(policy["pools"], list) else []
    if policy["match"] not in ("any", "all"):
        policy["match"] = "any"
    for key, default, minimum, maximum in (("threshold_percent", 10, 0, 100),
                                          ("max_age_seconds", 1800, 1, 86400)):
        try:
            value = float(policy[key])
            policy[key] = value if math.isfinite(value) and minimum <= value <= maximum else default
        except (TypeError, ValueError):
            policy[key] = default
    policy["configured"] = policy["configured"] is True
    policy["enabled"] = policy["enabled"] is True
    return policy


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo else None
    except (TypeError, ValueError, AttributeError):
        return None


def evaluate(meta, policy, now=None):
    """Return per-pool states and explicit trigger/replacement decisions.

    A pool blocks when any fresh, unexpired limit is at/below threshold.
    A pool is usable only when every reported limit is fresh and above it.
    """
    policy = normalize_policy(policy)
    now = now or datetime.now(timezone.utc)
    refreshed = timestamp(meta.get("quota_pools_refreshed_at"))
    fresh = refreshed is not None and 0 <= (now - refreshed).total_seconds() <= policy["max_age_seconds"]
    states = {}
    pools = meta.get("quota_pools") or {}
    for name in policy["pools"]:
        limits = pools.get(name) if isinstance(pools, dict) else None
        statuses = []
        for limit in limits if isinstance(limits, list) else []:
            if not isinstance(limit, dict):
                statuses.append("unknown")
                continue
            reset = timestamp(limit.get("reset_at"))
            value = limit.get("remaining_percent")
            if (not fresh or reset is None or reset <= now or isinstance(value, bool)
                    or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100):
                statuses.append("unknown")
            else:
                statuses.append("exhausted" if value <= policy["threshold_percent"] else "available")
        states[name] = ("exhausted" if "exhausted" in statuses else
                        "available" if statuses and all(s == "available" for s in statuses) else "unknown")
    enabled = policy["enabled"] and bool(states)
    any_rule = policy["match"] == "any"
    return {"pools": states, "fresh": fresh,
            "triggered": enabled and (any(s == "exhausted" for s in states.values()) if any_rule
                                      else all(s == "exhausted" for s in states.values())),
            "replacement_usable": enabled and (all(s == "available" for s in states.values()) if any_rule
                                                else any(s == "available" for s in states.values()))}
