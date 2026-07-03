"""Tests for the safe expression sandbox and Decimal money math.

Covers (REFACTOR_CONTRACT §1, §7, §8 / AUDIT C5, C7, H6, M4, M5, H1):
- Cross-SDK parity fixture (expression_cases).
- Sandbox-escape table (dunder/attribute/subscript/lambda/comprehension/
  f-string/walrus/starred) -> ExpressionError.
- ``**`` rejection, div/mod-by-zero -> error, non-finite -> error.
- Decimal precision (exact, no truncation, ROUND_HALF_UP).
- Helper arity/range errors (tier/percentile/clamp/if/min/max).
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from bursar.engine import _q
from bursar.expr import ExpressionError, evaluate_expression, validate_expression

# ── Parity fixture ─────────────────────────────────────────────────────────

_PARITY_PATH = Path(__file__).parent / "../../tests/parity/expression_cases.json"


def _load_parity() -> dict:
    with _PARITY_PATH.open() as f:
        return json.load(f)


_PARITY = _load_parity()
_EXPRESSION_CASES = _PARITY["expression_cases"]


@pytest.mark.parametrize("case", _EXPRESSION_CASES, ids=[c["name"] for c in _EXPRESSION_CASES])
def test_parity_expression_cases(case: dict) -> None:
    """Each fixture case: quantized result == expected (string) or raises."""
    expr = case["expr"]
    variables = case.get("vars", {})
    if case.get("expect_error"):
        with pytest.raises(ExpressionError):
            evaluate_expression(expr, variables)
    else:
        result = evaluate_expression(expr, variables)
        assert _q(result) == Decimal(case["expected"])
        # Byte-identical decimal string, quantized to 4dp.
        assert str(_q(result)) == case["expected"]


# ── Decimal precision / no truncation ──────────────────────────────────────


class TestDecimalPrecision:
    def test_point_one_plus_point_two_is_exact(self) -> None:
        result = evaluate_expression(
            "input_tokens * 0.1 + output_tokens * 0.2",
            {"input_tokens": 1, "output_tokens": 1},
        )
        assert result == Decimal("0.3")  # NOT 0.30000000000000004
        assert _q(result) == Decimal("0.3000")

    def test_sub_one_credit_not_truncated(self) -> None:
        # A 0.4-credit op charges 0.4, not 0 (revenue-leak guard, H1).
        result = evaluate_expression("input_tokens * 0.0004", {"input_tokens": 1000})
        assert _q(result) == Decimal("0.4000")

    def test_result_is_decimal_not_float(self) -> None:
        result = evaluate_expression("input_tokens * 5", {"input_tokens": 10})
        assert isinstance(result, Decimal)
        assert result == Decimal(50)

    def test_literals_parsed_exactly_from_string(self) -> None:
        # 1.1 is not exactly representable in binary float; Decimal('1.1') is.
        result = evaluate_expression("input_tokens * 1.1", {"input_tokens": 3})
        assert result == Decimal("3.3")

    def test_round_is_half_up_not_bankers(self) -> None:
        # Python's builtin round(2.5)==2 (bankers); ours is half-up -> 3.
        assert evaluate_expression("round(input_tokens * 0.0025)", {"input_tokens": 1000}) == Decimal("3")
        assert evaluate_expression("round(input_tokens)", {"input_tokens": Decimal("2.5")}) == Decimal("3")
        assert evaluate_expression("round(input_tokens, 2)", {"input_tokens": Decimal("3.14159")}) == Decimal("3.14")


# ── Sandbox escape table ────────────────────────────────────────────────────

_SANDBOX_ESCAPES = [
    ("dunder_name", "__import__"),
    ("dunder_proto", "__proto__ * 1"),
    ("constructor_ident", "constructor + input_tokens"),
    ("attribute_access", "input_tokens.__class__"),
    ("attribute_method", "input_tokens.bit_length"),
    ("subscript", "input_tokens[0]"),
    ("lambda_expr", "lambda x: x + 1"),
    ("list_comprehension", "[x for x in input_tokens]"),
    ("set_comprehension", "{x for x in input_tokens}"),
    ("dict_comprehension", "{x: x for x in input_tokens}"),
    ("generator_expr", "sum(x for x in input_tokens)"),
    ("fstring", 'f"{input_tokens}"'),
    ("walrus", "(x := input_tokens)"),
    ("starred", "max(*input_tokens)"),
    ("dict_literal", "{'a': 1}"),
    ("list_literal", "[1, 2, 3]"),
    ("import_call", "__import__('os')"),
    ("unknown_function", "evil_func(input_tokens)"),
    ("attribute_call", "input_tokens.method()"),
]


@pytest.mark.parametrize("name,expr", _SANDBOX_ESCAPES, ids=[n for n, _ in _SANDBOX_ESCAPES])
def test_sandbox_escapes_rejected_at_validate(name: str, expr: str) -> None:
    # With the known-variable set passed (as config-load does), bare dunder /
    # prototype-chain identifiers are rejected as unknown variables; structural
    # escapes (lambda/comprehension/subscript/...) are rejected by the AST allowlist.
    with pytest.raises(ExpressionError):
        validate_expression(expr, known_variables={"input_tokens"})


@pytest.mark.parametrize("name,expr", _SANDBOX_ESCAPES, ids=[n for n, _ in _SANDBOX_ESCAPES])
def test_sandbox_escapes_rejected_at_eval(name: str, expr: str) -> None:
    with pytest.raises(ExpressionError):
        evaluate_expression(expr, {"input_tokens": 5})


# ── Exponentiation rejection (C5) ───────────────────────────────────────────


class TestPowRejected:
    def test_simple_pow_rejected_validate(self) -> None:
        with pytest.raises(ExpressionError, match="exponentiation"):
            validate_expression("input_tokens ** 2")

    def test_pow_bomb_rejected(self) -> None:
        # 9 ** 9 ** 9 would allocate gigabytes; must be rejected pre-eval.
        with pytest.raises(ExpressionError):
            validate_expression("9 ** 9 ** 9")

    def test_pow_rejected_at_eval(self) -> None:
        with pytest.raises(ExpressionError):
            evaluate_expression("input_tokens ** 400", {"input_tokens": 1000})


# ── Division / modulo by zero, non-finite (C7) ──────────────────────────────


class TestDivModByZero:
    def test_division_by_zero_raises(self) -> None:
        with pytest.raises(ExpressionError, match="division or modulo by zero"):
            evaluate_expression("input_tokens / 0", {"input_tokens": 5})

    def test_division_by_zero_variable_raises(self) -> None:
        with pytest.raises(ExpressionError):
            evaluate_expression("x / y", {"x": 5, "y": 0})

    def test_modulo_by_zero_raises(self) -> None:
        with pytest.raises(ExpressionError):
            evaluate_expression("input_tokens % 0", {"input_tokens": 5})

    def test_floordiv_by_zero_raises(self) -> None:
        with pytest.raises(ExpressionError):
            evaluate_expression("input_tokens // 0", {"input_tokens": 5})


# ── Variable-name validation (M5) ───────────────────────────────────────────


class TestVariableValidation:
    def test_unknown_variable_rejected_when_set_passed(self) -> None:
        with pytest.raises(ExpressionError, match="unknown variable"):
            validate_expression("inputtokens * 0.001", known_variables={"input_tokens"})

    def test_known_variable_accepted(self) -> None:
        validate_expression("input_tokens * 0.001", known_variables={"input_tokens"})

    def test_no_set_means_no_name_check(self) -> None:
        # Backwards compatible: without a known set, any identifier validates.
        validate_expression("anything * 2")

    def test_undefined_variable_at_eval(self) -> None:
        with pytest.raises(ExpressionError, match="undefined variable"):
            evaluate_expression("foo + bar", {"x": 1})


# ── M4: `if(` rewrite must not mangle identifiers ending in `if` ─────────────


class TestIfRewriteAnchor:
    def test_identifier_ending_in_if_not_mangled(self) -> None:
        # 'qualif' must stay a variable, not become 'qual_bursar_if('.
        with pytest.raises(ExpressionError, match="unknown variable"):
            validate_expression("qualif * 2", known_variables={"input_tokens"})

    def test_qualif_treated_as_variable_at_eval(self) -> None:
        result = evaluate_expression("qualif * 2", {"qualif": 3})
        assert result == Decimal(6)

    def test_real_if_still_rewritten(self) -> None:
        result = evaluate_expression("if(input_tokens > 0, 5, 1)", {"input_tokens": 10})
        assert result == Decimal(5)


# ── Helper arity / range errors (H6) ────────────────────────────────────────


class TestHelperArity:
    def test_clamp_requires_three(self) -> None:
        with pytest.raises(ExpressionError, match="clamp"):
            evaluate_expression("clamp(input_tokens)", {"input_tokens": 1})
        with pytest.raises(ExpressionError, match="clamp"):
            evaluate_expression("clamp(input_tokens, 0)", {"input_tokens": 1})
        with pytest.raises(ExpressionError, match="clamp"):
            evaluate_expression("clamp(input_tokens, 0, 100, 200)", {"input_tokens": 1})

    def test_if_requires_three(self) -> None:
        with pytest.raises(ExpressionError, match="if"):
            evaluate_expression("if(input_tokens > 0, 5)", {"input_tokens": 1})
        with pytest.raises(ExpressionError, match="if"):
            evaluate_expression("if(input_tokens > 0, 5, 1, 9)", {"input_tokens": 1})

    def test_tier_requires_even_ge_four(self) -> None:
        # Canonical rule (§1): even and >= 4. Odd arg counts and <4 args error.
        with pytest.raises(ExpressionError, match="tier"):
            # 2 args -> too few
            evaluate_expression("tier(input_tokens, 100)", {"input_tokens": 50})
        with pytest.raises(ExpressionError, match="tier"):
            # 3 args (odd) -> error
            evaluate_expression("tier(input_tokens, 100, 1)", {"input_tokens": 50})
        with pytest.raises(ExpressionError, match="tier"):
            # 5 args (odd) -> error
            evaluate_expression("tier(input_tokens, 0, 0, 10, 5)", {"input_tokens": 5})

    def test_tier_single_pair_valid(self) -> None:
        # 4 args (one (threshold, rate) pair + default) is VALID.
        assert evaluate_expression("tier(input_tokens, 100, 1, 9)", {"input_tokens": 50}) == Decimal(1)
        assert evaluate_expression("tier(input_tokens, 100, 1, 9)", {"input_tokens": 150}) == Decimal(9)

    def test_tier_valid(self) -> None:
        assert evaluate_expression("tier(input_tokens, 100, 1, 500, 2, 3)", {"input_tokens": 50}) == Decimal(1)
        assert evaluate_expression("tier(input_tokens, 100, 1, 500, 2, 3)", {"input_tokens": 300}) == Decimal(2)
        assert evaluate_expression("tier(input_tokens, 100, 1, 500, 2, 3)", {"input_tokens": 1000}) == Decimal(3)

    def test_percentile_range(self) -> None:
        with pytest.raises(ExpressionError, match="0 <= p <= 100"):
            evaluate_expression("percentile(150, x, y)", {"x": 1, "y": 2})
        with pytest.raises(ExpressionError, match="0 <= p <= 100"):
            evaluate_expression("percentile(-5, x, y)", {"x": 1, "y": 2})

    def test_percentile_min_args(self) -> None:
        # 1 arg (references x) -> too few; must hit the percentile arity guard.
        with pytest.raises(ExpressionError, match="percentile"):
            evaluate_expression("percentile(x)", {"x": 1})

    def test_percentile_valid(self) -> None:
        assert evaluate_expression("percentile(50, x, y, z)", {"x": 10, "y": 20, "z": 30}) == Decimal(20)
        assert evaluate_expression("percentile(0, x, y, z)", {"x": 10, "y": 20, "z": 30}) == Decimal(10)
        assert evaluate_expression("percentile(100, x, y, z)", {"x": 10, "y": 20, "z": 30}) == Decimal(30)
        assert evaluate_expression("percentile(50, x)", {"x": 42}) == Decimal(42)

    def test_min_max_require_at_least_one(self) -> None:
        # min()/max() with zero args -> error (parsed as literal calls).
        with pytest.raises(ExpressionError, match="min"):
            evaluate_expression("min() + input_tokens", {"input_tokens": 1})
        with pytest.raises(ExpressionError, match="max"):
            evaluate_expression("max() + input_tokens", {"input_tokens": 1})

    def test_min_max_basic(self) -> None:
        assert evaluate_expression("min(input_tokens, 100)", {"input_tokens": 500}) == Decimal(100)
        assert evaluate_expression("max(input_tokens, 100)", {"input_tokens": 50}) == Decimal(100)


# ── Existing behavior preserved (validation, functions) ─────────────────────


class TestValidateExpression:
    def test_valid_simple_math(self) -> None:
        validate_expression("input_tokens * 0.005 + output_tokens * 0.015")

    def test_valid_with_functions(self) -> None:
        validate_expression("ceil(input_tokens * 0.5)")

    def test_valid_with_conditional(self) -> None:
        validate_expression("x if x > 0 else 0")

    def test_rejects_lambda(self) -> None:
        with pytest.raises(ExpressionError, match="disallowed node"):
            validate_expression("lambda x: x + 1")

    def test_rejects_dict(self) -> None:
        with pytest.raises(ExpressionError, match="disallowed node"):
            validate_expression("{'a': 1}")

    def test_rejects_import(self) -> None:
        with pytest.raises(ExpressionError, match="unknown function"):
            validate_expression("__import__('os')")

    def test_rejects_unknown_function(self) -> None:
        with pytest.raises(ExpressionError, match="unknown function"):
            validate_expression("evil_func(1)")

    def test_rejects_attribute_access(self) -> None:
        with pytest.raises(ExpressionError, match="disallowed node"):
            validate_expression("x.__class__")

    def test_if_function(self) -> None:
        validate_expression("if(x > 0, x, 0)")

    def test_tier_function(self) -> None:
        validate_expression("tier(x, 0, 0, 10, 5, 5)")

    def test_clamp_function(self) -> None:
        validate_expression("clamp(x, 0, 100)")

    def test_not_prefix(self) -> None:
        validate_expression("not (x > 0)")
        validate_expression("x if not (x > 0) else 0")


class TestEvaluateExpression:
    def test_simple_multiplication(self) -> None:
        result = evaluate_expression("input_tokens * 5", {"input_tokens": 10})
        assert result == Decimal(50)

    def test_multi_variable(self) -> None:
        result = evaluate_expression(
            "input_tokens * 0.005 + output_tokens * 0.015",
            {"input_tokens": 342, "output_tokens": 1204},
        )
        # 342*0.005 + 1204*0.015 = 1.71 + 18.06 = 19.77 (exact)
        assert result == Decimal("19.77")

    def test_ceil_function(self) -> None:
        assert evaluate_expression("ceil(x * 0.5)", {"x": 10}) == Decimal(5)
        assert evaluate_expression("ceil(x * 0.0011)", {"x": 1000}) == Decimal(2)

    def test_floor_function(self) -> None:
        assert evaluate_expression("floor(x * 0.5)", {"x": 11}) == Decimal(5)
        assert evaluate_expression("floor(x * 0.0019)", {"x": 1000}) == Decimal(1)

    def test_min_function(self) -> None:
        assert evaluate_expression("min(x, y)", {"x": 5, "y": 10}) == Decimal(5)

    def test_max_function(self) -> None:
        assert evaluate_expression("max(x, y)", {"x": 5, "y": 10}) == Decimal(10)

    def test_round_function(self) -> None:
        assert evaluate_expression("round(x, 2)", {"x": Decimal("3.14159")}) == Decimal("3.14")

    def test_negative_result(self) -> None:
        assert evaluate_expression("-x", {"x": 5}) == Decimal(-5)

    def test_zero_variables(self) -> None:
        result = evaluate_expression("input_tokens * 5", {"input_tokens": 0, "output_tokens": 100})
        assert result == Decimal(0)

    def test_if_function(self) -> None:
        assert evaluate_expression("if(x > 10, x * 5, x * 2)", {"x": 20}) == Decimal(100)
        assert evaluate_expression("if(x > 10, x * 5, x * 2)", {"x": 5}) == Decimal(10)

    def test_tier_function(self) -> None:
        # value, (0->0), (10->5), default 5  (6 args, two tiers)
        assert evaluate_expression("tier(x, 0, 0, 10, 5, 5)", {"x": -1}) == Decimal(0)
        assert evaluate_expression("tier(x, 0, 0, 10, 5, 5)", {"x": 5}) == Decimal(5)
        assert evaluate_expression("tier(x, 0, 0, 10, 5, 5)", {"x": 15}) == Decimal(5)

    def test_tier_with_default(self) -> None:
        # value, (0->0), (10->5), (100->10), default 10  (8 args, three tiers)
        assert evaluate_expression("tier(x, 0, 0, 10, 5, 100, 10, 10)", {"x": 50}) == Decimal(10)

    def test_clamp_function(self) -> None:
        assert evaluate_expression("clamp(x, 0, 100)", {"x": 50}) == Decimal(50)
        assert evaluate_expression("clamp(x, 0, 100)", {"x": -10}) == Decimal(0)
        assert evaluate_expression("clamp(x, 0, 100)", {"x": 200}) == Decimal(100)

    def test_not_prefix(self) -> None:
        assert evaluate_expression("5 if not (x > 10) else 10", {"x": 5}) == Decimal(5)
        assert evaluate_expression("5 if not (x > 10) else 10", {"x": 15}) == Decimal(10)

    def test_in_operator_str(self) -> None:
        assert evaluate_expression('"hello" in x', {"x": 2.0}) == Decimal(0)
        assert evaluate_expression('"2" in x', {"x": 2.0}) == Decimal(0)


def _eval(expr: str) -> Decimal:
    """Evaluate a literal-only expression by injecting a dummy variable."""
    return evaluate_expression(f"({expr}) if _ == _ else 0", {"_": 0})


def test_in_operator_behavior() -> None:
    """Verify Python 'in' matches JS String(l).includes(String(r))."""
    assert not _eval("2 in 20")
    assert _eval("20 in 2")
    assert _eval("2.0 in 2")
    assert _eval('"hello world" in "hello"')
    assert _eval("2 not in 20")
    assert not _eval("20 not in 2")


def test_percentile_function() -> None:
    assert evaluate_expression("percentile(50, x, y, z)", {"x": 10, "y": 20, "z": 30}) == Decimal(20)
    assert evaluate_expression("percentile(0, x, y, z)", {"x": 10, "y": 20, "z": 30}) == Decimal(10)
    assert evaluate_expression("percentile(100, x, y, z)", {"x": 10, "y": 20, "z": 30}) == Decimal(30)
    assert evaluate_expression("percentile(50, x)", {"x": 42}) == Decimal(42)
    with pytest.raises(ExpressionError):
        evaluate_expression("percentile(50)", {"x": 1})


def test_percentile_is_validated() -> None:
    validate_expression("percentile(50, input_tokens, output_tokens)")


def test_not_precedence() -> None:
    """Verify 'not' binds tighter than comparison (matching JS)."""
    assert _eval("not 5 > 10")
    assert not _eval("not 10 > 5")
    assert _eval("not 5 > 10 and 3 > 1")


# ── E1: tier() exact boundary semantics ─────────────────────────────────────


class TestTierBoundarySemantics:
    """E1 — tier() uses strict less-than: val < threshold returns that tier's rate."""

    def test_at_first_threshold_falls_to_next_tier(self) -> None:
        # val=100, threshold=100: 100 < 100 is False -> falls to second tier (threshold=500)
        # 100 < 500 is True -> returns rate 2
        result = evaluate_expression("tier(input_tokens, 100, 1, 500, 2, 3)", {"input_tokens": 100})
        assert _q(result) == Decimal("2.0000")

    def test_just_below_first_threshold(self) -> None:
        # val=99, threshold=100: 99 < 100 is True -> returns rate 1
        result = evaluate_expression("tier(input_tokens, 100, 1, 500, 2, 3)", {"input_tokens": 99})
        assert _q(result) == Decimal("1.0000")

    def test_at_second_threshold_falls_to_default(self) -> None:
        # val=500, threshold=500: 500 < 500 is False -> falls to default 3
        result = evaluate_expression("tier(input_tokens, 100, 1, 500, 2, 3)", {"input_tokens": 500})
        assert _q(result) == Decimal("3.0000")


