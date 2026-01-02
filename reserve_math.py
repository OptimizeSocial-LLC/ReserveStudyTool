# reserve_math.py
from __future__ import annotations
from typing import Dict, List, Tuple


def _build_component_state(components: List[dict]) -> List[dict]:
    """
    Normalize component fields + create per-component state for simulation.
    """
    st = []
    for c in components:
        qty = int(c.get("quantity", 1) or 1)
        ul = int(c["useful_life_years"])
        rl = int(c["remaining_life_years"])
        cyc = int(c.get("cycle_years") or ul)
        cost = float(c["current_replacement_cost"])

        ul = max(1, ul)
        cyc = max(1, cyc)
        rl = max(0, rl)

        # infer "age" within cycle: age = cycle - remaining (clamped)
        age = max(0, min(cyc, cyc - rl))

        st.append(
            {
                "name": c["name"],
                "qty": max(1, qty),
                "cycle": cyc,
                "age": age,
                "cost_today": cost,
            }
        )
    return st


def _simulate(
    *,
    start_year: int,
    horizon_years: int,
    inflation_rate: float,
    interest_rate: float,
    starting_balance: float,
    annual_contribution: float,
    components: List[dict],
    min_balance: float,
) -> Tuple[bool, List[Dict]]:
    """
    Simulate year-by-year:
      - Levelized annual contribution
      - Expenses from scheduled replacements (cycle-based)
      - Fully Funded Balance (FFB) = sum(component_cost_in_year * %deterioration)
      - Enforce: ending_balance >= min_balance and >= FFB (full funding target)
    Returns: (passes_constraints, yearly_rows)
    """
    st = _build_component_state(components)

    bal = float(starting_balance)
    rows: List[Dict] = []

    for i in range(horizon_years):
        year = start_year + i

        # inflation factor relative to year 0
        infl = (1.0 + float(inflation_rate)) ** i

        # Compute Fully Funded Balance (FFB) for this year
        # % deterioration approximated as age/cycle
        ffb = 0.0
        for c in st:
            pct = max(0.0, min(1.0, c["age"] / float(c["cycle"])))
            ffb += (c["qty"] * c["cost_today"] * infl) * pct

        start_bal = bal
        interest = start_bal * float(interest_rate)

        # Expenses: replace any component when age reaches cycle
        expenses = 0.0
        for c in st:
            # if it will hit replacement this year, expense at inflated cost
            if c["age"] >= c["cycle"]:
                expenses += c["qty"] * c["cost_today"] * infl
                # reset after replacement
                c["age"] = 0

        contrib = float(annual_contribution)
        end_bal = start_bal + contrib + interest - expenses

        # constraints
        if end_bal < float(min_balance) - 1e-9:
            ok = False
        elif end_bal < ffb - 1e-9:
            ok = False
        else:
            ok = True

        pct_funded = 0.0
        if ffb > 0:
            pct_funded = max(0.0, end_bal / ffb)

        rows.append(
            {
                "year": year,
                "starting_balance": start_bal,
                "recommended_contribution": annual_contribution,
                "contributions": contrib,
                "expenses": expenses,
                "interest_earned": interest,
                "ending_balance": end_bal,
                "fully_funded_balance": ffb,
                "percent_funded": pct_funded,
            }
        )

        # advance component ages to next year
        for c in st:
            c["age"] += 1

        bal = end_bal

        if not ok:
            return False, rows

    return True, rows


def recommend_levelized_full_funding_contribution(
    *,
    start_year: int,
    horizon_years: int,
    inflation_rate: float,
    interest_rate: float,
    starting_balance: float,
    components: List[dict],
    min_balance: float = 0.0,
) -> Tuple[float, List[Dict]]:
    """
    Find the smallest annual contribution that satisfies full-funding constraints:
      - ending balance >= min_balance
      - ending balance >= fully funded balance (FFB) each year

    Uses binary search.
    """
    # quick lower bound
    lo = 0.0

    # upper bound: something safely high based on total inflated replacement
    # (not perfect, but avoids “no solution” due to too-low hi)
    base_total = sum(float(c["current_replacement_cost"]) * int(c.get("quantity", 1) or 1) for c in components)
    hi = max(5000.0, base_total)  # start here
    hi *= 2.0

    # expand hi until it passes (or we hit a hard cap)
    for _ in range(20):
        ok, _rows = _simulate(
            start_year=start_year,
            horizon_years=horizon_years,
            inflation_rate=inflation_rate,
            interest_rate=interest_rate,
            starting_balance=starting_balance,
            annual_contribution=hi,
            components=components,
            min_balance=min_balance,
        )
        if ok:
            break
        hi *= 2.0

    # binary search
    best = hi
    best_rows: List[Dict] = []

    for _ in range(50):
        mid = (lo + hi) / 2.0
        ok, rows = _simulate(
            start_year=start_year,
            horizon_years=horizon_years,
            inflation_rate=inflation_rate,
            interest_rate=interest_rate,
            starting_balance=starting_balance,
            annual_contribution=mid,
            components=components,
            min_balance=min_balance,
        )
        if ok:
            best = mid
            best_rows = rows
            hi = mid
        else:
            lo = mid

    # round to cents for display/storage
    best = round(best, 2)

    # update recommended_contribution in rows to match rounded best
    if best_rows:
        for r in best_rows:
            r["recommended_contribution"] = best

    return best, best_rows



