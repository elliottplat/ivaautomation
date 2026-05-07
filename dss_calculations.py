"""
DSS Workload Calculations
=========================
Pure functions — no database, no HTTP. All inputs are plain Python dicts/lists.
"""


def conversion_factor(task_rate_per_hour: float, base_rate_per_hour: float) -> float:
    """Returns base_rate / task_rate. DocuWare(15)/Spreadsheet(30) = 0.5"""
    if task_rate_per_hour <= 0:
        raise ValueError("task_rate_per_hour must be > 0")
    return base_rate_per_hour / task_rate_per_hour


def shift_metrics(hours_worked: float, completions: list, base_rate: float) -> dict:
    """
    completions: [{"count": N, "conversion_factor": F}, ...]
    Returns: {target_units, actual_units, pct_target_hit, status}
    status: "On Track" | "Below Target" | None (if hours=0)
    """
    target_units = hours_worked * base_rate
    actual_units = sum(c["count"] * c["conversion_factor"] for c in completions)
    if hours_worked == 0:
        return {"target_units": 0, "actual_units": actual_units, "pct_target_hit": None, "status": None}
    pct = actual_units / target_units if target_units > 0 else None
    status = "On Track" if pct is not None and pct >= 1.0 else "Below Target"
    return {
        "target_units": round(target_units, 2),
        "actual_units": round(actual_units, 2),
        "pct_target_hit": round(pct, 4) if pct is not None else None,
        "status": status,
    }


def rolling_avg_pct(shift_history: list, n_days: int = 7):
    """
    shift_history: list of {work_date, pct_target_hit} for an agent, sorted newest first.
    Only includes dates where hours_worked > 0 (caller filters before passing in).
    Returns mean of pct_target_hit for last n_days worked dates, or None if no data.
    """
    worked = [s for s in shift_history if s.get("pct_target_hit") is not None][:n_days]
    if not worked:
        return None
    return round(sum(s["pct_target_hit"] for s in worked) / len(worked), 4)


def daily_team_rollup(
    landed_units: float,
    completed_units: float,
    team_capacity: float,
    prior_backlog: float,
) -> dict:
    """
    Returns: {backlog_change, running_backlog, landed_units, completed_units, team_capacity}
    """
    backlog_change = landed_units - completed_units
    running_backlog = prior_backlog + backlog_change
    return {
        "landed_units": round(landed_units, 2),
        "completed_units": round(completed_units, 2),
        "team_capacity": round(team_capacity, 2),
        "backlog_change": round(backlog_change, 2),
        "running_backlog": round(running_backlog, 2),
    }


def days_of_work(running_backlog: float, avg_daily_capacity: float):
    if avg_daily_capacity <= 0:
        return None
    return round(running_backlog / avg_daily_capacity, 2)


def sla_status(days_of_work_value, threshold: int) -> str:
    if days_of_work_value is None:
        return "⚠️ No Data"
    return "✅ SLA OK" if days_of_work_value <= threshold else "❌ SLA Breached"


def hiring_trigger(sla_history: list, threshold_days: int) -> bool:
    """Returns True if the last threshold_days consecutive days were all SLA Breached."""
    if len(sla_history) < threshold_days:
        return False
    recent = sla_history[-threshold_days:]
    return all(s == "❌ SLA Breached" for s in recent)