# ── E2: percentile() edge cases ──────────────────────────────────────────────


class TestPercentileEdgeCases:
    """E2 — percentile() with single/two elements, uniform values, p=0/100."""

    def test_single_element(self) -> None:
        result = evaluate_expression("percentile(50, x)", {"x": 7})
        assert _q(result) == Decimal("7.0000")

    def test_two_elements_median(self) -> None:
        # p=50 of [3, 7]: rank = 0.5 * (2-1) = 0.5 -> lower=0, frac=0.5
        # 3*(1-0.5) + 7*0.5 = 1.5 + 3.5 = 5.0
        result = evaluate_expression("percentile(50, x, y)", {"x": 3, "y": 7})
        assert _q(result) == Decimal("5.0000")

    def test_all_same_values(self) -> None:
        result = evaluate_expression("percentile(50, x, y, z)", {"x": 5, "y": 5, "z": 5})
        assert _q(result) == Decimal("5.0000")

    def test_p_zero_returns_minimum(self) -> None:
        result = evaluate_expression("percentile(0, x, y, z)", {"x": 1, "y": 2, "z": 3})
        assert _q(result) == Decimal("1.0000")

    def test_p_hundred_returns_maximum(self) -> None:
        result = evaluate_expression("percentile(100, x, y, z)", {"x": 1, "y": 2, "z": 3})
        assert _q(result) == Decimal("3.0000")

    def test_out_of_range_p_raises(self) -> None:
        # p=150 is already in the parity fixture, but verify it raises ExpressionError at eval
        with pytest.raises(ExpressionError, match="0 <= p <= 100"):
            evaluate_expression("percentile(150, x, y)", {"x": 1, "y": 2})


