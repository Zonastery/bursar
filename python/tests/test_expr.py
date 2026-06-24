import math

import pytest

from ducto.expr import ExpressionError, evaluate_expression, validate_expression


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
        validate_expression("tier(x, 0, 0, 10, 5)")

    def test_clamp_function(self) -> None:
        validate_expression("clamp(x, 0, 100)")

    def test_not_prefix(self) -> None:
        validate_expression("not (x > 0)")
        validate_expression("x if not (x > 0) else 0")


class TestEvaluateExpression:
    def test_simple_multiplication(self) -> None:
        result = evaluate_expression("input_tokens * 5", {"input_tokens": 10})
        assert result == 50.0

    def test_multi_variable(self) -> None:
        result = evaluate_expression(
            "input_tokens * 0.005 + output_tokens * 0.015",
            {"input_tokens": 342, "output_tokens": 1204},
        )
        assert result == pytest.approx(19.77, rel=1e-3)

    def test_ceil_function(self) -> None:
        result = evaluate_expression("ceil(x * 0.5)", {"x": 10})
        assert result == 5.0

    def test_floor_function(self) -> None:
        result = evaluate_expression("floor(x * 0.5)", {"x": 11})
        assert result == 5.0

    def test_min_function(self) -> None:
        result = evaluate_expression("min(x, y)", {"x": 5, "y": 10})
        assert result == 5.0

    def test_max_function(self) -> None:
        result = evaluate_expression("max(x, y)", {"x": 5, "y": 10})
        assert result == 10.0

    def test_round_function(self) -> None:
        result = evaluate_expression("round(x, 2)", {"x": 3.14159})
        assert result == 3.14

    def test_negative_result(self) -> None:
        result = evaluate_expression("-x", {"x": 5})
        assert result == -5.0

    def test_zero_variables(self) -> None:
        result = evaluate_expression(
            "input_tokens * 5",
            {"input_tokens": 0, "output_tokens": 100},
        )
        assert result == 0.0

    def test_unknown_variable_raises(self) -> None:
        with pytest.raises(ExpressionError, match="undefined variable"):
            evaluate_expression("foo + bar", {"x": 1})

    def test_division_by_zero_returns_inf(self) -> None:
        result = evaluate_expression("x / y", {"x": 5, "y": 0})
        assert math.isinf(result)

    def test_if_function(self) -> None:
        result = evaluate_expression("if(x > 10, x * 5, x * 2)", {"x": 20})
        assert result == 100.0
        result = evaluate_expression("if(x > 10, x * 5, x * 2)", {"x": 5})
        assert result == 10.0

    def test_tier_function(self) -> None:
        result = evaluate_expression("tier(x, 0, 0, 10, 5)", {"x": -1})
        assert result == 0.0
        result = evaluate_expression("tier(x, 0, 0, 10, 5)", {"x": 5})
        assert result == 5.0
        result = evaluate_expression("tier(x, 0, 0, 10, 5)", {"x": 15})
        assert result == 5.0

    def test_tier_with_default(self) -> None:
        result = evaluate_expression("tier(x, 0, 0, 10, 5, 100, 10)", {"x": 50})
        assert result == 10.0

    def test_clamp_function(self) -> None:
        result = evaluate_expression("clamp(x, 0, 100)", {"x": 50})
        assert result == 50.0
        result = evaluate_expression("clamp(x, 0, 100)", {"x": -10})
        assert result == 0.0
        result = evaluate_expression("clamp(x, 0, 100)", {"x": 200})
        assert result == 100.0

    def test_not_prefix(self) -> None:
        result = evaluate_expression("5 if not (x > 10) else 10", {"x": 5})
        assert result == 5.0
        result = evaluate_expression("5 if not (x > 10) else 10", {"x": 15})
        assert result == 10.0

    def test_in_operator_str(self) -> None:
        result = evaluate_expression('"hello" in x', {"x": 2.0})
        assert result == 0.0
        result = evaluate_expression('"2" in x', {"x": 2.0})
        assert result == 0.0  # WAS 1.0 -- fixed: "2.0" in "2" = False


def _eval(expr: str) -> float:
    """Evaluate a literal-only expression by injecting a dummy variable."""
    return evaluate_expression(f"({expr}) if _ == _ else 0", {"_": 0})


def test_in_operator_behavior() -> None:
    """Verify Python 'in' matches JS String(l).includes(String(r))."""
    # Expression engine returns float (0.0=truthy, 1.0=truthy)
    # JS: String(2).includes(String(20)) = "2".includes("20") = False
    assert not _eval("2 in 20")
    # JS: String(20).includes(String(2)) = "20".includes("2") = True
    assert _eval("20 in 2")
    # JS: String(2.0).includes(String(2)) = "2.0".includes("2") = True
    assert _eval("2.0 in 2")
    # JS: String("hello world").includes(String("hello")) = True
    assert _eval('"hello world" in "hello"')
    # not in
    assert _eval("2 not in 20")
    assert not _eval("20 not in 2")


def test_not_precedence() -> None:
    """Verify 'not' binds tighter than comparison (matching JS)."""
    assert _eval("not 5 > 10")  # not (5 > 10) = not False = True
    assert not _eval("not 10 > 5")  # not (10 > 5) = not True = False
    assert _eval("not 5 > 10 and 3 > 1")  # (not (5 > 10)) and (3 > 1)
