"""
formula_parser.py
-----------------
Parse a clean SPINS formula string directly into Whiz JSON steps.
No LLM or template matching required.

Supported syntax (any nesting):
    SUM(COLUMN), MAX(COLUMN)    → sum / max steps
    A / B                       → division (or percent if flag set)
    A - B                       → subtraction
    A * B                       → multiplication
    (A / B) - 1                 → percentChange  (auto-detected)
    ((A/B) / (C/D)) - 1        → percentChange where A/B and C/D are intermediate steps
"""

import re
from dataclasses import dataclass
from typing import Union


# ── AST nodes ──────────────────────────────────────────────────────────────────

@dataclass
class AggNode:
    func: str    # "SUM" or "MAX"
    col: str     # column name, upper-cased

@dataclass
class NumNode:
    value: float

@dataclass
class BinOpNode:
    op: str      # "+", "-", "*", "/"
    left: "ASTNode"
    right: "ASTNode"

ASTNode = Union[AggNode, NumNode, BinOpNode]


# ── Tokenizer ──────────────────────────────────────────────────────────────────

def tokenize(formula: str) -> list:
    formula = formula.strip().upper()
    tokens = []
    i = 0
    while i < len(formula):
        c = formula[i]
        if c in " \t":
            i += 1
        elif c == "(":
            tokens.append(("LPAREN", "("))
            i += 1
        elif c == ")":
            tokens.append(("RPAREN", ")"))
            i += 1
        elif c == "+":
            tokens.append(("PLUS", "+"))
            i += 1
        elif c == "-":
            tokens.append(("MINUS", "-"))
            i += 1
        elif c == "*":
            tokens.append(("TIMES", "*"))
            i += 1
        elif c == "/":
            tokens.append(("SLASH", "/"))
            i += 1
        elif c.isdigit() or (c == "." and i + 1 < len(formula) and formula[i + 1].isdigit()):
            j = i
            while j < len(formula) and (formula[j].isdigit() or formula[j] == "."):
                j += 1
            tokens.append(("NUM", float(formula[i:j])))
            i = j
        elif c.isalpha() or c == "_":
            j = i
            while j < len(formula) and (formula[j].isalnum() or formula[j] == "_"):
                j += 1
            tokens.append(("WORD", formula[i:j]))
            i = j
        else:
            raise ValueError(f"Unexpected character {c!r} at position {i} in: {formula}")
    tokens.append(("EOF", None))
    return tokens


# ── Recursive descent parser ───────────────────────────────────────────────────

class Parser:
    def __init__(self, tokens: list):
        self.tokens = tokens
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos]

    def consume(self, expected_type=None):
        tok = self.tokens[self.pos]
        if expected_type and tok[0] != expected_type:
            raise ValueError(f"Expected {expected_type}, got {tok[0]!r} ({tok[1]!r})")
        self.pos += 1
        return tok

    def parse(self) -> ASTNode:
        node = self._expr()
        self.consume("EOF")
        return node

    def _expr(self) -> ASTNode:
        """expr → term (('+' | '-') term)*"""
        node = self._term()
        while self.peek()[0] in ("PLUS", "MINUS"):
            op = self.consume()[1]
            right = self._term()
            node = BinOpNode(op, node, right)
        return node

    def _term(self) -> ASTNode:
        """term → factor (('*' | '/') factor)*"""
        node = self._factor()
        while self.peek()[0] in ("TIMES", "SLASH"):
            op = self.consume()[1]
            right = self._factor()
            node = BinOpNode(op, node, right)
        return node

    def _factor(self) -> ASTNode:
        """factor → '(' expr ')' | agg_call | NUMBER"""
        tok = self.peek()
        if tok[0] == "LPAREN":
            self.consume("LPAREN")
            node = self._expr()
            self.consume("RPAREN")
            return node
        elif tok[0] == "WORD":
            if tok[1] in ("SUM", "MAX"):
                return self._agg()
            raise ValueError(f"Unexpected word: {tok[1]!r}")
        elif tok[0] == "NUM":
            self.consume()
            return NumNode(tok[1])
        else:
            raise ValueError(f"Unexpected token: {tok}")

    def _agg(self) -> AggNode:
        """agg_call → ('SUM' | 'MAX') '(' IDENT ')'"""
        func = self.consume("WORD")[1]
        self.consume("LPAREN")
        col = self.consume("WORD")[1]
        self.consume("RPAREN")
        return AggNode(func, col)


def parse_formula(formula: str) -> ASTNode:
    tokens = tokenize(formula)
    return Parser(tokens).parse()


# ── Pattern helpers ────────────────────────────────────────────────────────────

