"""Tests for the calculator tool's AST evaluator."""

import pytest

from kai.agent.tools.calculator import calculate


class TestArithmetic:
    def test_evaluates_integer_addition(self):
        assert calculate("2 + 3") == "5"

    def test_evaluates_float_result(self):
        assert calculate("7 / 2") == "3.5"

    def test_renders_whole_float_as_integer(self):
        assert calculate("4 / 2") == "2"

    @pytest.mark.parametrize(
        ("expression", "expected"),
        [
            ("7 - 3", "4"),
            ("4 * 5", "20"),
            ("9 // 2", "4"),
            ("9 % 2", "1"),
            ("2 ** 5", "32"),
            ("-4", "-4"),
            ("+4", "4"),
            ("2 + 3 * 4", "14"),
        ],
    )
    def test_evaluates_operators(self, expression, expected):
        assert calculate(expression) == expected

    def test_fractional_division(self):
        assert calculate("1 / 3") == "0.3333333333333333"


class TestFunctions:
    @pytest.mark.parametrize(
        ("expression", "expected"),
        [
            ("abs(-5)", "5"),
            ("round(2.6)", "3"),
            ("min(3, 1, 2)", "1"),
            ("max(3, 1, 2)", "3"),
            ("sqrt(9)", "3"),
            ("floor(2.9)", "2"),
            ("ceil(2.1)", "3"),
            ("log10(100)", "2"),
            ("ln(1)", "0"),
            ("sin(0)", "0"),
            ("cos(0)", "1"),
            ("tan(0)", "0"),
        ],
    )
    def test_evaluates_allowed_functions(self, expression, expected):
        assert calculate(expression) == expected


class TestRejections:
    def test_syntax_error_is_reported(self):
        assert calculate("2 +").startswith("Error: invalid expression")

    def test_unsupported_call_target_is_rejected(self):
        assert calculate("__import__('os')") == "Error: Function not allowed"

    def test_attribute_call_is_rejected(self):
        assert calculate("math.sqrt(4)") == "Error: Function not allowed"

    def test_keyword_arguments_are_rejected(self):
        assert calculate("round(2.5, ndigits=1)") == "Error: Keyword arguments are not supported"

    def test_boolean_constant_is_rejected(self):
        assert calculate("True") == "Error: Unsupported constant: True"

    def test_unsupported_operator_is_rejected(self):
        assert calculate("1 << 2") == "Error: Unsupported operator: LShift"

    def test_oversized_exponent_is_rejected(self):
        assert calculate("2 ** 1001") == "Error: Exponentiation operands too large"

    def test_zero_division_is_reported(self):
        assert calculate("1 / 0") == "Error: division by zero"

    def test_unsupported_expression_type_is_rejected(self):
        assert calculate("[1, 2]") == "Error: Unsupported expression: List"
