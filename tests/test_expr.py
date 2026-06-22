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