# ── E3: clamp(min > max) ─────────────────────────────────────────────────────


class TestClampMinGtMax:
    """E3 — clamp() when lo > hi: max(lo, min(x, hi)) so lo always wins."""

    def test_clamp_min_greater_than_max_returns_min(self) -> None:
        # clamp(5, 10, 3): max(10, min(5, 3)) = max(10, 3) = 10
        result = evaluate_expression("clamp(x, 10, 3)", {"x": 5})
        assert result == Decimal(10)

    def test_clamp_min_greater_than_max_with_value_above_min(self) -> None:
        # clamp(20, 10, 3): max(10, min(20, 3)) = max(10, 3) = 10
        result = evaluate_expression("clamp(x, 10, 3)", {"x": 20})
        assert result == Decimal(10)

    def test_clamp_min_greater_than_max_with_value_below_max(self) -> None:
        # clamp(1, 10, 3): max(10, min(1, 3)) = max(10, 1) = 10
        result = evaluate_expression("clamp(x, 10, 3)", {"x": 1})
        assert result == Decimal(10)


# ── E4: Negative operand edge cases ─────────────────────────────────────────


class TestNegativeOperandEdgeCases:
    """E4 — negation, double-negation, min/max with negatives."""

    def test_unary_negate_then_multiply(self) -> None:
        # (-input_tokens) * 0.001 with input_tokens=1000 -> -1.0
        result = evaluate_expression("(-input_tokens) * 0.001", {"input_tokens": 1000})
        assert _q(result) == Decimal("-1.0000")

    def test_double_negate(self) -> None:
        # -(-input_tokens) * 0.001 with input_tokens=1000 -> 1.0
        result = evaluate_expression("-(-input_tokens) * 0.001", {"input_tokens": 1000})
        assert _q(result) == Decimal("1.0000")

    def test_max_with_negative_and_zero(self) -> None:
        result = evaluate_expression("max(x, 0)", {"x": -5})
        assert _q(result) == Decimal("0.0000")

    def test_min_both_negative(self) -> None:
        result = evaluate_expression("min(x, y)", {"x": -5, "y": -3})
        assert _q(result) == Decimal("-5.0000")


