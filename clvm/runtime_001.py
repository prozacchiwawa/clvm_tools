from opacity.Var import Var

from . import core_ops, more_ops

from .casts import int_from_bytes, int_to_bytes
from .core import make_reduce_f
from .op_utils import operators_for_module
from .subclass_sexp import subclass_sexp


class mixin:
    @classmethod
    def to_atom(class_, v):
        if isinstance(v, int):
            v = int_to_bytes(v)
        return v

    def as_int(self):
        return int_from_bytes(self.as_atom())


to_sexp_f = subclass_sexp(mixin, (bytes, Var))


KEYWORDS = ". q e a i c f r l x = sha256 + - * . wrap unwrap point_add pubkey_for_exp".split()

KEYWORD_FROM_ATOM = {int_to_bytes(k): v for k, v in enumerate(KEYWORDS)}
KEYWORD_TO_ATOM = {v: k for k, v in KEYWORD_FROM_ATOM.items()}

OP_REWRITE = {
    "+": "add",
    "-": "subtract",
    "*": "multiply",
    "/": "divide",
    "i": "if",
    "c": "cons",
    "f": "first",
    "r": "rest",
    "l": "listp",
    "x": "raise",
    "=": "eq",
}


OPERATOR_LOOKUP = operators_for_module(KEYWORD_TO_ATOM, core_ops, OP_REWRITE)
OPERATOR_LOOKUP.update(operators_for_module(KEYWORD_TO_ATOM, more_ops, OP_REWRITE))


reduce_f = make_reduce_f(
    OPERATOR_LOOKUP, KEYWORD_TO_ATOM["q"], KEYWORD_TO_ATOM["e"], KEYWORD_TO_ATOM["a"])