#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LTS (Live Trading System) - Test Runner

This script provides a convenient way to run all LTS tests with proper configuration.
It supports running tests by level (unit, integration, system, acceptance) and 
provides detailed reporting.
"""

import os
import sys
import subprocess
import argparse
from pathlib import Path

def run_tests(test_level=None, verbose=False, coverage=False, parallel=False):
    """
    Run tests for the specified level or all tests if no level specified.
    
    Args:
        test_level: Test level to run (unit, integration, system, acceptance, all)
        verbose: Run tests in verbose mode
        coverage: Generate coverage report
        parallel: Run tests in parallel
    """
    # Ensure we're in the correct directory
    script_dir = Path(__file__).parent
    os.chdir(script_dir)
    
    # Base pytest command
    cmd = ["python", "-m", "pytest"]
    
    # Add test path based on level
    if test_level and test_level != "all":
        if test_level == "acceptance":
            cmd.append("tests/acceptance/")
        elif test_level == "system":
            cmd.append("tests/system/")
        elif test_level == "integration":
            cmd.append("tests/integration/")
        elif test_level == "unit":
            cmd.append("tests/unit/")
        else:
            print(f"Unknown test level: {test_level}")
            return 1
    else:
        cmd.append("tests/")
    
    # Add verbose flag
    if verbose:
        cmd.append("-v")
    
    # Add coverage
    if coverage:
        cmd.extend(["--cov=app", "--cov-report=html", "--cov-report=term"])
    
    # Add parallel execution
    if parallel:
        cmd.extend(["-n", "auto"])
    
    # Add additional pytest flags
    cmd.extend([
        "--tb=short",  # Shorter traceback format
        "--strict-markers",  # Strict marker enforcement
    ])
    
    print(f"Running command: {' '.join(cmd)}")
    
    # Run the tests
    try:
        result = subprocess.run(cmd, check=False)
        return result.returncode
    except FileNotFoundError:
        print("Error: pytest not found. Please install it with: pip install pytest")
        return 1
    except Exception as e:
        print(f"Error running tests: {e}")
        return 1

def main():
    """Main function to parse arguments and run tests."""
    parser = argparse.ArgumentParser(
        description="Run LTS tests with various options",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run_tests.py                    # Run all tests
  python run_tests.py unit              # Run only unit tests  
  python run_tests.py integration -v    # Run integration tests with verbose output
  python run_tests.py system --coverage # Run system tests with coverage
  python run_tests.py acceptance --parallel # Run acceptance tests in parallel
        """
    )
    
    parser.add_argument(
        "level",
        nargs="?",
        choices=["unit", "integration", "system", "acceptance", "all"],
        default="all",
        help="Test level to run (default: all)"
    )
    
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Run tests in verbose mode"
    )
    
    parser.add_argument(
        "-c", "--coverage",
        action="store_true",
        help="Generate coverage report"
    )
    
    parser.add_argument(
        "-p", "--parallel",
        action="store_true",
        help="Run tests in parallel"
    )
    
    args = parser.parse_args()
    
    # Print header
    print("=" * 60)
    print("LTS (Live Trading System) - Test Runner")
    print("=" * 60)
    print(f"Test Level: {args.level}")
    print(f"Verbose: {args.verbose}")
    print(f"Coverage: {args.coverage}")
    print(f"Parallel: {args.parallel}")
    print("=" * 60)
    
    # Run tests
    exit_code = run_tests(
        test_level=args.level,
        verbose=args.verbose,
        coverage=args.coverage,
        parallel=args.parallel
    )
    
    # Print summary
    print("=" * 60)
    if exit_code == 0:
        print("✅ All tests passed!")
    else:
        print("❌ Some tests failed!")
    print("=" * 60)
    
    return exit_code

if __name__ == "__main__":
    sys.exit(main())
