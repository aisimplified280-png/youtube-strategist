import sys
import os
import unittest

def run(target=None):
    os.environ["PYTHONPATH"] = "."
    sys.path.insert(0, ".")
    
    loader = unittest.TestLoader()
    if target:
        if target.endswith(".py"):
            target = target[:-3]
        target = target.replace("/", ".").replace("\\", ".")
        print(f"Loading single test module: {target}...", flush=True)
        suite = loader.loadTestsFromName(target)
    else:
        print("Discovering all tests in tests/...", flush=True)
        suite = loader.discover("tests", pattern="test_*.py")
    
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)

if __name__ == "__main__":
    t = sys.argv[1] if len(sys.argv) > 1 else None
    run(t)
