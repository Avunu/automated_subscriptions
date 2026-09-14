"""Pure anchored-billing date math. No frappe.db, no frappe.local."""

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

Interval = Literal["Month", "Year"]


@dataclass(frozen=True)
class Grid:
	anchor: date  # any occurrence of the anchor; only (month, day) matters for Year, only day for Month
	interval: Interval
	count: int = 1  # >= 1


def clamp(year: int, month: int, day: int) -> date:
	return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def occurrence(grid: Grid, k: int) -> date:
	"""k-th anchor occurrence, k in Z (negative allowed). Always computed from the anchor, never iterated."""
	if grid.interval == "Year":
		return clamp(grid.anchor.year + k * grid.count, grid.anchor.month, grid.anchor.day)
	m = grid.anchor.year * 12 + (grid.anchor.month - 1) + k * grid.count
	return clamp(m // 12, m % 12 + 1, grid.anchor.day)


def term_bounds(grid: Grid, d: date) -> tuple[date, date]:
	"""The anchored term containing d: (occurrence(k), occurrence(k+1) - 1 day) with
	occurrence(k) <= d < occurrence(k+1)."""
	if grid.interval == "Year":
		k = (d.year - grid.anchor.year) // grid.count
	else:
		k = ((d.year * 12 + d.month - 1) - (grid.anchor.year * 12 + grid.anchor.month - 1)) // grid.count
	# correct by at most one step in each direction (clamping can move an occurrence past d)
	while occurrence(grid, k) > d:
		k -= 1
	while occurrence(grid, k + 1) <= d:
		k += 1
	return occurrence(grid, k), occurrence(grid, k + 1) - timedelta(days=1)


def period_end(grid: Grid, d: date) -> date:
	"""Day before the first anchor occurrence strictly after d. A date on the boundary gets a full term."""
	return term_bounds(grid, d)[1]


def prorate_factor(
	grid: Grid, start: date, end: date, units_per_term: int = 1, discount_pct: float = 0, prorate: bool = True
) -> float:
	"""(billed_days / term_days) * units_per_term * (1 - discount). Full terms short-circuit to an exact value."""
	if start > end:
		raise ValueError(f"start {start} after end {end}")
	term_start, term_end = term_bounds(grid, start)
	if not (term_start <= start <= end <= term_end):
		raise ValueError(f"{start}..{end} is not inside one anchored term {term_start}..{term_end}")
	if not prorate or (start == term_start and end == term_end):
		return units_per_term * (100 - discount_pct) / 100
	billed_days = (end - start).days + 1
	term_days = (term_end - term_start).days + 1
	return units_per_term * (100 - discount_pct) * billed_days / (100 * term_days)


def resolve_grid(
	anchor: date, customer_interval: str, plan_interval: str, plan_count: int, annual_discount_pct: float
) -> tuple[Grid, int, float]:
	"""Returns (grid, units_per_term, discount_pct). Raises ValueError for unsupported combinations
	(the Subscription mixin converts ValueError -> UnsupportedBillingGrid)."""
	plan_count = plan_count or 1
	if plan_interval not in ("Month", "Year"):
		raise ValueError(f"cannot anchor a {plan_interval} plan")
	if customer_interval == "Year" and plan_interval == "Month":
		if 12 % plan_count:
			raise ValueError(f"a {plan_count}-month plan does not divide a year")
		return Grid(anchor, "Year", 1), 12 // plan_count, annual_discount_pct
	return Grid(anchor, plan_interval, plan_count), 1, 0.0