def _canonical(node: ASTNode) -> str:
    """Canonical string for a node — used to detect duplicate sub-expressions."""
    if isinstance(node, AggNode):
        return f"{node.func}({node.col})"
    if isinstance(node, NumNode):
        return str(node.value)
    if isinstance(node, BinOpNode):
        return f"({_canonical(node.left)}{node.op}{_canonical(node.right)})"
    raise TypeError(f"Unknown node: {node}")


def _is_pct_change(node: ASTNode) -> bool:
    """True for (A / B) - 1 at any depth of A and B."""
    return (
        isinstance(node, BinOpNode)
        and node.op == "-"
        and isinstance(node.right, NumNode)
        and node.right.value == 1.0
        and isinstance(node.left, BinOpNode)
        and node.left.op == "/"
    )


# ── Step generator ─────────────────────────────────────────────────────────────

def to_snake(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", s).strip("_").lower()


def generate_steps(
    ast: ASTNode,
    measure_name: str,
    default_div_type: str = "division",  # "division" or "percent"
) -> list[dict]:
    """
    Walk the AST and emit Whiz JSON steps.

    Rules:
    - SUM(X)/MAX(X)              → {type: "sum"/"max", column: X}
    - A - B                      → {type: "subtraction", from: A, value: B}
    - A * B                      → {type: "multiplication", values: [A, B]}
    - A / B (default_div_type=division) → {type: "division", value: A, denominator: B}
    - A / B (default_div_type=percent)  → {type: "percent",   value: A, outOf: B}
    - (A / B) - 1                → {type: "percentChange",  value: A, base: B}   (auto-detected)

    Deduplication:
    - Same SUM(X)/MAX(X) referenced twice → step emitted once, reused by name.
    - Same sub-expression (e.g. A/B appearing in both numerator and denominator
      like in % Discount) → intermediate step emitted once, reused.
    """
    steps: list[dict] = []
    # Cache: canonical(node) → step_name (or numeric value for NumNode)
    cache: dict[str, object] = {}
    counter = [0]

    def _new_intermediate_name() -> str:
        counter[0] += 1
        return f"step_{counter[0]}"

    def _gen(node: ASTNode, is_root: bool = False) -> object:
        key = _canonical(node)
        if key in cache:
            return cache[key]

        if isinstance(node, NumNode):
            cache[key] = node.value
            return node.value

        if isinstance(node, AggNode):
            # Deduplicate by (func, col)
            name = node.col.lower()
            # Ensure uniqueness if two different columns lowercase to same name
            base = name
            n = 2
            while any(s["name"] == name for s in steps):
                name = f"{base}_{n}"
                n += 1
            steps.append({"name": name, "type": node.func.lower(), "column": node.col})
            cache[key] = name
            return name

        # BinOpNode
        assert isinstance(node, BinOpNode)

        # ── Special pattern: (A / B) - 1  →  percentChange ──────────────────
        if _is_pct_change(node):
            div_node = node.left  # BinOpNode('/', A, B)
            val_ref = _gen(div_node.left)
            base_ref = _gen(div_node.right)
            step_name = to_snake(measure_name) if is_root else _new_intermediate_name()
            step = {
                "name": step_name,
                "type": "percentChange",
                "value": val_ref,
                "base": base_ref,
                "divByZeroResponse": 0.0,
            }
            steps.append(step)
            cache[key] = step_name
            return step_name

        # ── Generic binary ops ────────────────────────────────────────────────
        left_ref = _gen(node.left)
        right_ref = _gen(node.right)
        step_name = to_snake(measure_name) if is_root else _new_intermediate_name()

        if node.op == "/":
            if default_div_type == "percent":
                step = {"name": step_name, "type": "percent",
                        "value": left_ref, "outOf": right_ref, "divByZeroResponse": 0.0}
            else:
                step = {"name": step_name, "type": "division",
                        "value": left_ref, "denominator": right_ref, "divByZeroResponse": 0.0}
        elif node.op == "-":
            step = {"name": step_name, "type": "subtraction",
                    "from": left_ref, "value": right_ref}
        elif node.op == "*":
            step = {"name": step_name, "type": "multiplication",
                    "values": [left_ref, right_ref]}
        elif node.op == "+":
            step = {"name": step_name, "type": "addition",
                    "values": [left_ref, right_ref]}
        else:
            raise ValueError(f"Unsupported operator: {node.op}")

        steps.append(step)
        cache[key] = step_name
        return step_name

    _gen(ast, is_root=True)
    return steps


# ── Public API ─────────────────────────────────────────────────────────────────

def formula_to_steps(
    formula: str,
    measure_name: str,
    default_div_type: str = "division",
) -> list[dict]:
    """
    Convert a clean formula string to a Whiz JSON step list.

    Args:
        formula:          e.g. "SUM(DOLLARS) / SUM(UNITS)"
        measure_name:     used to name the final (root) step
        default_div_type: "division" or "percent" — how to treat bare A/B
    """
    ast = parse_formula(formula)
    return generate_steps(ast, measure_name, default_div_type)
