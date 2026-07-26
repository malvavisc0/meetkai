import ast
import math
import operator

_CALC_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

_CALC_FUNCTIONS = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sqrt": math.sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "log": math.log,
    "log10": math.log10,
    "ln": math.log,
    "floor": math.floor,
    "ceil": math.ceil,
}

_CALC_MAX_POW_EXPONENT = 1000
_CALC_MAX_POW_BASE = 1e12


def _calc_eval(node: ast.AST) -> float | int:
    if isinstance(node, ast.Constant):
        return _eval_constant(node)
    if isinstance(node, ast.BinOp):
        return _eval_binop(node)
    if isinstance(node, ast.UnaryOp):
        return _eval_unaryop(node)
    if isinstance(node, ast.Call):
        return _eval_call(node)
    raise ValueError(f"Unsupported expression: {type(node).__name__}")


def _eval_constant(node: ast.Constant) -> float | int:
    if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    raise ValueError(f"Unsupported constant: {node.value!r}")


def _eval_binop(node: ast.BinOp) -> float | int:
    op = _CALC_OPERATORS.get(type(node.op))
    if op is None:
        raise ValueError(f"Unsupported operator: {type(node.op).__name__}")
    left = _calc_eval(node.left)
    right = _calc_eval(node.right)
    if isinstance(node.op, ast.Pow) and (
        abs(right) > _CALC_MAX_POW_EXPONENT or abs(left) > _CALC_MAX_POW_BASE
    ):
        raise ValueError("Exponentiation operands too large")
    return op(left, right)


def _eval_unaryop(node: ast.UnaryOp) -> float | int:
    op = _CALC_OPERATORS.get(type(node.op))
    if op is None:
        raise ValueError(f"Unsupported operator: {type(node.op).__name__}")
    return op(_calc_eval(node.operand))


def _eval_call(node: ast.Call) -> float | int:
    if not isinstance(node.func, ast.Name) or node.func.id not in _CALC_FUNCTIONS:
        raise ValueError("Function not allowed")
    if node.keywords:
        raise ValueError("Keyword arguments are not supported")
    fn = _CALC_FUNCTIONS[node.func.id]
    args = [_calc_eval(a) for a in node.args]
    return fn(*args)


def calculate(expression: str) -> str:
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as exc:
        return f"Error: invalid expression ({exc.msg})"
    try:
        result = _calc_eval(tree.body)
    except (ValueError, TypeError, ZeroDivisionError) as exc:
        return f"Error: {exc}"
    if isinstance(result, float) and result.is_integer():
        return str(int(result))
    return str(result)
