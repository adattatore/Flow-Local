"""
Valutatore matematico e matriciale sicuro (AST + NumPy) per l'auto-calcolo inline di FlowNotes.
Non utilizza mai eval()/exec() grezzi su stringhe arbitrarie.
"""

from __future__ import annotations

import ast
import math
import re
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np


_SAFE_CONSTANTS: Dict[str, float] = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
    "inf": math.inf,
}

_SAFE_SCALAR_FUNCS: Dict[str, Any] = {
    "sqrt": math.sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "sinh": math.sinh,
    "cosh": math.cosh,
    "tanh": math.tanh,
    "log": math.log10,
    "log10": math.log10,
    "log2": math.log2,
    "ln": math.log,
    "exp": math.exp,
    "abs": abs,
    "round": round,
    "floor": math.floor,
    "ceil": math.ceil,
    "radians": math.radians,
    "degrees": math.degrees,
    "rad": math.radians,
    "deg": math.degrees,
}


def _safe_factorial(n: Union[int, float]) -> int:
    if isinstance(n, float):
        if not n.is_integer():
            raise ValueError("factorial requires integer")
        n = int(n)
    if n < 0 or n > 170:
        raise ValueError("factorial out of range")
    return math.factorial(n)


_SAFE_SCALAR_FUNCS["factorial"] = _safe_factorial


def format_number(val: Union[int, float, complex, np.number]) -> str:
    """Formatta un numero reale o complesso in modo compatto e leggibile."""
    if isinstance(val, (complex, np.complexfloating)):
        re_p = float(val.real)
        im_p = float(val.imag)
        if abs(im_p) < 1e-9:
            return format_number(re_p)
        re_s = format_number(re_p) if abs(re_p) >= 1e-9 else ""
        im_abs = abs(im_p)
        im_s = "" if abs(im_abs - 1.0) < 1e-9 else format_number(im_abs)
        sign = "+" if im_p >= 0 else "-"
        if not re_s:
            return f"{'-' if im_p < 0 else ''}{im_s}i"
        return f"{re_s}{sign}{im_s}i"

    fval = float(val)
    if math.isnan(fval) or math.isinf(fval):
        raise ValueError("Non-finite result")
    if abs(fval) < 1e-11:
        return "0"
    rounded = round(fval)
    if abs(fval - rounded) < 1e-9:
        return str(int(rounded))
    s = f"{fval:.6f}".rstrip("0").rstrip(".")
    return "0" if s == "-0" else s


def array_to_latex(arr: np.ndarray, env: str = "bmatrix") -> str:
    """Converte un array NumPy 1D o 2D in codice LaTeX \\begin{bmatrix}...\\end{bmatrix}."""
    a = np.asarray(arr)
    if a.ndim == 1:
        a = a.reshape(-1, 1)
    if a.ndim != 2 or a.size == 0:
        raise ValueError("Invalid matrix shape")
    rows_tex: List[str] = []
    for r in range(a.shape[0]):
        cells = [format_number(a[r, c]) for c in range(a.shape[1])]
        rows_tex.append(" & ".join(cells))
    body = r" \\ ".join(rows_tex)
    return rf"\begin{{{env}}} {body} \end{{{env}}}"


def _preprocess_expr(expr: str) -> str:
    s = expr.strip()
    s = (
        s.replace("^", "**")
        .replace("×", "*")
        .replace("·", "*")
        .replace("÷", "/")
        .replace("−", "-")
        .replace("π", "pi")
        .replace(",", ".")
    )
    return s


def _has_operation_or_matrix(node: ast.AST, matrices: Dict[str, np.ndarray]) -> bool:
    """Evita di suggerire calcoli su costanti nude come '5 ='."""
    if isinstance(node, ast.Expression):
        return _has_operation_or_matrix(node.body, matrices)
    if isinstance(node, (ast.BinOp, ast.Call)):
        return True
    if isinstance(node, ast.UnaryOp):
        return _has_operation_or_matrix(node.operand, matrices)
    if isinstance(node, ast.Name):
        return node.id in matrices or node.id in _SAFE_CONSTANTS
    return False


