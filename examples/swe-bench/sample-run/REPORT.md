# Bug Fix Report: PolyElement.as_expr() not accepting symbols

## Issue Summary
The `PolyElement.as_expr()` method in `sympy/polys/rings.py` was ignoring the symbols passed as arguments and always using `self.ring.symbols` instead. This meant that users could not substitute their own symbols when converting a polynomial element to a SymPy expression.

## Root Cause
The bug was in the control flow logic of the `as_expr()` method at line 618-624:

```python
def as_expr(self, *symbols):
    if symbols and len(symbols) != self.ring.ngens:
        raise ValueError("not enough symbols, expected %s got %s" % (self.ring.ngens, len(symbols)))
    else:
        symbols = self.ring.symbols

    return expr_from_dict(self.as_expr_dict(), *symbols)
```

The problem was the `else:` clause. The logic flow was:
1. If symbols are provided AND the count is wrong → raise ValueError
2. Otherwise (else) → use `self.ring.symbols`

This meant that even when the correct number of symbols was provided (passing the first condition), the code would still execute the `else` branch and overwrite the provided symbols with `self.ring.symbols`.

## The Fix
Changed line 621 from `else:` to `elif not symbols:` to properly handle three cases:
1. If symbols are provided AND count is wrong → raise ValueError
2. If NO symbols are provided → use `self.ring.symbols` (default behavior)
3. If symbols are provided AND count is correct → use the provided symbols

```python
def as_expr(self, *symbols):
    if symbols and len(symbols) != self.ring.ngens:
        raise ValueError("not enough symbols, expected %s got %s" % (self.ring.ngens, len(symbols)))
    elif not symbols:
        symbols = self.ring.symbols

    return expr_from_dict(self.as_expr_dict(), *symbols)
```

## Verification
Before the fix:
```python
>>> from sympy import ring, ZZ, symbols
>>> R, x, y, z = ring("x,y,z", ZZ)
>>> f = 3*x**2*y - x*y*z + 7*z**3 + 1
>>> U, V, W = symbols("u,v,w")
>>> f.as_expr(U, V, W)
3*x**2*y - x*y*z + 7*z**3 + 1  # Wrong! Still using x, y, z
```

After the fix:
```python
>>> f.as_expr(U, V, W)
3*u**2*v - u*v*w + 7*w**3 + 1  # Correct! Now using u, v, w
```

## Test Results
All tests passed: 855 passed, 3 xfailed, 1383 warnings in 108.45s

The existing test in `sympy/polys/tests/test_rings.py` (function `test_PolyElement_as_expr`) already covered this functionality and now passes correctly with the fix.