# ── E5: Division with negative operands ─────────────────────────────────────


class TestDivisionNegativeOperands:
    """E5 — division and modulo sign conventions with negative operands."""

    def test_negative_dividend(self) -> None:
        result = evaluate_expression("(-x) / 2", {"x": 10})
        assert _q(result) == Decimal("-5.0000")

    def test_negative_divisor(self) -> None:
        result = evaluate_expression("x / (-y)", {"x": 10, "y": 2})
        assert _q(result) == Decimal("-5.0000")

    def test_modulo_negative_dividend_decimal_convention(self) -> None:
        # Decimal modulo uses truncated (C-style) division, not Python's floor division.
        # Decimal(-10) % Decimal(3) == -1  (sign of dividend, not divisor)
        result = evaluate_expression("(-x) % 3", {"x": 10})
        assert result == Decimal(-1)

    def test_modulo_negative_divisor_decimal_convention(self) -> None:
        # Decimal(10) % Decimal(-3) == 1  (sign of dividend, not divisor)
        result = evaluate_expression("x % (-y)", {"x": 10, "y": 3})
        assert result == Decimal(1)


# ── E6: Floor division by zero ───────────────────────────────────────────────


class TestFloorDivByZero:
    """E6 — floor division by zero raises ExpressionError."""

    def test_floor_div_by_zero_raises(self) -> None:
        with pytest.raises(ExpressionError):
            evaluate_expression("x // 0", {"x": 10})