def _eval_ast(
    node: ast.AST,
    matrices: Dict[str, np.ndarray],
) -> Union[float, np.ndarray]:
    if isinstance(node, ast.Expression):
        return _eval_ast(node.body, matrices)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return float(node.value)
        raise ValueError("Unsupported constant")

    if isinstance(node, ast.Name):
        if node.id in matrices:
            return matrices[node.id]
        if node.id in _SAFE_CONSTANTS:
            return _SAFE_CONSTANTS[node.id]
        raise ValueError(f"Unknown identifier: {node.id}")

    if isinstance(node, ast.UnaryOp):
        val = _eval_ast(node.operand, matrices)
        if isinstance(node.op, ast.UAdd):
            return val
        if isinstance(node.op, ast.USub):
            return -val
        raise ValueError("Unsupported unary op")

    if isinstance(node, ast.BinOp):
        left = _eval_ast(node.left, matrices)
        right = _eval_ast(node.right, matrices)
        is_l_mat = isinstance(left, np.ndarray)
        is_r_mat = isinstance(right, np.ndarray)

        if isinstance(node.op, ast.Add):
            if is_l_mat and is_r_mat:
                if left.shape != right.shape:
                    raise ValueError("Matrix shape mismatch for +")
                return left + right
            if is_l_mat or is_r_mat:
                raise ValueError("Cannot add scalar and matrix")
            return left + right

        if isinstance(node.op, ast.Sub):
            if is_l_mat and is_r_mat:
                if left.shape != right.shape:
                    raise ValueError("Matrix shape mismatch for -")
                return left - right
            if is_l_mat or is_r_mat:
                raise ValueError("Cannot subtract scalar and matrix")
            return left - right

        if isinstance(node.op, ast.Mult):
            if is_l_mat and is_r_mat:
                if left.shape[1] != right.shape[0]:
                    raise ValueError("Matrix shape mismatch for *")
                return left @ right
            return left * right

        if isinstance(node.op, ast.Div):
            if is_r_mat:
                raise ValueError("Cannot divide by matrix")
            if right == 0:
                raise ZeroDivisionError("Division by zero")
            return left / right

        if isinstance(node.op, ast.FloorDiv):
            if is_l_mat or is_r_mat:
                raise ValueError("FloorDiv not supported on matrices")
            if right == 0:
                raise ZeroDivisionError("Division by zero")
            return float(left // right)

        if isinstance(node.op, ast.Mod):
            if is_l_mat or is_r_mat:
                raise ValueError("Mod not supported on matrices")
            if right == 0:
                raise ZeroDivisionError("Division by zero")
            return float(left % right)

        if isinstance(node.op, ast.Pow):
            if is_l_mat:
                if is_r_mat or not float(right).is_integer():
                    raise ValueError("Matrix power requires integer exponent")
                exp_i = int(right)
                if abs(exp_i) > 20:
                    raise ValueError("Matrix exponent too large")
                return np.linalg.matrix_power(left, exp_i)
            if is_r_mat:
                raise ValueError("Scalar to matrix power not supported")
            if abs(right) > 1000:
                raise ValueError("Exponent too large")
            return float(left ** right)

        raise ValueError("Unsupported binary op")

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.keywords:
            raise ValueError("Unsupported function call")
        fname = node.func.id
        args = [_eval_ast(a, matrices) for a in node.args]

        if fname in ("inv", "det", "eig", "eigvals", "transpose", "T", "trace", "tr"):
            if len(args) != 1 or not isinstance(args[0], np.ndarray):
                raise ValueError(f"{fname} requires 1 matrix argument")
            mat = args[0]
            if fname in ("transpose", "T"):
                return mat.T
            if mat.ndim != 2 or mat.shape[0] != mat.shape[1]:
                raise ValueError(f"{fname} requires square matrix")
            if fname == "inv":
                return np.linalg.inv(mat)
            if fname == "det":
                return float(np.linalg.det(mat))
            if fname in ("trace", "tr"):
                return float(np.trace(mat))
            if fname in ("eig", "eigvals"):
                vals = np.linalg.eigvals(mat)
                return vals.reshape(-1, 1)

        if fname in _SAFE_SCALAR_FUNCS:
            if len(args) != 1 or isinstance(args[0], np.ndarray):
                raise ValueError(f"{fname} requires 1 scalar argument")
            return float(_SAFE_SCALAR_FUNCS[fname](args[0]))

        raise ValueError(f"Unknown function: {fname}")

    raise ValueError(f"Unsupported AST node: {type(node).__name__}")


def safe_eval_expression(
    expr: str,
    matrices: Optional[Dict[str, np.ndarray]] = None,
) -> Union[float, np.ndarray]:
    """Valuta un'espressione matematica/matriciale in modo sicuro tramite AST."""
    mats = matrices or {}
    cleaned = _preprocess_expr(expr)
    if not cleaned:
        raise ValueError("Empty expression")
    tree = ast.parse(cleaned, mode="eval")
    if not _has_operation_or_matrix(tree, mats):
        raise ValueError("No operation to evaluate")
    return _eval_ast(tree, mats)


def parse_latex_matrix(tex: str) -> Optional[Tuple[Optional[str], np.ndarray]]:
    """
    Estrae un'eventuale etichetta (es. 'A' da 'A = \\begin{bmatrix}...') e
    converte la matrice LaTeX in un np.ndarray di float.
    """
    m = re.search(r"\\begin\{([bpv])matrix\}(.*?)\\end\{\1matrix\}", tex or "", re.DOTALL)
    if not m:
        return None
    prefix = tex[: m.start()].strip()
    label: Optional[str] = None
    if prefix:
        lm = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=$", prefix)
        if lm:
            label = lm.group(1)

    body = m.group(2).strip()
    raw_rows = [r.strip() for r in body.split(r"\\") if r.strip()]
    if not raw_rows:
        return None

    rows_vals: List[List[float]] = []
    ncols = 0
    for r in raw_rows:
        cells = [c.strip() for c in r.split("&")]
        ncols = max(ncols, len(cells))
        row_nums: List[float] = []
        for c in cells:
            if not c:
                row_nums.append(0.0)
            else:
                try:
                    # Permette numeri semplici o espressioni come 1/2, sqrt(2), -3
                    cleaned = _preprocess_expr(c)
                    tree = ast.parse(cleaned, mode="eval")
                    val = _eval_ast(tree, {})
                    if isinstance(val, np.ndarray):
                        return None
                    row_nums.append(float(val))
                except Exception:
                    return None
        rows_vals.append(row_nums)

    for r in rows_vals:
        while len(r) < ncols:
            r.append(0.0)

    return label, np.array(rows_vals, dtype=float)


def evaluate_inline_equals(
    text_before_eq: str,
    matrices: Optional[Dict[str, np.ndarray]] = None,
) -> Optional[Dict[str, str]]:
    """
    Dato il testo sulla riga prima del carattere '=', estrae la sotto-espressione
    matematica finale valida e la valuta.
    Restituisce un dizionario con 'kind' ('scalar' | 'matrix') o None.
    """
    if not text_before_eq:
        return None
    # Ignora operatori relazionali / assegnazioni (==, !=, <=, >=, :=)
    if text_before_eq[-1] in ("=", "!", "<", ">", ":"):
        return None

    raw = text_before_eq.rstrip()
    if not raw:
        return None

    mats = matrices or {}

    # Prova dall'inizio o dopo separatori testuali (es. "Calcola 3 + 2", "Risultato: A * B")
    # Genera candidati partendo dai token più a sinistra verso destra fino all'ultima espressione valida
    candidates: List[str] = [raw]
    for m in re.finditer(r"[\s:;,\[\]\"\']+", raw):
        tail = raw[m.end() :].strip()
        if tail and tail not in candidates:
            candidates.append(tail)

    # Ordina dal candidato più lungo al più corto così "3 + 2 * 4" vince su "2 * 4"
    candidates.sort(key=len, reverse=True)

    for cand in candidates:
        # Evita espressioni che sono solo un nome di matrice nudo (es. "A =" per definire la matrice)
        if cand in mats:
            continue
        try:
            res = safe_eval_expression(cand, mats)
        except Exception:
            continue

        if isinstance(res, np.ndarray):
            try:
                latex = array_to_latex(res, env="bmatrix")
            except Exception:
                return None
            return {
                "kind": "matrix",
                "latex": latex,
                "ghost_text": "  [Premi Tab per inserire matrice risultato]",
            }
        else:
            try:
                num_str = format_number(res)
            except Exception:
                return None
            # Aggiungi spazio solo se il testo non termina già con spazio prima dell'='
            return {
                "kind": "scalar",
                "value": num_str,
                "ghost_text": f" {num_str}",
                "insert_text": f" {num_str}",
            }

    return None
