# reserve_math.py
def run_simple_reserve_math(
    start_year: int,
    horizon_years: int,
    inflation_rate: float,
    interest_rate: float,
    components: list[dict],
    starting_balance: float,
    annual_contribution: float,
):
    """
    Placeholder reserve model:
    - Fixed annual contributions
    - Component replacements occur when remaining life hits 0 (scheduled once)
    - Replacement costs inflate over time
    - Interest earned on starting balance each year
    """
    expense_by_year = {}
    for c in components:
        replace_year = start_year + max(0, int(c["remaining_life_years"]))
        expense_by_year[replace_year] = expense_by_year.get(replace_year, 0.0) + float(c["current_replacement_cost"])

    results = []
    bal = float(starting_balance)

    for i in range(horizon_years):
        year = start_year + i
        start_bal = bal
        interest = start_bal * float(interest_rate)

        raw_exp = expense_by_year.get(year, 0.0)
        inflated_exp = raw_exp * ((1.0 + float(inflation_rate)) ** i)

        contrib = float(annual_contribution)
        end_bal = start_bal + contrib + interest - inflated_exp

        results.append({
            "year": year,
            "starting_balance": start_bal,
            "contributions": contrib,
            "expenses": inflated_exp,
            "interest_earned": interest,
            "ending_balance": end_bal,
        })
        bal = end_bal

    return results