# ── E7: Large numeric literals ───────────────────────────────────────────────


class TestLargeNumericLiterals:
    """E7 — large literals stay exact, no overflow."""

    def test_large_literal_exact(self) -> None:
        result = evaluate_expression("x * 999999999999.9999", {"x": 1})
        assert str(_q(result)) == "999999999999.9999"


# ── E8: Expression with no variables ─────────────────────────────────────────


class TestExpressionNoVariables:
    """E8 — expression referencing no variables is rejected."""

    def test_constant_expression_rejected(self) -> None:
        # The engine requires at least one metric variable in every expression.
        with pytest.raises(ExpressionError, match="no variables"):
            evaluate_expression("1 + 2", {"x": 1})


# ── E9: Nested function calls ────────────────────────────────────────────────


class TestNestedFunctionCalls:
    """E9 — functions nested inside other function arguments evaluate correctly."""

    def test_max_of_ceil(self) -> None:
        # ceil(500 * 0.001) = ceil(0.5) = 1; max(1, 1) = 1
        result = evaluate_expression("max(ceil(input_tokens * 0.001), 1)", {"input_tokens": 500})
        assert _q(result) == Decimal("1.0000")

    def test_clamp_of_round(self) -> None:
        # round(1000 * 0.0025) = round(2.5) = 3 (ROUND_HALF_UP); clamp(3, 0, 5) = 3
        result = evaluate_expression("clamp(round(input_tokens * 0.0025), 0, 5)", {"input_tokens": 1000})
        assert _q(result) == Decimal("3.0000")

    def test_if_with_nested_ceil_and_floor(self) -> None:
        # input_tokens=200 > 100 is True -> ceil(200 * 0.001) = ceil(0.2) = 1
        result = evaluate_expression(
            "if(input_tokens > 100, ceil(input_tokens * 0.001), floor(input_tokens * 0 + 0.5))",
            {"input_tokens": 200},
        )
        assert _q(result) == Decimal("1.0000")


