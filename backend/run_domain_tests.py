"""Minimal runner so the pure-domain tests run without pytest: python run_domain_tests.py"""
import importlib, sys, traceback
sys.path.insert(0, ".")
mod = importlib.import_module("tests.test_domain")
fails = 0
tests = [n for n in dir(mod) if n.startswith("test_")]
for n in tests:
    try:
        getattr(mod, n)(); print("PASS", n)
    except Exception:
        fails += 1; print("FAIL", n); traceback.print_exc()
print(f"\n{len(tests)-fails}/{len(tests)} passed"); sys.exit(1 if fails else 0)