# ── E10: Decimal quantization boundary ──────────────────────────────────────


class TestDecimalQuantizationBoundary:
    """E10 — values near the 4dp boundary quantize with ROUND_HALF_UP."""

    def test_below_half_ulp_rounds_to_zero(self) -> None:
        # a * 0.00001 = 0.00001; 4dp quantize rounds down to 0.0000
        result = evaluate_expression("a * 0.00001", {"a": 1})
        assert str(_q(result)) == "0.0000"

    def test_exactly_half_ulp_rounds_up(self) -> None:
        # a * 0.000050 = 0.000050; exactly half a 4dp unit -> ROUND_HALF_UP -> 0.0001
        result = evaluate_expression("a * 0.000050", {"a": 1})
        assert str(_q(result)) == "0.0001"

    def test_just_below_half_ulp_rounds_down(self) -> None:
        # a * 0.000049 = 0.000049 < 0.00005 -> rounds down to 0.0000
        result = evaluate_expression("a * 0.000049", {"a": 1})
        assert str(_q(result)) == "0.0000"


# ── E11: Parity pricing_cases via PricingEngine ──────────────────────────────


_PRICING_CASES_FOR_EXPR = _PARITY["pricing_cases"]


@pytest.mark.parametrize("case", _PRICING_CASES_FOR_EXPR, ids=[c["name"] for c in _PRICING_CASES_FOR_EXPR])
def test_parity_pricing_cases_via_engine(case: dict) -> None:
    """E11 — every pricing_cases entry in the parity fixture passes through
    PricingEngine.from_dict() and produces the expected total (byte-identical string)."""
    from bursar.engine import PricingEngine
    from bursar.metrics import UsageMetrics

    engine = PricingEngine.from_dict(case["config"])
    breakdown = engine.calculate(UsageMetrics(**case["metrics"]))
    assert str(breakdown.total) == case["expected_total"]


# ── E12: Concurrent eval isolation ──────────────────────────────────────────


class TestConcurrentEvalIsolation:
    """E12 — concurrent evaluate_expression calls on different threads produce
    correct results and do not bleed Decimal context state."""

    def test_20_threads_no_state_bleed(self) -> None:
        import threading

        NUM_THREADS = 20
        results: dict[int, Decimal] = {}
        errors: list[tuple[int, Exception]] = []

        def worker(tid: int) -> None:
            try:
                # Each thread uses a distinct multiplier to detect cross-thread bleed.
                result = evaluate_expression("input_tokens * 0.001", {"input_tokens": tid * 100})
                results[tid] = _q(result)
            except Exception as exc:
                errors.append((tid, exc))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(1, NUM_THREADS + 1)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"threads raised errors: {errors}"
        for tid in range(1, NUM_THREADS + 1):
            expected = _q(Decimal(tid * 100) * Decimal("0.001"))
            assert results[tid] == expected, f"thread {tid}: got {results[tid]}, expected {expected}"
